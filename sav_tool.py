#!/usr/bin/env python3
"""Inspect and losslessly rebuild Discord Times .sav files.

The tool is standard-library only.  It understands the AEpf chunked BZip2
container and, with a companion .DTm, maps the major regions of the
uncompressed save snapshot.

Examples:
  python sav_tool.py info game.sav --pretty
  python sav_tool.py unpack game.sav payload.bin
  python sav_tool.py pack payload.bin rebuilt.sav --template game.sav
  python sav_tool.py roundtrip game.sav rebuilt.sav
  python sav_tool.py relate game.sav map.DTm --pretty -o relation.json
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import json
import struct
import sys
from pathlib import Path
from typing import Any


SAV_MAGIC = b"AEpf"
SAV_DEFAULT_TAG = b"\x40\x00\x11\xe2"
DTM_MAGIC = b"AIpf\r\n\x13\x00"
MAP_MAGIC = b"MapLDV V.4\r\n"
CHUNK_SIZE = 65536
RUNTIME_ARMY_SIZE = 14375
MAP_PREFIX_SIZE = 289
DTM_SECTION_BASE = 0x12F

SECTION_SPECS = (
    ("surface", None),
    ("decorations", 6),
    ("buildings", 358),
    ("armies", 89),
    ("lanterns", 99),
    ("events", 171),
)


class SavError(ValueError):
    pass


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_cstring(data: bytes, offset: int, encoding: str = "cp1251") -> tuple[str, int]:
    end = data.find(b"\0", offset)
    if end < 0:
        raise SavError(f"unterminated string at 0x{offset:X}")
    return data[offset:end].decode(encoding, "replace"), end + 1


def read_cstrings_raw(data: bytes, offset: int, count: int) -> tuple[list[bytes], int]:
    values: list[bytes] = []
    for _ in range(count):
        end = data.find(b"\0", offset)
        if end < 0:
            raise SavError(f"unterminated string at 0x{offset:X}")
        values.append(data[offset:end])
        offset = end + 1
    return values, offset


def unpack_sav_bytes(raw: bytes) -> tuple[bytes, dict[str, Any]]:
    if len(raw) < 16 or raw[:4] != SAV_MAGIC:
        raise SavError("not an AEpf save")
    declared = u32(raw, 8)
    pos = 12
    chunks = []
    parts = []
    while pos < len(raw):
        if pos + 4 > len(raw):
            raise SavError(f"truncated chunk length at 0x{pos:X}")
        compressed_size = u32(raw, pos)
        length_offset = pos
        pos += 4
        end = pos + compressed_size
        if end > len(raw):
            raise SavError(f"chunk at 0x{pos:X} extends past EOF")
        compressed = raw[pos:end]
        if not compressed.startswith(b"BZh1"):
            raise SavError(f"chunk at 0x{pos:X} is not BZip2 level 1")
        try:
            unpacked = bz2.decompress(compressed)
        except OSError as exc:
            raise SavError(f"bad BZip2 chunk at 0x{pos:X}: {exc}") from exc
        chunks.append(
            {
                "index": len(chunks),
                "length_offset": length_offset,
                "data_offset": pos,
                "compressed_size": compressed_size,
                "uncompressed_size": len(unpacked),
            }
        )
        parts.append(unpacked)
        pos = end
    payload = b"".join(parts)
    if len(payload) != declared:
        raise SavError(f"declared {declared} unpacked bytes, got {len(payload)}")
    if any(c["uncompressed_size"] != CHUNK_SIZE for c in chunks[:-1]):
        raise SavError("non-final save chunk is not 64 KiB")
    info = {
        "magic": "AEpf",
        "tag_hex": raw[4:8].hex(" "),
        "file_size": len(raw),
        "sha256": sha256(raw),
        "declared_unpacked_size": declared,
        "actual_unpacked_size": len(payload),
        "payload_sha256": sha256(payload),
        "chunk_size": CHUNK_SIZE,
        "chunk_count": len(chunks),
        "chunks": chunks,
    }
    return payload, info


def pack_sav_bytes(payload: bytes, tag: bytes = SAV_DEFAULT_TAG) -> bytes:
    if len(tag) != 4:
        raise SavError("save tag must be exactly four bytes")
    result = bytearray(SAV_MAGIC + tag + struct.pack("<I", len(payload)))
    for offset in range(0, len(payload), CHUNK_SIZE):
        compressed = bz2.compress(payload[offset:offset + CHUNK_SIZE], compresslevel=1)
        result += struct.pack("<I", len(compressed))
        result += compressed
    return bytes(result)


def read_dtm(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw.startswith(DTM_MAGIC):
        if len(raw) < 16 or raw[12:16] != b"BZh9":
            raise SavError("bad AIpf/BZip2 map wrapper")
        inner = bz2.decompress(raw[12:])
        if len(inner) != u32(raw, 8):
            raise SavError("map unpacked-size field does not match")
    elif raw.startswith(MAP_MAGIC):
        inner = raw
    else:
        raise SavError("not an AIpf .DTm or unpacked MapLDV V.4 stream")
    if not inner.startswith(MAP_MAGIC) or len(inner) < DTM_SECTION_BASE:
        raise SavError("bad or truncated MapLDV V.4 stream")
    return inner


def dtm_layout(inner: bytes) -> tuple[dict[str, dict[str, int]], tuple[int, ...]]:
    sizes = struct.unpack_from("<6I", inner, 0x1C)
    pos = DTM_SECTION_BASE
    sections: dict[str, dict[str, int]] = {}
    for (name, record_size), size in zip(SECTION_SPECS, sizes):
        end = pos + size
        if end > len(inner):
            raise SavError(f"map section {name} extends past EOF")
        item = {"offset": pos, "size": size, "end": end}
        if record_size:
            if size % record_size:
                raise SavError(f"map section {name} has an invalid size")
            item.update({"record_size": record_size, "count": size // record_size})
        sections[name] = item
        pos = end
    return sections, sizes


def save_metadata(payload: bytes) -> dict[str, Any]:
    pos = 0
    values = []
    for label in ("slot_name", "map_name", "timestamp"):
        value, pos = read_cstring(payload, pos)
        values.append((label, value))
    if payload[pos:pos + len(MAP_MAGIC)] != MAP_MAGIC:
        raise SavError(f"MapLDV header not found after save metadata at 0x{pos:X}")
    result = {label: value for label, value in values}
    result["map_header_offset"] = pos
    result["metadata_prefix_size"] = pos
    return result


def hamming(a: bytes, b: bytes) -> int:
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b))


def find_building_start(
    payload: bytes,
    building_count: int,
    army_count: int,
    lantern_count: int,
    event_count: int,
    *,
    expected_start: int | None = None,
) -> tuple[int, int]:
    signature = struct.pack("<4I", building_count, army_count, lantern_count, event_count)
    positions = []
    pos = 0
    while True:
        pos = payload.find(signature, pos)
        if pos < 0:
            break
        positions.append(pos)
        pos += 1
    if not positions:
        raise SavError("building/army/lantern/event count signature not found")
    # In observed V.4 saves the signature directly precedes building[0].  The
    # count tuple can be very weak (for example 0/15/0/0) and may occur inside
    # zero-heavy runtime blocks.  When the compiled-grid formula is available,
    # prefer the structurally expected occurrence instead of the last match.
    if expected_start is not None:
        expected_signature = expected_start - len(signature)
        signature_offset = min(positions, key=lambda value: abs(value - expected_signature))
        # A large disagreement means the map/save layout is not the V.4 layout
        # we know how to edit safely; do not silently choose an unrelated hit.
        if abs(signature_offset - expected_signature) > 64:
            raise SavError(
                "count signature is not at the expected compiled-grid boundary "
                f"(expected 0x{expected_signature:X}, closest 0x{signature_offset:X})"
            )
    else:
        signature_offset = positions[-1]
    return signature_offset + len(signature), signature_offset


def parse_texts_from_map(inner: bytes, sections: dict[str, dict[str, int]]) -> tuple[list[bytes], int]:
    building_count = sections["buildings"]["count"]
    army_count = sections["armies"]["count"]
    event_count = sections["events"]["count"]
    named_count = inner[238]
    count = 4 + building_count * 3 + army_count * 3 + event_count * 3 + named_count
    text_start = u32(inner, 0x18)
    values, end = read_cstrings_raw(inner, text_start, count)
    return values, end


def _find_save_text_start(
    payload: bytes,
    after_events: int,
    expected_strings: list[bytes],
    save_string_count: int,
    army_count: int,
) -> tuple[int, list[bytes], int]:
    """Locate the save text table without requiring the scenario name to match.

    The original implementation searched for map_strings[0] verbatim.  That
    fails after the scenario title is edited (for example New -> New 2), even
    though the binary layout is otherwise valid.  Prefer the direct hit when
    available, then score nearby c-string parses against the remaining known
    strings and validate the resulting dynamic-tail geometry.
    """
    scenario = expected_strings[0] if expected_strings else b''
    if scenario:
        direct = payload.find(scenario + b"\0", after_events, min(len(payload), after_events + 65536))
        if direct >= 0:
            values, end = read_cstrings_raw(payload, direct, save_string_count)
            return direct, values, end

    required_tail = (army_count + 1) * RUNTIME_ARMY_SIZE
    limit = min(len(payload), after_events + 1024)
    best = None
    # Save strings correspond to map_strings[:2] + map_strings[4:].  The first
    # entry (scenario title) is allowed to differ; the rest are strong anchors.
    aligned = expected_strings[:2] + expected_strings[4:]
    for start in range(after_events, limit):
        try:
            values, end = read_cstrings_raw(payload, start, save_string_count)
        except SavError:
            continue
        if end + required_tail > len(payload):
            continue
        # Runtime tail plausibility: inspect up to three NPC unit counts.
        plausible = True
        for ai in range(min(army_count, 3)):
            off = end + (ai + 1) * RUNTIME_ARMY_SIZE + 0x800
            if off + 4 > len(payload) or u32(payload, off) > 12:
                plausible = False
                break
        if not plausible:
            continue
        score = 0
        for i, (expected, actual) in enumerate(zip(aligned, values)):
            if i == 0:
                # scenario title may legitimately change
                continue
            if actual == expected:
                score += 5 if expected else 1
            elif expected and actual:
                # weak prefix affinity helps distinguish renamed titles
                common = 0
                for a, b in zip(expected, actual):
                    if a != b:
                        break
                    common += 1
                score += min(common, 3)
        candidate = (score, -start, start, values, end)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None:
        raise SavError("save text table not found after event array")
    _, _, start, values, end = best
    return start, values, end


def relate(payload: bytes, inner: bytes, outer: dict[str, Any]) -> dict[str, Any]:
    metadata = save_metadata(payload)
    map_offset = metadata["map_header_offset"]
    sections, sizes = dtm_layout(inner)
    width, height = struct.unpack_from("<II", inner, 0x0C)
    cell_count = width * height
    building_count = sections["buildings"]["count"]
    army_count = sections["armies"]["count"]
    lantern_count = sections["lanterns"]["count"]
    event_count = sections["events"]["count"]

    grid_start = map_offset + MAP_PREFIX_SIZE + RUNTIME_ARMY_SIZE
    inferred_grid_size = 38 * cell_count + 24 * width + 9054
    expected_building_start = grid_start + inferred_grid_size
    building_start, signature_offset = find_building_start(
        payload, building_count, army_count, lantern_count, event_count,
        expected_start=expected_building_start,
    )
    observed_grid_size = building_start - grid_start

    map_building_start = sections["buildings"]["offset"]
    map_lantern_start = sections["lanterns"]["offset"]
    map_lanterns = inner[map_lantern_start:sections["lanterns"]["end"]]
    pos = building_start
    building_changed_bytes = 0
    changed_buildings = 0
    garrison_indices: list[int] = []
    building_offsets: list[int] = []
    for index in range(building_count):
        building_offsets.append(pos)
        original = inner[
            map_building_start + index * 358:map_building_start + (index + 1) * 358
        ]
        saved = payload[pos:pos + 358]
        changed = hamming(original, saved)
        if changed:
            changed_buildings += 1
            building_changed_bytes += changed
        pos += 358
        # MapLDV V.4 native controls prove that building types 1/3/4/12
        # always carry one attached 14375-byte garrison/runtime block, even
        # when the garrison is empty.  Type-based parsing also resolves the
        # otherwise ambiguous case "last building + zero lanterns", where the
        # old look-ahead heuristic compared two empty byte ranges.
        has_state = original[6] in (1, 3, 4, 12)
        if has_state:
            garrison_indices.append(index)
            pos += RUNTIME_ARMY_SIZE

    lantern_start = pos
    saved_lanterns = payload[lantern_start:lantern_start + sizes[4]]
    lantern_changed = hamming(map_lanterns, saved_lanterns)
    event_start = lantern_start + sizes[4]
    map_events = inner[sections["events"]["offset"]:sections["events"]["end"]]
    saved_events = payload[event_start:event_start + sizes[5]]
    event_changed = hamming(map_events, saved_events)
    after_events = event_start + sizes[5]

    map_strings, map_text_end = parse_texts_from_map(inner, sections)
    save_string_count = len(map_strings) - 2
    text_start, save_strings, save_text_end = _find_save_text_start(
        payload, after_events, map_strings, save_string_count, army_count
    )
    aligned_map_strings = map_strings[:2] + map_strings[4:]
    changed_strings = [
        {
            "index": i,
            "map": a.decode("cp1251", "replace"),
            "save": b.decode("cp1251", "replace"),
        }
        for i, (a, b) in enumerate(zip(aligned_map_strings, save_strings))
        if a != b
    ]

    prefix_matches = payload[map_offset:map_offset + MAP_PREFIX_SIZE] == inner[:MAP_PREFIX_SIZE]
    header_differences_303 = [
        [i, a, b]
        for i, (a, b) in enumerate(zip(inner[:303], payload[map_offset:map_offset + 303]))
        if a != b
    ]

    return {
        "save_container": outer,
        "save_metadata": metadata,
        "map": {
            "inner_size": len(inner),
            "inner_sha256": sha256(inner),
            "width": width,
            "height": height,
            "cell_count": cell_count,
            "section_sizes": dict(zip((x[0] for x in SECTION_SPECS), sizes)),
            "counts": {
                "buildings": building_count,
                "armies": army_count,
                "lanterns": lantern_count,
                "events": event_count,
            },
        },
        "relationship": {
            "exact_map_prefix_size": MAP_PREFIX_SIZE,
            "map_prefix_matches": prefix_matches,
            "first_303_byte_differences": header_differences_303,
            "initial_opaque_state": {
                "offset": map_offset + MAP_PREFIX_SIZE,
                "size": RUNTIME_ARMY_SIZE,
                "end": grid_start,
            },
            "compiled_grid": {
                "offset": grid_start,
                "end": building_start,
                "observed_size": observed_grid_size,
                "inferred_formula": "38*width*height + 24*width + 9054",
                "inferred_size": inferred_grid_size,
                "formula_matches": observed_grid_size == inferred_grid_size,
                "inferred_u16_cell_layers": 19,
                "cell_layers_size": 38 * cell_count,
                "row_and_fixed_tail_size": 24 * width + 9054,
            },
            "count_signature_offset": signature_offset,
            "buildings": {
                "offset": building_start,
                "count": building_count,
                "record_size": 358,
                "changed_record_count_vs_dtm": changed_buildings,
                "changed_byte_count_vs_dtm": building_changed_bytes,
                "runtime_garrison_state_size": RUNTIME_ARMY_SIZE,
                "runtime_garrison_count": len(garrison_indices),
                "runtime_garrison_building_indices": garrison_indices,
                "record_offsets": building_offsets,
            },
            "raw_dtm_army_section_present_here": False,
            "lanterns": {
                "offset": lantern_start,
                "size": sizes[4],
                "count": lantern_count,
                "record_size": 99,
                "changed_byte_count_vs_dtm": lantern_changed,
            },
            "events": {
                "offset": event_start,
                "size": sizes[5],
                "count": event_count,
                "record_size": 171,
                "changed_byte_count_vs_dtm": event_changed,
            },
            "pre_text_state": {"offset": after_events, "size": text_start - after_events},
            "texts": {
                "offset": text_start,
                "end": save_text_end,
                "string_count": save_string_count,
                "omitted_map_strings": ["campaign_name", "next_scenario"],
                "changed_string_count_vs_dtm": len(changed_strings),
                "changed_strings": changed_strings,
                "map_text_end": map_text_end,
            },
            "dynamic_tail": {
                "offset": save_text_end,
                "size": len(payload) - save_text_end,
            },
        },
    }


def output_json(value: Any, pretty: bool, path: Path | None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2 if pretty else None)
    if path:
        path.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def command_info(args: argparse.Namespace) -> None:
    raw = args.save.read_bytes()
    payload, info = unpack_sav_bytes(raw)
    info["metadata"] = save_metadata(payload)
    output_json(info, args.pretty, args.output)


def command_unpack(args: argparse.Namespace) -> None:
    payload, _ = unpack_sav_bytes(args.save.read_bytes())
    args.payload.write_bytes(payload)
    print(f"wrote {len(payload)} bytes to {args.payload}")


def command_pack(args: argparse.Namespace) -> None:
    payload = args.payload.read_bytes()
    tag = SAV_DEFAULT_TAG
    if args.template:
        raw = args.template.read_bytes()
        if len(raw) < 8 or raw[:4] != SAV_MAGIC:
            raise SavError("template is not an AEpf save")
        tag = raw[4:8]
    packed = pack_sav_bytes(payload, tag)
    args.save.write_bytes(packed)
    print(f"wrote {len(packed)} bytes to {args.save}")


def command_roundtrip(args: argparse.Namespace) -> None:
    original = args.input.read_bytes()
    payload, info = unpack_sav_bytes(original)
    rebuilt = pack_sav_bytes(payload, original[4:8])
    args.output.write_bytes(rebuilt)
    result = {
        "input_sha256": sha256(original),
        "output_sha256": sha256(rebuilt),
        "byte_identical": original == rebuilt,
        "payload_size": len(payload),
        "chunk_count": info["chunk_count"],
    }
    output_json(result, True, None)


def command_relate(args: argparse.Namespace) -> None:
    payload, outer = unpack_sav_bytes(args.save.read_bytes())
    inner = read_dtm(args.map)
    output_json(relate(payload, inner, outer), args.pretty, args.output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="show AEpf/chunk metadata")
    p.add_argument("save", type=Path)
    p.add_argument("--pretty", action="store_true")
    p.add_argument("-o", "--output", type=Path)
    p.set_defaults(func=command_info)

    p = sub.add_parser("unpack", help="extract the complete uncompressed payload")
    p.add_argument("save", type=Path)
    p.add_argument("payload", type=Path)
    p.set_defaults(func=command_unpack)

    p = sub.add_parser("pack", help="wrap an edited payload as a save")
    p.add_argument("payload", type=Path)
    p.add_argument("save", type=Path)
    p.add_argument("--template", type=Path, help="copy the four-byte tag from an existing save")
    p.set_defaults(func=command_pack)

    p = sub.add_parser("roundtrip", help="unpack and rebuild, then test byte identity")
    p.add_argument("input", type=Path)
    p.add_argument("output", type=Path)
    p.set_defaults(func=command_roundtrip)

    p = sub.add_parser("relate", help="map save regions against a companion .DTm")
    p.add_argument("save", type=Path)
    p.add_argument("map", type=Path)
    p.add_argument("--pretty", action="store_true")
    p.add_argument("-o", "--output", type=Path)
    p.set_defaults(func=command_relate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except (OSError, SavError, EOFError, struct.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
