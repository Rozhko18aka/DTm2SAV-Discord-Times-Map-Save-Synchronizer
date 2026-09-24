#!/usr/bin/env python3
"""Copy future Discord Times quests and supported appended objects into SAV.

The source save is compared with the original DTm that it was created from.
An event is considered progressed/touched when its 171-byte save record differs
from the original DTm record.  Such events and their text are preserved.
Only changed events whose save records still equal the original are imported
from the modified DTm.

Army runtime state is synchronized conservatively: existing gameplay state is
preserved when it no longer matches the original DTm, while unprogressed army
fields, compositions, additions, deletions and ID references can be rebuilt.
Terrain is rebuilt per changed cell. Buildings, armies, lanterns and decorations
may be added, edited, moved or removed. Building runtime state is merged
conservatively; garrisons are rebuilt from native unit profiles when their
composition changes. Decoration metadata is read from the game's Objects.ugs atlas.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sav_tool
import army_runtime
import building_runtime


EVENT_SIZE = 171
BUILDING_SIZE = 358
ARMY_SIZE = 89
LANTERN_SIZE = 99
HERO_TOKEN = b"#HERONAME"
RUNTIME_GARRISON_SIZE = building_runtime.RUNTIME_GARRISON_SIZE
RUNTIME_GARRISON_TYPES = building_runtime.GARRISON_TYPES
PROPERTY_A_SURFACES = frozenset((0, 1, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14))
PROPERTY_B_SURFACES = frozenset((0, 1, 8, 10, 11, 12, 13, 14))
PROPERTY_A_BASE_VALUES = {
    0: frozenset((2,)),
    1: frozenset((1,)),
    4: frozenset((15,)),
    5: frozenset((25,)),
    6: frozenset((25,)),
    7: frozenset((25,)),
    8: frozenset((8, 40)),
    10: frozenset((5, 25)),
    11: frozenset((5, 25)),
    12: frozenset((4,)),
    13: frozenset((5,)),
    14: frozenset((6,)),
}
PROPERTY_B_BASE_VALUES = {
    0: frozenset((2,)),
    1: frozenset((1,)),
    8: frozenset((40,)),
    10: frozenset((25,)),
    11: frozenset((25,)),
    12: frozenset((20,)),
    13: frozenset((25,)),
    14: frozenset((30,)),
}
PROPERTY_A_TERRAIN_BASE = {
    0: 2, 1: 1, 2: 0, 3: 0, 4: 15, 5: 25, 6: 25, 7: 25,
    8: 8, 9: 0, 10: 5, 11: 5, 12: 4, 13: 5, 14: 6, 15: 0,
}
PROPERTY_B_TERRAIN_BASE = {
    0: 2, 1: 1, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0,
    8: 40, 9: 0, 10: 25, 11: 25, 12: 20, 13: 25, 14: 30, 15: 0,
}
PROPERTY_C_TERRAIN_BASE = {
    0: 0, 1: 0, 2: 0, 3: 0, 4: 3, 5: 5, 6: 5, 7: 5,
    8: 8, 9: 0, 10: 5, 11: 5, 12: 4, 13: 5, 14: 6, 15: 0,
}
PROPERTY_C_BASE_VALUES = {
    tile_id: frozenset((value,))
    for tile_id, value in PROPERTY_C_TERRAIN_BASE.items()
}
VISUAL_TERRAIN_BASE = {
    0: 10956, 1: 2603, 2: 2538, 3: 24999,
    4: 31463, 5: 4771, 6: 10944, 7: 25248,
    8: 7077, 9: 12965, 10: 42057, 11: 25253,
    12: 37871, 13: 12711, 14: 65535, 15: 65535,
}


class QuestSyncError(ValueError):
    pass


@dataclass(frozen=True)
class EventDecision:
    index: int
    event_id: int
    progressed: bool
    binary_changed_in_modified_map: bool
    text_changed_in_modified_map: bool
    selected_for_import: bool
    save_vs_original_changed_bytes: int
    modified_vs_original_changed_bytes: int
    original_title: str
    modified_title: str
    change_summary: str
    save_change_ranges: tuple[str, ...]
    binary_change_ranges: tuple[str, ...]
    text_changes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "event_id": self.event_id,
            "progressed": self.progressed,
            "binary_changed_in_modified_map": self.binary_changed_in_modified_map,
            "text_changed_in_modified_map": self.text_changed_in_modified_map,
            "selected_for_import": self.selected_for_import,
            "save_vs_original_changed_bytes": self.save_vs_original_changed_bytes,
            "modified_vs_original_changed_bytes": self.modified_vs_original_changed_bytes,
            "original_title": self.original_title,
            "modified_title": self.modified_title,
            "change_summary": self.change_summary,
            "save_change_ranges": list(self.save_change_ranges),
            "binary_change_ranges": list(self.binary_change_ranges),
            "text_changes": list(self.text_changes),
        }


@dataclass(frozen=True)
class ArmyChange:
    index: int
    army_id: int
    binary_changed: bool
    text_changed: bool
    old_x: int | None
    old_y: int | None
    new_x: int | None
    new_y: int | None
    original_title: str
    modified_title: str
    change_summary: str
    binary_change_ranges: tuple[str, ...]
    text_changes: tuple[str, ...]
    action: str = "modify"
    source_index: int | None = None
    target_index: int | None = None

    @property
    def text_import_supported(self) -> bool:
        return self.text_changed

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "army_id": self.army_id,
            "action": self.action,
            "source_index": self.source_index,
            "target_index": self.target_index,
            "binary_changed": self.binary_changed,
            "text_changed": self.text_changed,
            "text_import_supported": self.text_import_supported,
            "old_coordinates": [self.old_x, self.old_y] if self.old_x is not None else None,
            "new_coordinates": [self.new_x, self.new_y] if self.new_x is not None else None,
            "original_title": self.original_title,
            "modified_title": self.modified_title,
            "change_summary": self.change_summary,
            "binary_change_ranges": list(self.binary_change_ranges),
            "text_changes": list(self.text_changes),
        }


@dataclass(frozen=True)
class BuildingAddition:
    index: int
    building_type: int
    picture_number: int
    picture_variant: int
    raw_x: int
    raw_y: int
    save_x: int
    save_y: int
    coordinate_template_index: int | None
    runtime_state_added: bool
    pointer_template_index: int | None
    core: bytes = b""
    state: bytes = b""
    serialized_size: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "building_id": self.index + 1,
            "building_type": self.building_type,
            "picture_number": self.picture_number,
            "picture_variant": self.picture_variant,
            "raw_coordinates": [self.raw_x, self.raw_y],
            "save_coordinates": [self.save_x, self.save_y],
            "coordinate_template_building_id": (
                self.coordinate_template_index + 1
                if self.coordinate_template_index is not None else None
            ),
            "runtime_state_added": self.runtime_state_added,
            "pointer_template_building_id": (
                self.pointer_template_index + 1
                if self.pointer_template_index is not None else None
            ),
            "serialized_size": (
                self.serialized_size
                if self.serialized_size is not None
                else len(self.core) + len(self.state)
            ),
        }


@dataclass(frozen=True)
class LanternAddition:
    index: int
    x: int
    y: int
    lantern_id: int
    record: bytes

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "lantern_id": self.lantern_id,
            "coordinates": [self.x, self.y],
            "serialized_size": len(self.record),
        }


@dataclass(frozen=True)
class DecorationAtlasRecord:
    object_id: int
    kind: int
    sprite_width: int
    sprite_height: int
    color_r: int
    color_g: int
    color_b: int
    alpha: int

    @property
    def passable(self) -> bool:
        return self.kind in (1, 2, 3, 4, 9, 10)

    @property
    def is_tree(self) -> bool:
        return self.kind in (9, 10, 11)

    @property
    def footprint(self) -> tuple[int, int]:
        if self.is_tree:
            return (1, 1)
        width = max(1, self.sprite_width // 32 - 1)
        if self.kind in (3, 6):
            return (width, width)
        return (width, max(1, self.sprite_height // 22 - 1))


@dataclass(frozen=True)
class DecorationAddition:
    index: int
    x: int
    y: int
    object_id: int
    atlas: DecorationAtlasRecord

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "coordinates": [self.x, self.y],
            "object_id": self.object_id,
            "ugs_kind": self.atlas.kind,
            "passable": self.atlas.passable,
            "visual_footprint": list(self.atlas.footprint),
            "representative_bgra": [
                self.atlas.color_b,
                self.atlas.color_g,
                self.atlas.color_r,
                self.atlas.alpha,
            ],
        }


@dataclass(frozen=True)
class TerrainChange:
    x: int
    y: int
    old_tile: int
    new_tile: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "coordinates": [self.x, self.y],
            "old_tile": self.old_tile,
            "new_tile": self.new_tile,
        }


@dataclass
class SyncPlan:
    source_sav: Path
    original_dtm: Path
    modified_dtm: Path
    save_raw: bytes
    payload: bytes
    container_info: dict[str, Any]
    original_inner: bytes
    modified_inner: bytes
    relationship: dict[str, Any]
    original_sections: dict[str, dict[str, int]]
    modified_sections: dict[str, dict[str, int]]
    original_strings: list[bytes]
    modified_strings: list[bytes]
    save_strings: list[bytes]
    text_start: int
    text_end: int
    map_header_offset: int
    event_start: int
    event_count: int
    modified_event_count: int
    building_count: int
    modified_building_count: int
    building_mapping: list[int | None]
    building_deleted_indices: list[int]
    building_structure_changed: bool
    building_binary_changed: bool
    army_count: int
    modified_army_count: int
    army_changes: list[ArmyChange]
    lantern_count: int
    modified_lantern_count: int
    width: int
    height: int
    cell_table_base_u16: int
    cell_table_stride: int
    property_ring_size_u16: int
    property_rotation_u16: int
    visual_grid_base_u16: int
    visual_grid_stride: int
    terrain_grid_base_byte: int
    terrain_grid_stride_bytes: int
    objects_ugs: Path | None
    hero_replacement: bytes | None
    building_additions: list[BuildingAddition]
    building_navigation_unseen_profile_ids: list[int]
    building_runtime_template_missing_ids: list[int]
    lantern_additions: list[LanternAddition]
    lantern_records: list[LanternAddition]
    lantern_rewrite: bool
    lantern_removed_ids: list[int]
    lantern_changed_ids: list[int]
    decoration_additions: list[DecorationAddition]
    decoration_removals: list[DecorationAddition]
    decoration_rebuild_cells: set[tuple[int, int]]
    decoration_rebuild_original: list[DecorationAddition]
    decoration_rebuild_modified: list[DecorationAddition]
    terrain_changes: list[TerrainChange]
    terrain_reapply_decorations: list[DecorationAddition]
    decisions: list[EventDecision]

    @property
    def selected(self) -> list[EventDecision]:
        return [item for item in self.decisions if item.selected_for_import]

    @property
    def progressed(self) -> list[EventDecision]:
        return [item for item in self.decisions if item.progressed]

    @property
    def modified_progressed(self) -> list[EventDecision]:
        return [
            item for item in self.decisions
            if item.progressed
            and (item.binary_changed_in_modified_map or item.text_changed_in_modified_map)
        ]


def hamming(a: bytes, b: bytes) -> int:
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b))


def decode_surface(
    inner: bytes,
    section: dict[str, int],
    expected_cells: int,
) -> bytes:
    encoded = inner[section["offset"]:section["end"]]
    if len(encoded) % 2:
        raise QuestSyncError("RLE-секция ландшафта имеет нечётный размер.")
    decoded = bytearray()
    for value, run_minus_one in zip(encoded[0::2], encoded[1::2]):
        if len(decoded) + run_minus_one + 1 > expected_cells:
            raise QuestSyncError('RLE-ландшафт превышает заявленное количество клеток.')
        decoded.extend(bytes((value,)) * (run_minus_one + 1))
    if len(decoded) != expected_cells:
        raise QuestSyncError(
            f"Ландшафт содержит {len(decoded)} клеток вместо {expected_cells}."
        )
    return bytes(decoded)


def army_text_base(building_count: int, *, save: bool) -> int:
    # DTm: scenario, description, campaign, next scenario, then object texts.
    # SAV: campaign and next-scenario strings are omitted.
    return (2 if save else 4) + building_count * 3


def event_text_base(building_count: int, army_count: int, *, save: bool) -> int:
    return army_text_base(building_count, save=save) + army_count * 3


def describe_binary_changes(before: bytes, after: bytes) -> tuple[str, ...]:
    """Describe contiguous changed byte ranges without guessing field semantics."""
    if len(before) != len(after):
        return (f"размер {len(before)} → {len(after)} байт",)

    ranges: list[str] = []
    start: int | None = None
    for index, (old, new) in enumerate(zip(before, after)):
        if old != new and start is None:
            start = index
        if old == new and start is not None:
            end = index
            old_blob = before[start:end]
            new_blob = after[start:end]
            label = f"0x{start:02X}" if end - start == 1 else f"0x{start:02X}–0x{end - 1:02X}"
            ranges.append(f"{label}: {old_blob.hex(' ').upper()} → {new_blob.hex(' ').upper()}")
            start = None
    if start is not None:
        end = len(before)
        old_blob = before[start:end]
        new_blob = after[start:end]
        label = f"0x{start:02X}" if end - start == 1 else f"0x{start:02X}–0x{end - 1:02X}"
        ranges.append(f"{label}: {old_blob.hex(' ').upper()} → {new_blob.hex(' ').upper()}")
    return tuple(ranges)


def normalize_event_text_for_compare(value: bytes) -> bytes:
    """Normalize editor-only line-ending differences in event text.

    CRLF, LF and lone CR are treated as the same line break.  Trailing line
    breaks are ignored completely, while line breaks inside the text remain
    significant.  Spaces and every other byte remain significant.
    """
    normalized = value.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return normalized.rstrip(b"\n")


def event_texts_equal(before: bytes, after: bytes) -> bool:
    return normalize_event_text_for_compare(before) == normalize_event_text_for_compare(after)


def _display_event_text(value: bytes, limit: int = 80) -> str:
    text = value.decode("cp1251", "replace").replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def describe_event_text_changes(
    before: list[bytes] | tuple[bytes, ...],
    after: list[bytes] | tuple[bytes, ...],
) -> tuple[str, ...]:
    labels = ("Название", "Текст 2", "Текст 3")
    changes: list[str] = []
    for index, (old, new) in enumerate(zip(before, after)):
        if event_texts_equal(old, new):
            continue
        label = labels[index] if index < len(labels) else f"Текст {index + 1}"
        changes.append(
            f'{label}: «{_display_event_text(old)}» → «{_display_event_text(new)}»'
        )
    return tuple(changes)


def summarize_event_changes(
    binary_ranges: tuple[str, ...],
    text_changes: tuple[str, ...],
    *,
    new_event: bool = False,
) -> str:
    parts: list[str] = []
    if new_event:
        parts.append("новое событие")
    elif binary_ranges:
        if len(binary_ranges) == 1:
            parts.append("бинарное поле: 1 диапазон")
        elif 2 <= len(binary_ranges) <= 4:
            parts.append(f"бинарные поля: {len(binary_ranges)} диапазона")
        else:
            parts.append(f"бинарные поля: {len(binary_ranges)} диапазонов")
    if text_changes:
        parts.extend(change.split(":", 1)[0] for change in text_changes)
    return "; ".join(parts) if parts else "без изменений"


def extract_token_replacement(original: bytes, saved: bytes) -> bytes | None:
    marker = original.find(HERO_TOKEN)
    if marker < 0:
        return None
    prefix = original[:marker]
    suffix = original[marker + len(HERO_TOKEN):]
    if not saved.startswith(prefix):
        return None
    if suffix and not saved.endswith(suffix):
        return None
    end = len(saved) - len(suffix) if suffix else len(saved)
    if end < len(prefix):
        return None
    return saved[len(prefix):end]


def infer_hero_replacement(original_aligned: list[bytes], saved: list[bytes]) -> bytes | None:
    candidates = []
    for original, current in zip(original_aligned, saved):
        replacement = extract_token_replacement(original, current)
        if replacement is not None and replacement != HERO_TOKEN:
            candidates.append(replacement)
    if not candidates:
        return None
    first = candidates[0]
    return first if all(item == first for item in candidates) else None


def _building_record(inner: bytes, section: dict[str, int], index: int) -> bytes:
    start = section["offset"] + index * BUILDING_SIZE
    return inner[start:start + BUILDING_SIZE]


def _runtime_building_record(payload: bytes, relationship: dict[str, Any], index: int) -> bytes:
    start = relationship["buildings"]["record_offsets"][index]
    return payload[start:start + BUILDING_SIZE]


def _coordinate_delta_candidates(
    raw: bytes,
    original: bytes,
    original_buildings: dict[str, int],
    payload: bytes,
    relationship: dict[str, Any],
    building_count: int,
) -> list[tuple[int, int, int]]:
    tiers: list[list[tuple[int, int, int]]] = [[], [], []]
    raw_x, raw_y = struct.unpack_from("<HH", raw, 0)
    for index in range(building_count):
        candidate = _building_record(original, original_buildings, index)
        if candidate[6] != raw[6] or candidate[290] != raw[290]:
            continue
        saved = _runtime_building_record(payload, relationship, index)
        saved_x, saved_y = struct.unpack_from("<HH", saved, 0)
        candidate_x, candidate_y = struct.unpack_from("<HH", candidate, 0)
        item = (index, candidate_x - saved_x, candidate_y - saved_y)
        if candidate[4:7] == raw[4:7]:
            tiers[0].append(item)
        elif candidate[5:7] == raw[5:7]:
            tiers[1].append(item)
        else:
            tiers[2].append(item)
    for tier in tiers:
        if tier:
            valid = [item for item in tier if raw_x >= item[1] and raw_y >= item[2]]
            if valid:
                return valid
    return []


def _select_coordinate_template(candidates: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    counts: dict[tuple[int, int], int] = {}
    for _, dx, dy in candidates:
        counts[(dx, dy)] = counts.get((dx, dy), 0) + 1
    best_delta, best_count = max(counts.items(), key=lambda item: item[1])
    if sum(value == best_count for value in counts.values()) > 1:
        raise QuestSyncError(
            "Не удалось однозначно определить преобразование координат нового строения."
        )
    for index, dx, dy in candidates:
        if (dx, dy) == best_delta:
            return index, dx, dy
    raise AssertionError("coordinate template selection failed")


def _empty_runtime_garrison(defense: int) -> bytes:
    state = bytearray(RUNTIME_GARRISON_SIZE)
    state[5732:5736] = b"\xff" * 4
    state[5752:5764] = b"\xff" * 12
    state[5772:5780] = b"\xff" * 8
    state[14220] = defense
    return bytes(state)


def _record(inner: bytes, section: dict[str, int], index: int, size: int) -> bytes:
    start = section["offset"] + index * size
    return inner[start:start + size]


def infer_cell_table(
    payload: bytes,
    original: bytes,
    sections: dict[str, dict[str, int]],
    relationship: dict[str, Any],
) -> tuple[int, int]:
    """Locate the nine-u16-per-cell lookup table in the compiled SAV grid.

    The table uses a padded row stride of width + 8.  Its absolute location is
    map-size dependent, so it is inferred from every known building and
    lantern ID instead of relying on a hard-coded offset.
    """
    width, height = struct.unpack_from("<II", original, 0x0C)
    stride = width + 8
    grid = relationship["compiled_grid"]
    raw = payload[grid["offset"]:grid["offset"] + grid["cell_layers_size"]]
    if len(raw) % 2:
        raise QuestSyncError("Клеточная область SAV имеет нечётный размер.")
    values = struct.unpack(f"<{len(raw) // 2}H", raw)

    wanted: set[int] = set()
    building_count = sections["buildings"]["count"]
    wanted.update(range(1, building_count + 1))
    lantern_count = sections["lanterns"]["count"]
    for index in range(lantern_count):
        wanted.add(_record(original, sections["lanterns"], index, LANTERN_SIZE)[4])
    positions: dict[int, list[int]] = {value: [] for value in wanted}
    for position, value in enumerate(values):
        if value in positions:
            positions[value].append(position)

    candidates: dict[int, int] = {}
    used_known_empty_layout = False
    for index in range(building_count):
        record = _building_record(original, sections["buildings"], index)
        x, y = struct.unpack_from("<HH", record, 0)
        expected = index + 1
        for position in positions.get(expected, ()):  # field +1 is building ID
            base = position - 9 * (x + stride * y) - 1
            candidates[base] = candidates.get(base, 0) + 1
    for index in range(lantern_count):
        record = _record(original, sections["lanterns"], index, LANTERN_SIZE)
        x, y = struct.unpack_from("<HH", record, 0)
        expected = record[4]
        for position in positions.get(expected, ()):  # field +2 is lantern ID
            base = position - 9 * (x + stride * y) - 2
            candidates[base] = candidates.get(base, 0) + 1
    if not candidates:
        # Empty/minimal maps may contain neither buildings nor lanterns.  In
        # that case use the already-loaded runtime NPC armies as structural
        # anchors: cell field +3 stores (ArmyID+1)*0x0101 and +4 stores kind 18.
        # Current runtime coordinates are used intentionally, so this also
        # works after armies have moved since the initial map load.
        army_count = sections["armies"]["count"]
        tail_info = relationship.get("dynamic_tail", {})
        tail_off = tail_info.get("offset") if isinstance(tail_info, dict) else None
        if isinstance(tail_off, int):
            tail = payload[tail_off:]
            for index in range(army_count):
                a = (index + 1) * army_runtime.RUNTIME_ARMY_SIZE
                b = a + army_runtime.RUNTIME_ARMY_SIZE
                if b > len(tail):
                    break
                block = tail[a:b]
                try:
                    x = struct.unpack_from("<I", block, army_runtime.CURRENT_X_OFF)[0]
                    y = struct.unpack_from("<I", block, army_runtime.CURRENT_Y_OFF)[0]
                except struct.error:
                    continue
                if not (0 <= x < width and 0 <= y < height):
                    continue
                marker, kind = army_runtime.encode_army_cell_marker(index + 1)
                for position, value in enumerate(values):
                    if value != marker:
                        continue
                    if position + 1 >= len(values) or values[position + 1] != kind:
                        continue
                    base = position - 9 * (x + stride * y) - 3
                    if base < 0:
                        continue
                    candidates[base] = candidates.get(base, 0) + 2
        if not candidates:
            # A completely empty/minimal map can legitimately have no
            # buildings, lanterns *and* no live army occupancy markers (for
            # example, the 102-army profile corpus where every army starts
            # inactive).  In that situation there is no content anchor at all.
            # MapLDV V.4 serializes the cell layers deterministically; these
            # two layouts are byte-certified by native 100x100 and 200x200
            # saves.  Refuse unknown geometry rather than extrapolating.
            known_empty_bases = {
                (50, 50): 24701,
                (100, 100): 90740,
                (200, 200): 377540,
            }
            fallback = known_empty_bases.get((width, height))
            last_index = None
            if fallback is not None:
                last_index = fallback + 9 * ((width - 1) + stride * (height - 1)) + 8
            if (
                fallback is not None
                and original.startswith(b"MapLDV V.4\r\n")
                and len(values) * 2 == sav_tool.compiled_grid_profile(width, height)['cell_layers_size']
                and last_index is not None
                and last_index < len(values)
            ):
                candidates[fallback] = 1
                used_known_empty_layout = True
            else:
                raise QuestSyncError("Не удалось найти клеточную таблицу объектов в SAV.")
    base, _ = max(candidates.items(), key=lambda item: item[1])

    def value_at(x: int, y: int, field: int) -> int:
        position = base + 9 * (x + stride * y) + field
        if not 0 <= position < len(values):
            raise QuestSyncError("Клеточная таблица объектов выходит за границы SAV.")
        return values[position]

    for index in range(building_count):
        record = _building_record(original, sections["buildings"], index)
        x, y = struct.unpack_from("<HH", record, 0)
        if value_at(x, y, 1) != index + 1 or value_at(x, y, 5) != x + stride * y:
            raise QuestSyncError("Клеточная таблица строений SAV не прошла проверку.")
    for index in range(lantern_count):
        record = _record(original, sections["lanterns"], index, LANTERN_SIZE)
        x, y = struct.unpack_from("<HH", record, 0)
        if value_at(x, y, 2) != record[4]:
            raise QuestSyncError("Клеточная таблица фонарей SAV не прошла проверку.")

    # On maps where armies were needed as the only anchors, require at least
    # one marker to validate at the selected base.  Do not require every army:
    # inactive/removed armies legitimately have no live cell marker.
    if building_count == 0 and lantern_count == 0 and not used_known_empty_layout:
        army_count = sections["armies"]["count"]
        tail_info = relationship.get("dynamic_tail", {})
        tail_off = tail_info.get("offset") if isinstance(tail_info, dict) else None
        validated = 0
        if isinstance(tail_off, int):
            tail = payload[tail_off:]
            for index in range(army_count):
                a = (index + 1) * army_runtime.RUNTIME_ARMY_SIZE
                b = a + army_runtime.RUNTIME_ARMY_SIZE
                if b > len(tail):
                    break
                block = tail[a:b]
                try:
                    x = struct.unpack_from("<I", block, army_runtime.CURRENT_X_OFF)[0]
                    y = struct.unpack_from("<I", block, army_runtime.CURRENT_Y_OFF)[0]
                except struct.error:
                    continue
                if not (0 <= x < width and 0 <= y < height):
                    continue
                marker, kind = army_runtime.encode_army_cell_marker(index + 1)
                try:
                    if value_at(x, y, 3) == marker and value_at(x, y, 4) == kind:
                        validated += 1
                except QuestSyncError:
                    pass
        if validated == 0:
            raise QuestSyncError("Клеточная таблица SAV не прошла проверку по runtime-армиям.")
    return base, stride


def infer_decoration_grid_layout(
    cell_table_base_u16: int,
    width: int,
    height: int,
    cell_layer_u16_count: int,
) -> tuple[int, int, int, int]:
    """Derive the two property rings and the padded visual grid.

    The layout was verified against native saves for both 100x100 and 200x200
    maps. The four spare words in each property ring are part of the game's
    serializer and must be included in the modulo operation.
    """
    cell_count = width * height
    ring_size = cell_count + 4
    visual_stride = width + 4
    if (width, height) == (50, 50):
        # Measured from all 31 building / 54 lantern anchors and 2500 terrain
        # cells in the native control. C begins at window word 0, A/B at
        # 2504/5008. These are complete linear layers, not wrapped fragments.
        if cell_table_base_u16 != 24701 or cell_layer_u16_count < 54980:
            raise QuestSyncError('Карта 50×50 не соответствует проверенному расположению клеточных слоёв.')
        return ring_size, 2504, 20534, visual_stride
    visual_base = cell_table_base_u16 - (3 * cell_count // 2 + 8 * width + 17)
    rotation = (visual_base - 7 * cell_count - 10 * width - 30) % ring_size
    if 2 * ring_size > cell_layer_u16_count:
        raise QuestSyncError("Скрытые сетки декораций выходят за клеточную область SAV.")
    if not 0 <= visual_base < cell_layer_u16_count:
        raise QuestSyncError("Не удалось определить начало визуальной сетки декораций.")
    if visual_base + visual_stride * height > cell_layer_u16_count:
        raise QuestSyncError("Визуальная сетка декораций выходит за клеточную область SAV.")
    return ring_size, rotation, visual_base, visual_stride


def infer_terrain_grid_layout(
    payload: bytes,
    surface: bytes,
    width: int,
    height: int,
    relationship: dict[str, Any],
    cell_table_base_u16: int,
) -> tuple[int, int]:
    """Locate the byte-packed terrain grid immediately before the cell table."""
    grid = relationship["compiled_grid"]
    raw = payload[grid["offset"]:grid["offset"] + grid["cell_layers_size"]]
    stride = width + 2
    table_byte = cell_table_base_u16 * 2
    expected_size = stride * height
    search_start = max(0, table_byte - expected_size - 8 * stride)
    search_end = min(len(raw), table_byte - expected_size + 8 * stride)
    first_row = surface[:width]
    position = search_start
    while True:
        found = raw.find(first_row, position, search_end + width + 1)
        if found < 0:
            break
        base = found - 1
        if base >= 0 and all(
            raw[base + y * stride + 1:base + y * stride + 1 + width]
            == surface[y * width:(y + 1) * width]
            for y in range(height)
        ):
            return base, stride
        position = found + 1
    raise QuestSyncError("Не удалось найти упакованную сетку ландшафта в SAV.")


def locate_objects_ugs(
    explicit: Path | None,
    source_sav: Path,
    original_dtm: Path,
    modified_dtm: Path,
) -> Path:
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise QuestSyncError(f"Файл Objects.ugs не найден: {path}")
        return path

    candidates: list[Path] = []
    for source in (original_dtm, modified_dtm, source_sav):
        for parent in (source.parent, *source.parents):
            candidates.extend(
                (
                    parent / "Graphics" / "Objects" / "Objects.ugs",
                    parent / "Objects" / "Objects.ugs",
                    parent / "Objects.ugs",
                )
            )
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            key = candidate.resolve()
        except OSError:
            key = candidate
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    raise QuestSyncError(
        "Для добавления декораций нужен Graphics\\Objects\\Objects.ugs. "
        "Файл не найден рядом с выбранной картой или сохранением."
    )


def parse_objects_ugs(path: Path) -> dict[int, DecorationAtlasRecord]:
    data = Path(path).read_bytes()
    records: dict[int, DecorationAtlasRecord] = {}
    offset = 0
    skipped = 0
    while offset + 28 <= len(data):
        kind, variant, texture_width, frame_width, frame_height = struct.unpack_from(
            "<5I", data, offset
        )
        parsed: tuple[int, int, int, int] | None = None
        for header_size in (24, 28):
            dimensions_offset = offset + header_size
            if dimensions_offset + 4 > len(data):
                continue
            width, height = struct.unpack_from("<HH", data, dimensions_offset)
            end = dimensions_offset + 4 + width * height * 2
            if (
                0 < width <= 1024
                and 0 < height <= 1024
                and end <= len(data)
                and frame_width == width
                and frame_height == height
                and texture_width <= 4096
                and kind <= 255
            ):
                parsed = (header_size, width, height, end)
                break
        if parsed is None:
            offset += 1
            skipped += 1
            if skipped > 1 << 20:
                break
            continue
        skipped = 0
        header_size, width, height, end = parsed
        cookie = struct.unpack_from("<I", data, offset + 20)[0]
        if header_size == 24:
            color_b, color_g, color_r, alpha = cookie.to_bytes(4, "little")
            object_id = (kind << 8) | variant
            records[object_id] = DecorationAtlasRecord(
                object_id=object_id,
                kind=kind,
                sprite_width=width,
                sprite_height=height,
                color_r=color_r,
                color_g=color_g,
                color_b=color_b,
                alpha=alpha,
            )
        offset = end
    if len(records) < 200:
        raise QuestSyncError(
            f"Objects.ugs распознан не полностью: найдено только {len(records)} объектов."
        )
    return records


def _decoration_cells(item: DecorationAddition) -> set[tuple[int, int]]:
    footprint_width, footprint_height = item.atlas.footprint
    return {
        (item.x - dx, item.y - dy)
        for dy in range(footprint_height)
        for dx in range(footprint_width)
    }


def _parse_decorations(
    inner: bytes,
    section: dict[str, int],
    atlas: dict[int, DecorationAtlasRecord],
    width: int,
    height: int,
) -> list[DecorationAddition]:
    result: list[DecorationAddition] = []
    for index in range(section["count"]):
        x, y, object_id = struct.unpack(
            "<HHH", _record(inner, section, index, 6)
        )
        item = atlas.get(object_id)
        if item is None or item.kind not in (1, 2, 3, 4, 5, 6, 8, 9, 10, 11):
            raise QuestSyncError(
                f"Декорация №{index + 1} имеет неизвестный объект Objects.ugs: {object_id}."
            )
        result.append(DecorationAddition(index, x, y, object_id, item))
    return result


def _validate_changed_decoration(
    item: DecorationAddition,
    width: int,
    height: int,
) -> None:
    footprint_width, footprint_height = item.atlas.footprint
    if not (
        footprint_width - 1 <= item.x < width
        and footprint_height - 1 <= item.y < height
    ):
        raise QuestSyncError(
            f"Изменяемая декорация №{item.index + 1} с областью "
            f"{footprint_width}×{footprint_height} выходит за карту: "
            f"({item.x}, {item.y})."
        )
    table_x = item.x if item.atlas.is_tree else item.x - 1
    if not 0 <= table_x < width:
        raise QuestSyncError(
            f"Служебная клетка декорации №{item.index + 1} выходит за карту."
        )


def prepare_decoration_changes(
    original: bytes,
    modified: bytes,
    original_sections: dict[str, dict[str, int]],
    modified_sections: dict[str, dict[str, int]],
    atlas: dict[int, DecorationAtlasRecord],
    width: int,
    height: int,
) -> tuple[
    list[DecorationAddition],
    list[DecorationAddition],
    set[tuple[int, int]],
    list[DecorationAddition],
    list[DecorationAddition],
]:
    old = _parse_decorations(
        original, original_sections["decorations"], atlas, width, height
    )
    new = _parse_decorations(
        modified, modified_sections["decorations"], atlas, width, height
    )
    old_keys = [(item.x, item.y, item.object_id) for item in old]
    new_keys = [(item.x, item.y, item.object_id) for item in new]
    if old_keys == new_keys:
        return [], [], set(), [], []

    # A suffix append can be applied without reconstructing old cells.
    if len(new) >= len(old) and new_keys[:len(old)] == old_keys:
        additions = new[len(old):]
        occupied: set[tuple[int, int]] = set()
        for item in old:
            occupied.update(_decoration_cells(item))
        for item in additions:
            _validate_changed_decoration(item, width, height)
            cells = _decoration_cells(item)
            overlap = cells & occupied
            if overlap:
                sample_x, sample_y = min(overlap)
                raise QuestSyncError(
                    f"Область новой декорации №{item.index + 1} пересекается с другой "
                    f"декорацией в клетке ({sample_x}, {sample_y}). Наложения при "
                    "обычном добавлении пока не поддерживаются."
                )
            occupied.update(cells)
        return additions, [], set(), [], []

    old_counter = Counter(old_keys)
    new_counter = Counter(new_keys)
    if old_counter == new_counter:
        raise QuestSyncError(
            "Изменён только порядок декораций. Такое изменение пока не поддерживается."
        )

    removed_needed = old_counter - new_counter
    added_needed = new_counter - old_counter
    removals: list[DecorationAddition] = []
    additions: list[DecorationAddition] = []
    for item, key in zip(old, old_keys):
        if removed_needed[key]:
            removals.append(item)
            removed_needed[key] -= 1
    for item, key in zip(new, new_keys):
        if added_needed[key]:
            additions.append(item)
            added_needed[key] -= 1

    for item in removals + additions:
        _validate_changed_decoration(item, width, height)

    affected: set[tuple[int, int]] = set()
    for item in removals + additions:
        affected.update(_decoration_cells(item))

    changed = True
    while changed:
        changed = False
        for item in old + new:
            cells = _decoration_cells(item)
            if cells & affected and not cells <= affected:
                affected.update(cells)
                changed = True

    rebuild_old = [item for item in old if _decoration_cells(item) & affected]
    rebuild_new = [item for item in new if _decoration_cells(item) & affected]
    for item in rebuild_old + rebuild_new:
        _validate_changed_decoration(item, width, height)
    return additions, removals, affected, rebuild_old, rebuild_new


def _building_navigation_profile_key(
    record: building_runtime.BuildingRecord,
) -> tuple[int, int, int, int, int]:
    return (
        record.picture_variant,
        record.picture_number,
        record.building_type,
        record.size_x,
        record.size_y,
    )


def _building_grid_semantics_changed(old_raw: bytes, new_raw: bytes) -> bool:
    """Return whether a BuildingData edit can change compiled-grid occupancy.

    Navigation footprint is keyed by picture variant/number, building type and
    nominal size in addition to the DTm anchor coordinates.  Comparing only
    x/y and size_x/size_y leaves stale field+5/property cells when an editor
    swaps the building picture or type without moving/resizing it.
    """
    if len(old_raw) != BUILDING_SIZE or len(new_raw) != BUILDING_SIZE:
        raise ValueError("BuildingData record must be 358 bytes")
    if old_raw[:4] != new_raw[:4]:
        return True
    for off in (
        building_runtime.PICTURE_NUMBER_OFF,
        building_runtime.PICTURE_VARIANT_OFF,
        building_runtime.TYPE_OFF,
        building_runtime.SIZE_X_OFF,
        building_runtime.SIZE_Y_OFF,
    ):
        if old_raw[off] != new_raw[off]:
            return True
    return False


def _building_addition_report(
    target_index: int,
    raw: bytes,
) -> BuildingAddition:
    """Create the legacy GUI/report row from the active runtime path."""
    record = building_runtime.parse_building_record(raw)
    save_x, save_y = record.runtime_coords
    state_size = (
        building_runtime.RUNTIME_GARRISON_SIZE if record.runtime_garrison else 0
    )
    return BuildingAddition(
        index=target_index,
        building_type=record.building_type,
        picture_number=record.picture_number,
        picture_variant=record.picture_variant,
        raw_x=record.x,
        raw_y=record.y,
        save_x=save_x,
        save_y=save_y,
        coordinate_template_index=None,
        runtime_state_added=record.runtime_garrison,
        pointer_template_index=None,
        core=b"",
        state=b"",
        serialized_size=building_runtime.BUILDING_SIZE + state_size,
    )


def _building_additions_from_mapping(
    mapping: list[int | None],
    new_raw_records: list[bytes],
) -> list[BuildingAddition]:
    """Return report/GUI rows for structurally new target buildings."""
    return [
        _building_addition_report(target, new_raw_records[target])
        for target, source in enumerate(mapping)
        if source is None
    ]


def infer_building_navigation_profiles(
    payload: bytes,
    relationship: dict[str, Any],
    records: list[building_runtime.BuildingRecord],
    cell_table_base_u16: int,
    cell_table_stride: int,
    width: int,
    height: int,
) -> dict[tuple[int, int, int, int, int], set[tuple[int, int]]]:
    """Learn navigation cells that extend beyond BuildingData size_x/size_y.

    Native V.4 saves show that some building pictures have an extra navigation
    row not represented by ``size_y`` (Town pictures 1:0 and 1:1 on the
    supplied shoreline control).  Rather than hard-code a picture list, learn
    connected extra cells from the source SAV and reuse them for buildings with
    the same picture/type/size signature.  The ordinary rectangular footprint
    remains the fallback for unseen pictures.
    """
    grid = relationship["compiled_grid"]
    grid_offset = grid["offset"]
    grid_end = grid_offset + grid["cell_layers_size"]

    def nav_value(x: int, y: int) -> int:
        index = cell_table_base_u16 + 9 * (x + cell_table_stride * y) + 5
        off = grid_offset + index * 2
        if not grid_offset <= off <= grid_end - 2:
            return -1
        return struct.unpack_from("<H", payload, off)[0]

    profiles: dict[tuple[int, int, int, int, int], set[tuple[int, int]]] = {}
    for record in records:
        standard = set(record.footprint)
        anchor = record.x + cell_table_stride * record.y
        # Flood only outward from the normal rectangle.  Field +5 also stores
        # unrelated navigation metadata elsewhere on the map, so a global
        # search for the same numeric value would create false positives.
        frontier: list[tuple[int, int]] = []
        queued = set(standard)
        for x, y in standard:
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                cell = (x + dx, y + dy)
                if cell in queued:
                    continue
                if 0 <= cell[0] < width and 0 <= cell[1] < height:
                    queued.add(cell)
                    frontier.append(cell)
        extras: set[tuple[int, int]] = set()
        while frontier:
            x, y = frontier.pop()
            if nav_value(x, y) != anchor:
                continue
            extras.add((x - record.x, y - record.y))
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                cell = (x + dx, y + dy)
                if cell in queued:
                    continue
                if 0 <= cell[0] < width and 0 <= cell[1] < height:
                    queued.add(cell)
                    frontier.append(cell)
        key = _building_navigation_profile_key(record)
        # Presence of an empty set is meaningful: this exact signature was
        # observed in the source SAV and its native footprint was rectangular.
        # Missing keys are the only cases using the unseen-picture fallback.
        profiles.setdefault(key, set()).update(extras)
    return profiles


def building_navigation_footprint(
    record: building_runtime.BuildingRecord,
    profiles: dict[tuple[int, int, int, int, int], set[tuple[int, int]]],
) -> set[tuple[int, int]]:
    cells = set(record.footprint)
    for dx, dy in profiles.get(_building_navigation_profile_key(record), ()):
        cells.add((record.x + dx, record.y + dy))
    return cells


def validate_decoration_rebuild_safety(
    payload: bytes,
    relationship: dict[str, Any],
    cell_table_base_u16: int,
    cell_table_stride: int,
    cells: set[tuple[int, int]],
) -> None:
    if not cells:
        return
    grid = relationship["compiled_grid"]
    values = memoryview(payload)[
        grid["offset"]:grid["offset"] + grid["cell_layers_size"]
    ]
    for x, y in cells:
        cell = cell_table_base_u16 + 9 * (x + cell_table_stride * y)
        # Field 5 also contains ordinary navigation metadata on empty cells;
        # a non-zero value there alone does not mean that an object occupies it.
        for field in (1, 2, 3, 4, 6, 7):
            value = struct.unpack_from("<H", values, (cell + field) * 2)[0]
            if value:
                raise QuestSyncError(
                    f"Нельзя безопасно пересобрать декорации в клетке ({x}, {y}): "
                    "там находится строение, фонарь или другой служебный объект."
                )


def prepare_lantern_additions(
    original: bytes,
    modified: bytes,
    original_sections: dict[str, dict[str, int]],
    modified_sections: dict[str, dict[str, int]],
    width: int,
    height: int,
) -> tuple[
    list[LanternAddition],
    list[LanternAddition],
    bool,
    list[int],
    list[int],
]:
    def parse(inner: bytes, section: dict[str, int]) -> list[LanternAddition]:
        result = []
        ids: set[int] = set()
        coordinates: set[tuple[int, int]] = set()
        for index in range(section["count"]):
            record = _record(inner, section, index, LANTERN_SIZE)
            x, y = struct.unpack_from("<HH", record, 0)
            lantern_id = record[4]
            if not (0 <= x < width and 0 <= y < height):
                raise QuestSyncError(
                    f"Фонарь ID {lantern_id} находится вне карты: ({x}, {y})."
                )
            if lantern_id == 0 or lantern_id in ids:
                raise QuestSyncError(f"Некорректный или повторный ID фонаря: {lantern_id}.")
            if (x, y) in coordinates:
                raise QuestSyncError(f"Несколько фонарей находятся в клетке ({x}, {y}).")
            ids.add(lantern_id)
            coordinates.add((x, y))
            result.append(LanternAddition(index, x, y, lantern_id, record))
        return result

    old = parse(original, original_sections["lanterns"])
    new = parse(modified, modified_sections["lanterns"])
    old_by_id = {item.lantern_id: item for item in old}
    new_by_id = {item.lantern_id: item for item in new}
    additions = [item for item in new if item.lantern_id not in old_by_id]
    removed_ids = sorted(set(old_by_id) - set(new_by_id))
    changed_ids = sorted(
        lantern_id
        for lantern_id in set(old_by_id) & set(new_by_id)
        if old_by_id[lantern_id].record != new_by_id[lantern_id].record
    )
    old_blob = original[
        original_sections["lanterns"]["offset"]:original_sections["lanterns"]["end"]
    ]
    new_blob = modified[
        modified_sections["lanterns"]["offset"]:modified_sections["lanterns"]["end"]
    ]
    return additions, new, old_blob != new_blob, removed_ids, changed_ids


def prepare_building_additions(
    payload: bytes,
    original: bytes,
    modified: bytes,
    original_sections: dict[str, dict[str, int]],
    modified_sections: dict[str, dict[str, int]],
    relationship: dict[str, Any],
) -> list[BuildingAddition]:
    old_count = original_sections["buildings"]["count"]
    new_count = modified_sections["buildings"]["count"]
    if new_count == old_count:
        return []

    state_indices = set(
        relationship["buildings"]["runtime_garrison_building_indices"]
    )
    additions = []
    for index in range(old_count, new_count):
        raw = _building_record(modified, modified_sections["buildings"], index)
        building_type = raw[6]
        candidates = _coordinate_delta_candidates(
            raw,
            original,
            original_sections["buildings"],
            payload,
            relationship,
            old_count,
        )
        if candidates:
            template_index, dx, dy = _select_coordinate_template(candidates)
        else:
            # Confirmed by the all-types control save: runtime building anchors
            # are x+1 and y-(stored image height-1). This also covers a type
            # absent from the original map, where no record template exists.
            template_index = None
            dx, dy = -1, max(raw[290] - 1, 0)
        raw_x, raw_y = struct.unpack_from("<HH", raw, 0)
        if raw_x < dx or raw_y < dy:
            raise QuestSyncError(
                f"Координаты нового строения №{index + 1} нельзя преобразовать "
                "в runtime-формат SAV."
            )
        save_x, save_y = raw_x - dx, raw_y - dy
        core = bytearray(raw)
        struct.pack_into("<HH", core, 0, save_x, save_y)

        state = b""
        pointer_template: int | None = None
        if building_type in RUNTIME_GARRISON_TYPES:
            if any(raw[314:332]):
                raise QuestSyncError(
                    f"Новое строение №{index + 1} содержит гарнизон. "
                    "Автоматическая сборка заполненного runtime-гарнизона пока не поддерживается."
                )
            pointer_candidates = [
                candidate_index
                for candidate_index in state_indices
                if _building_record(
                    original, original_sections["buildings"], candidate_index
                )[6] == building_type
            ]
            if not pointer_candidates:
                raise QuestSyncError(
                    f"Для нового строения №{index + 1} (тип {building_type}) нет "
                    "образца служебного указателя в исходном SAV."
                )
            pointer_template = pointer_candidates[0]
            template_core = _runtime_building_record(
                payload, relationship, pointer_template
            )
            pointer = template_core[342:346]
            if pointer == b"\0\0\0\0":
                raise QuestSyncError("Образец служебного указателя оказался пустым.")
            core[342:346] = pointer
            state = _empty_runtime_garrison(raw[332])
        elif any(raw[314:332]):
            raise QuestSyncError(
                f"Новое строение №{index + 1} неизвестного гарнизонного типа "
                f"{building_type} содержит войска."
            )

        additions.append(
            BuildingAddition(
                index=index,
                building_type=building_type,
                picture_number=raw[4],
                picture_variant=raw[5],
                raw_x=raw_x,
                raw_y=raw_y,
                save_x=save_x,
                save_y=save_y,
                coordinate_template_index=template_index,
                runtime_state_added=bool(state),
                pointer_template_index=pointer_template,
                core=bytes(core),
                state=state,
            )
        )
    return additions


def _army_records(inner: bytes, section: dict[str, int]) -> list[bytes]:
    return [
        _record(inner, section, index, ARMY_SIZE)
        for index in range(section["count"])
    ]


def prepare_army_changes(
    original: bytes,
    modified: bytes,
    original_sections: dict[str, dict[str, int]],
    modified_sections: dict[str, dict[str, int]],
    original_strings: list[bytes],
    modified_strings: list[bytes],
    building_count: int,
    modified_building_count: int,
) -> list[ArmyChange]:
    old_records = _army_records(original, original_sections["armies"])
    new_records = _army_records(modified, modified_sections["armies"])
    mapping, deleted = army_runtime.align_armies(old_records, new_records)
    old_text_start = army_text_base(building_count, save=False)
    new_text_start = army_text_base(modified_building_count, save=False)
    changes: list[ArmyChange] = []

    mapped_sources = {i for i in mapping if i is not None}
    for ti, si in enumerate(mapping):
        new_record = new_records[ti]
        new_texts = modified_strings[new_text_start + ti * 3:new_text_start + (ti + 1) * 3]
        if si is None:
            new_x, new_y = struct.unpack_from("<HH", new_record, 0)
            changes.append(ArmyChange(
                index=ti, army_id=ti + 1, binary_changed=True, text_changed=any(new_texts),
                old_x=None, old_y=None, new_x=new_x, new_y=new_y, original_title="",
                modified_title=new_texts[0].decode("cp1251", "replace") if new_texts else "",
                change_summary="новая армия", binary_change_ranges=("новая 89-байтовая запись армии",),
                text_changes=describe_event_text_changes((b"", b"", b""), new_texts),
                action="add", source_index=None, target_index=ti,
            ))
            continue
        old_record = old_records[si]
        old_texts = original_strings[old_text_start + si * 3:old_text_start + (si + 1) * 3]
        binary_ranges = describe_binary_changes(old_record, new_record)
        text_changes = describe_event_text_changes(old_texts, new_texts)
        # A pure ID renumber caused by a deletion is structural, not a user edit.
        meaningful_binary = tuple(r for r in binary_ranges if not (r == "0x04" or r == "0x04-0x04"))
        if not meaningful_binary and not text_changes and si == ti:
            continue
        old_x, old_y = struct.unpack_from("<HH", old_record, 0)
        new_x, new_y = struct.unpack_from("<HH", new_record, 0)
        changes.append(ArmyChange(
            index=ti, army_id=ti + 1, binary_changed=bool(meaningful_binary), text_changed=bool(text_changes),
            old_x=old_x, old_y=old_y, new_x=new_x, new_y=new_y,
            original_title=old_texts[0].decode("cp1251", "replace") if old_texts else "",
            modified_title=new_texts[0].decode("cp1251", "replace") if new_texts else "",
            change_summary=summarize_event_changes(meaningful_binary, text_changes),
            binary_change_ranges=meaningful_binary, text_changes=text_changes, action="modify",
            source_index=si, target_index=ti,
        ))

    for si in range(len(old_records)):
        if si in mapped_sources:
            continue
        old_record=old_records[si]
        old_texts=original_strings[old_text_start + si*3:old_text_start + (si+1)*3]
        old_x,old_y=struct.unpack_from("<HH",old_record,0)
        changes.append(ArmyChange(
            index=si, army_id=si+1, binary_changed=True, text_changed=any(old_texts),
            old_x=old_x, old_y=old_y, new_x=None, new_y=None,
            original_title=old_texts[0].decode("cp1251","replace") if old_texts else "", modified_title="",
            change_summary="армия удалена", binary_change_ranges=("89-байтовая запись армии удалена",),
            text_changes=describe_event_text_changes(old_texts,(b"",b"",b"")), action="delete",
            source_index=si, target_index=None,
        ))
    changes.sort(key=lambda x: (x.target_index if x.target_index is not None else 10**6 + (x.source_index or 0), x.action))
    return changes


def validate_compatible_maps(
    original: bytes,
    modified: bytes,
    original_sections: dict[str, dict[str, int]],
    modified_sections: dict[str, dict[str, int]],
) -> None:
    ow, oh = struct.unpack_from("<II", original, 0x0C)
    mw, mh = struct.unpack_from("<II", modified, 0x0C)
    if (ow, oh) != (mw, mh):
        raise QuestSyncError(
            f"Размер карты изменён: было {ow}×{oh}, стало {mw}×{mh}. "
            "Эта версия импортёра не пересобирает клеточные runtime-слои."
        )
    old_armies = original_sections["armies"]["count"]
    new_armies = modified_sections["armies"]["count"]
    if new_armies > 254:
        raise QuestSyncError("Количество армий больше 254 не поддерживается форматом игры.")

    old_buildings = original_sections["buildings"]["count"]
    new_buildings = modified_sections["buildings"]["count"]
    if new_buildings > 255:
        raise QuestSyncError("Количество строений больше 255 не поддерживается форматом игры.")

    new_lanterns = modified_sections["lanterns"]["count"]
    if new_lanterns > 255:
        raise QuestSyncError("Количество фонарей больше 255 не поддерживается форматом игры.")

    old_events = original_sections["events"]["count"]
    new_events = modified_sections["events"]["count"]
    if new_events < old_events:
        raise QuestSyncError(
            f"Удалены события: было {old_events}, стало {new_events}. "
            "Можно изменять существующие события или добавлять новые в конец списка."
        )
    for index in range(old_events, new_events):
        record = _record(modified, modified_sections["events"], index, EVENT_SIZE)
        if struct.unpack_from("<H", record, 164)[0]:
            raise QuestSyncError(
                f"Новое событие №{index + 1} содержит встроенную картинку. "
                "Перенос картинок событий в SAV пока не поддерживается."
            )


def analyze_paths(
    source_sav: Path,
    original_dtm: Path,
    modified_dtm: Path,
    objects_ugs: Path | None = None,
) -> SyncPlan:
    source_sav = Path(source_sav)
    original_dtm = Path(original_dtm)
    modified_dtm = Path(modified_dtm)
    save_raw = sav_tool.read_bounded(source_sav)
    payload, container_info = sav_tool.unpack_sav_bytes(save_raw)
    original = sav_tool.read_dtm(original_dtm)
    modified = sav_tool.read_dtm(modified_dtm)
    return analyze_data(source_sav, original_dtm, modified_dtm, save_raw, original, modified, objects_ugs)


def analyze_data(source_sav, original_dtm, modified_dtm, save_raw, original, modified, objects_ugs=None):
    """Analyze an immutable input snapshot, including a filtered in-memory DTm."""
    payload, container_info = sav_tool.unpack_sav_bytes(save_raw)
    original_sections, _ = sav_tool.dtm_layout(original)
    modified_sections, _ = sav_tool.dtm_layout(modified)
    validate_compatible_maps(original, modified, original_sections, modified_sections)

    relation = sav_tool.relate(payload, original, container_info)
    relationship = relation["relationship"]
    if not relationship["map_prefix_matches"]:
        raise QuestSyncError(
            "Исходная DTm не соответствует сохранению: первые 289 байт карты различаются."
        )

    event_start = relationship["events"]["offset"]
    event_count = original_sections["events"]["count"]
    modified_event_count = modified_sections["events"]["count"]
    text_start = relationship["texts"]["offset"]
    text_end = relationship["texts"]["end"]
    building_count = original_sections["buildings"]["count"]
    modified_building_count = modified_sections["buildings"]["count"]
    army_count = original_sections["armies"]["count"]
    modified_army_count = modified_sections["armies"]["count"]
    lantern_count = original_sections["lanterns"]["count"]
    modified_lantern_count = modified_sections["lanterns"]["count"]
    decoration_count = original_sections["decorations"]["count"]
    modified_decoration_count = modified_sections["decorations"]["count"]
    width, height = struct.unpack_from("<II", original, 0x0C)
    map_header_offset = relation["save_metadata"]["map_header_offset"]

    original_strings, _ = sav_tool.parse_texts_from_map(original, original_sections)
    modified_strings, _ = sav_tool.parse_texts_from_map(modified, modified_sections)
    expected_save_strings = len(original_strings) - 2
    save_strings, parsed_text_end = sav_tool.read_cstrings_raw(
        payload, text_start, expected_save_strings
    )
    if parsed_text_end != text_end:
        raise QuestSyncError("Не удалось однозначно определить конец текстовой секции SAV.")

    original_aligned = original_strings[:2] + original_strings[4:]
    hero_replacement = infer_hero_replacement(original_aligned, save_strings)
    army_changes = prepare_army_changes(
        original,
        modified,
        original_sections,
        modified_sections,
        original_strings,
        modified_strings,
        building_count,
        modified_building_count,
    )
    original_event_text = event_text_base(building_count, army_count, save=False)
    modified_event_text = event_text_base(modified_building_count, modified_army_count, save=False)
    sav_event_text = event_text_base(building_count, army_count, save=True)
    original_event_offset = original_sections["events"]["offset"]
    modified_event_offset = modified_sections["events"]["offset"]

    decisions = []
    for index in range(event_count):
        original_record = original[
            original_event_offset + index * EVENT_SIZE:
            original_event_offset + (index + 1) * EVENT_SIZE
        ]
        modified_record = modified[
            modified_event_offset + index * EVENT_SIZE:
            modified_event_offset + (index + 1) * EVENT_SIZE
        ]
        saved_record = payload[
            event_start + index * EVENT_SIZE:event_start + (index + 1) * EVENT_SIZE
        ]
        old_texts = original_strings[
            original_event_text + index * 3:original_event_text + (index + 1) * 3
        ]
        new_texts = modified_strings[
            modified_event_text + index * 3:modified_event_text + (index + 1) * 3
        ]
        progressed = saved_record != original_record
        binary_changed = modified_record != original_record
        text_changed = any(
            not event_texts_equal(old, new)
            for old, new in zip(old_texts, new_texts)
        )
        save_change_ranges = describe_binary_changes(original_record, saved_record)
        binary_change_ranges = describe_binary_changes(original_record, modified_record)
        text_changes = describe_event_text_changes(old_texts, new_texts)
        decisions.append(
            EventDecision(
                index=index,
                event_id=index + 1,
                progressed=progressed,
                binary_changed_in_modified_map=binary_changed,
                text_changed_in_modified_map=text_changed,
                selected_for_import=(not progressed and (binary_changed or text_changed)),
                save_vs_original_changed_bytes=hamming(saved_record, original_record),
                modified_vs_original_changed_bytes=hamming(modified_record, original_record),
                original_title=old_texts[0].decode("cp1251", "replace"),
                modified_title=new_texts[0].decode("cp1251", "replace"),
                change_summary=summarize_event_changes(binary_change_ranges, text_changes),
                save_change_ranges=save_change_ranges,
                binary_change_ranges=binary_change_ranges,
                text_changes=text_changes,
            )
        )

    for index in range(event_count, modified_event_count):
        modified_record = modified[
            modified_event_offset + index * EVENT_SIZE:
            modified_event_offset + (index + 1) * EVENT_SIZE
        ]
        new_texts = modified_strings[
            modified_event_text + index * 3:modified_event_text + (index + 1) * 3
        ]
        text_changes = describe_event_text_changes((b"", b"", b""), new_texts)
        decisions.append(
            EventDecision(
                index=index,
                event_id=index + 1,
                progressed=False,
                binary_changed_in_modified_map=True,
                text_changed_in_modified_map=any(new_texts),
                selected_for_import=True,
                save_vs_original_changed_bytes=0,
                modified_vs_original_changed_bytes=sum(value != 0 for value in modified_record),
                original_title="",
                modified_title=new_texts[0].decode("cp1251", "replace"),
                change_summary=summarize_event_changes((), text_changes, new_event=True),
                save_change_ranges=(),
                binary_change_ranges=("новая 171-байтовая запись события",),
                text_changes=text_changes,
            )
        )

    # Guard against an unexpected shift in string indexing.
    if sav_event_text + event_count * 3 > len(save_strings):
        raise QuestSyncError("Текстовая секция SAV короче ожидаемой.")

    old_building_records = [
        _building_record(original, original_sections["buildings"], index)
        for index in range(building_count)
    ]
    new_building_records = [
        _building_record(modified, modified_sections["buildings"], index)
        for index in range(modified_building_count)
    ]
    building_mapping, building_deleted_indices = building_runtime.align_buildings(
        old_building_records, new_building_records
    )
    building_structure_changed = building_runtime.structure_changed(
        building_mapping, building_deleted_indices, building_count, modified_building_count
    )
    building_binary_changed = (
        old_building_records != new_building_records
        or building_structure_changed
    )
    building_grid_semantics_changed = building_structure_changed or any(
        source is None
        or _building_grid_semantics_changed(
            old_building_records[source], new_building_records[target]
        )
        for target, source in enumerate(building_mapping)
    )
    # Keep the legacy GUI/report field populated from the active structural
    # mapping.  Runtime construction itself is handled later by
    # building_runtime.build_runtime_blob().
    building_additions = _building_additions_from_mapping(
        building_mapping, new_building_records
    )
    (
        lantern_additions,
        lantern_records,
        lantern_rewrite,
        lantern_removed_ids,
        lantern_changed_ids,
    ) = prepare_lantern_additions(
        original,
        modified,
        original_sections,
        modified_sections,
        width,
        height,
    )
    cell_table_base_u16, cell_table_stride = infer_cell_table(
        payload, original, original_sections, relationship
    )
    parsed_old_buildings = [
        building_runtime.parse_building_record(raw) for raw in old_building_records
    ]
    parsed_new_buildings = [
        building_runtime.parse_building_record(raw) for raw in new_building_records
    ]
    observed_navigation_profiles = infer_building_navigation_profiles(
        payload,
        relationship,
        parsed_old_buildings,
        cell_table_base_u16,
        cell_table_stride,
        width,
        height,
    )
    building_navigation_unseen_profile_ids = [
        index + 1
        for index, record in enumerate(parsed_new_buildings)
        if _building_navigation_profile_key(record) not in observed_navigation_profiles
    ]
    source_building_types = {record.building_type for record in parsed_old_buildings}
    building_runtime_template_missing_ids = [
        target + 1
        for target, source in enumerate(building_mapping)
        if source is None
        and parsed_new_buildings[target].building_type not in source_building_types
    ]
    cell_layer_u16_count = relationship["compiled_grid"]["cell_layers_size"] // 2
    (
        property_ring_size_u16,
        property_rotation_u16,
        visual_grid_base_u16,
        visual_grid_stride,
    ) = infer_decoration_grid_layout(
        cell_table_base_u16,
        width,
        height,
        cell_layer_u16_count,
    )
    original_surface = decode_surface(original, original_sections["surface"], width * height)
    modified_surface = decode_surface(modified, modified_sections["surface"], width * height)
    terrain_changes = [
        TerrainChange(index % width, index // width, old_tile, new_tile)
        for index, (old_tile, new_tile) in enumerate(zip(original_surface, modified_surface))
        if old_tile != new_tile
    ]
    if any(item.new_tile not in VISUAL_TERRAIN_BASE for item in terrain_changes):
        raise QuestSyncError("Ландшафт содержит неизвестный тип клетки.")
    terrain_grid_base_byte, terrain_grid_stride_bytes = infer_terrain_grid_layout(
        payload,
        original_surface,
        width,
        height,
        relationship,
        cell_table_base_u16,
    )

    resolved_objects_ugs: Path | None = None
    decoration_additions: list[DecorationAddition] = []
    decoration_removals: list[DecorationAddition] = []
    decoration_rebuild_cells: set[tuple[int, int]] = set()
    decoration_rebuild_original: list[DecorationAddition] = []
    decoration_rebuild_modified: list[DecorationAddition] = []
    terrain_reapply_decorations: list[DecorationAddition] = []
    original_decoration_bytes = original[
        original_sections["decorations"]["offset"]:
        original_sections["decorations"]["end"]
    ]
    modified_decoration_bytes = modified[
        modified_sections["decorations"]["offset"]:
        modified_sections["decorations"]["end"]
    ]
    decorations_changed = original_decoration_bytes != modified_decoration_bytes
    if decorations_changed or terrain_changes or building_grid_semantics_changed:
        resolved_objects_ugs = locate_objects_ugs(
            objects_ugs,
            source_sav,
            original_dtm,
            modified_dtm,
        )
        atlas = parse_objects_ugs(resolved_objects_ugs)
    if decorations_changed:
        (
            decoration_additions,
            decoration_removals,
            decoration_rebuild_cells,
            decoration_rebuild_original,
            decoration_rebuild_modified,
        ) = prepare_decoration_changes(
            original,
            modified,
            original_sections,
            modified_sections,
            atlas,
            width,
            height,
        )
        validate_decoration_rebuild_safety(
            payload,
            relationship,
            cell_table_base_u16,
            cell_table_stride,
            decoration_rebuild_cells,
        )
    if terrain_changes:
        terrain_cells = {(item.x, item.y) for item in terrain_changes}
        # Terrain and the decoration list may now change in the same cells.
        # Cells that belong to decoration_rebuild_cells are rebuilt later as
        # one combined layer: modified terrain first, then modified
        # decorations in DTm order. Terrain-only cells keep the conservative
        # path below so unknown runtime navigation values are still preserved.
        validate_decoration_rebuild_safety(
            payload,
            relationship,
            cell_table_base_u16,
            cell_table_stride,
            terrain_cells,
        )
        modified_decorations = _parse_decorations(
            modified,
            modified_sections["decorations"],
            atlas,
            width,
            height,
        )
        added = Counter(
            (item.x, item.y, item.object_id) for item in decoration_additions
        )
        for item in modified_decorations:
            key = (item.x, item.y, item.object_id)
            if added[key]:
                added[key] -= 1
                continue
            if _decoration_cells(item) & terrain_cells:
                _validate_changed_decoration(item, width, height)
                terrain_reapply_decorations.append(item)

    for item in decisions:
        if not item.selected_for_import:
            continue
        record = modified[
            modified_event_offset + item.index * EVENT_SIZE:
            modified_event_offset + (item.index + 1) * EVENT_SIZE
        ]
        original_size = 0
        if item.index < event_count:
            original_record = original[
                original_sections["events"]["offset"] + item.index * EVENT_SIZE:
                original_sections["events"]["offset"] + (item.index + 1) * EVENT_SIZE
            ]
            original_size = struct.unpack_from("<H", original_record, 164)[0]
        if struct.unpack_from("<H", record, 164)[0] != original_size:
            raise QuestSyncError(
                f"Событие №{item.index + 1} изменяет встроенную картинку. "
                "Перенос картинок событий в SAV пока не поддерживается."
            )

    return SyncPlan(
        source_sav=source_sav,
        original_dtm=original_dtm,
        modified_dtm=modified_dtm,
        save_raw=save_raw,
        payload=payload,
        container_info=container_info,
        original_inner=original,
        modified_inner=modified,
        relationship=relationship,
        original_sections=original_sections,
        modified_sections=modified_sections,
        original_strings=original_strings,
        modified_strings=modified_strings,
        save_strings=save_strings,
        text_start=text_start,
        text_end=text_end,
        map_header_offset=map_header_offset,
        event_start=event_start,
        event_count=event_count,
        modified_event_count=modified_event_count,
        building_count=building_count,
        modified_building_count=modified_building_count,
        building_mapping=building_mapping,
        building_deleted_indices=building_deleted_indices,
        building_structure_changed=building_structure_changed,
        building_binary_changed=building_binary_changed,
        army_count=army_count,
        modified_army_count=modified_army_count,
        army_changes=army_changes,
        lantern_count=lantern_count,
        modified_lantern_count=modified_lantern_count,
        width=width,
        height=height,
        cell_table_base_u16=cell_table_base_u16,
        cell_table_stride=cell_table_stride,
        property_ring_size_u16=property_ring_size_u16,
        property_rotation_u16=property_rotation_u16,
        visual_grid_base_u16=visual_grid_base_u16,
        visual_grid_stride=visual_grid_stride,
        terrain_grid_base_byte=terrain_grid_base_byte,
        terrain_grid_stride_bytes=terrain_grid_stride_bytes,
        objects_ugs=resolved_objects_ugs,
        hero_replacement=hero_replacement,
        building_additions=building_additions,
        building_navigation_unseen_profile_ids=building_navigation_unseen_profile_ids,
        building_runtime_template_missing_ids=building_runtime_template_missing_ids,
        lantern_additions=lantern_additions,
        lantern_records=lantern_records,
        lantern_rewrite=lantern_rewrite,
        lantern_removed_ids=lantern_removed_ids,
        lantern_changed_ids=lantern_changed_ids,
        decoration_additions=decoration_additions,
        decoration_removals=decoration_removals,
        decoration_rebuild_cells=decoration_rebuild_cells,
        decoration_rebuild_original=decoration_rebuild_original,
        decoration_rebuild_modified=decoration_rebuild_modified,
        terrain_changes=terrain_changes,
        terrain_reapply_decorations=terrain_reapply_decorations,
        decisions=decisions,
    )


def plan_summary(plan: SyncPlan, include_events: bool = True) -> dict[str, Any]:
    changed_in_modified = [
        item for item in plan.decisions
        if item.binary_changed_in_modified_map or item.text_changed_in_modified_map
    ]
    value: dict[str, Any] = {
        "source_sav": str(plan.source_sav),
        "original_dtm": str(plan.original_dtm),
        "modified_dtm": str(plan.modified_dtm),
        "source_event_count": plan.event_count,
        "modified_event_count": plan.modified_event_count,
        "appended_event_count": plan.modified_event_count - plan.event_count,
        "progressed_or_touched_event_count": len(plan.progressed),
        "changed_event_count_in_modified_map": len(changed_in_modified),
        "future_changed_event_count_selected_for_import": len(plan.selected),
        "changed_progressed_event_count_preserved": len(plan.modified_progressed),
        "selected_event_ids": [item.event_id for item in plan.selected],
        "preserved_progressed_event_ids": [item.event_id for item in plan.progressed],
        "source_building_count": plan.building_count,
        "modified_building_count": plan.modified_building_count,
        "building_structure_changed": plan.building_structure_changed,
        "building_binary_changed": plan.building_binary_changed,
        "building_mapping_target_to_source": [
            (source + 1) if source is not None else None
            for source in plan.building_mapping
        ],
        "added_building_ids": [
            index + 1 for index, source in enumerate(plan.building_mapping)
            if source is None
        ],
        "deleted_source_building_ids": [index + 1 for index in plan.building_deleted_indices],
        # Backward-compatible aliases retained for older GUI/report readers.
        "appended_building_count": len(plan.building_additions),
        "appended_buildings": [item.as_dict() for item in plan.building_additions],
        "building_navigation_unseen_profile_ids": list(
            plan.building_navigation_unseen_profile_ids
        ),
        "building_runtime_template_missing_ids": list(
            plan.building_runtime_template_missing_ids
        ),
        "source_army_count": plan.army_count,
        "modified_army_count": plan.modified_army_count,
        "changed_army_count": len(plan.army_changes),
        "binary_changed_army_count": sum(item.binary_changed for item in plan.army_changes),
        "text_changed_army_count": sum(item.text_changed for item in plan.army_changes),
        "army_changes": [item.as_dict() for item in plan.army_changes],
        "source_lantern_count": plan.lantern_count,
        "modified_lantern_count": plan.modified_lantern_count,
        "appended_lantern_count": len(plan.lantern_additions),
        "appended_lanterns": [item.as_dict() for item in plan.lantern_additions],
        "removed_lantern_ids": plan.lantern_removed_ids,
        "changed_lantern_ids": plan.lantern_changed_ids,
        "appended_decoration_count": len(plan.decoration_additions),
        "appended_decorations": [
            item.as_dict() for item in plan.decoration_additions
        ],
        "removed_decoration_count": len(plan.decoration_removals),
        "removed_decorations": [
            item.as_dict() for item in plan.decoration_removals
        ],
        "decoration_rebuild_cell_count": len(plan.decoration_rebuild_cells),
        "terrain_change_count": len(plan.terrain_changes),
        "terrain_changes": [item.as_dict() for item in plan.terrain_changes],
        "objects_ugs": str(plan.objects_ugs) if plan.objects_ugs is not None else None,
        "cell_table": {
            "base_u16": plan.cell_table_base_u16,
            "row_stride": plan.cell_table_stride,
            "fields_per_cell": 9,
        },
        "decoration_grids": {
            "property_ring_size_u16": plan.property_ring_size_u16,
            "property_rotation_u16": plan.property_rotation_u16,
            "visual_base_u16": plan.visual_grid_base_u16,
            "visual_row_stride": plan.visual_grid_stride,
            "visual_blend": "experimental Objects.ugs representative BGRA",
        },
        "terrain_grid": {
            "base_byte": plan.terrain_grid_base_byte,
            "row_stride_bytes": plan.terrain_grid_stride_bytes,
        },
        "hero_placeholder_replacement": (
            plan.hero_replacement.decode("cp1251", "replace")
            if plan.hero_replacement is not None else None
        ),
        "unchanged_regions": [
            "save metadata",
            "terrain and decoration layers outside rebuilt cells",
            "mapped building runtime/progress state is preserved by three-way merge",
            "unchanged and progressed army runtime fields are preserved conservatively",
            "opaque per-slot army AI/header state is preserved",
            "existing lanterns outside requested changes",
            "unrelated pre-text state and dynamic runtime data",
        ],
    }
    active_catalog = army_runtime.load_catalog()
    if isinstance(active_catalog.get("_game_data"), dict):
        value["game_data"] = active_catalog["_game_data"]
        value["game_data_profile_compat"] = active_catalog.get("_compat", {})
    if include_events:
        value["events"] = [item.as_dict() for item in plan.decisions]
    return value


def build_synced_payload(plan: SyncPlan) -> tuple[bytes, dict[str, Any]]:
    payload = bytearray(plan.payload)
    property_a_base = dict(PROPERTY_A_TERRAIN_BASE)
    property_b_base = dict(PROPERTY_B_TERRAIN_BASE)
    property_c_base = PROPERTY_C_TERRAIN_BASE
    if (plan.width, plan.height) == (50, 50):
        # Native compact-map layers: rough land uses the full walking cost
        # in A and is impassable in B. C keeps the unscaled terrain cost.
        for tile in (8, 10, 11, 12, 13, 14):
            property_a_base[tile] = PROPERTY_B_TERRAIN_BASE[tile]
            property_b_base[tile] = 0

    old_building_records_raw = [
        _building_record(plan.original_inner, plan.original_sections["buildings"], i)
        for i in range(plan.building_count)
    ]
    new_building_records_raw = [
        _building_record(plan.modified_inner, plan.modified_sections["buildings"], i)
        for i in range(plan.modified_building_count)
    ]
    old_building_records = [building_runtime.parse_building_record(x) for x in old_building_records_raw]
    new_building_records = [building_runtime.parse_building_record(x) for x in new_building_records_raw]
    building_mapping = plan.building_mapping
    building_deleted_indices = plan.building_deleted_indices
    old_to_new_building = {
        source_index + 1: target_index + 1
        for target_index, source_index in enumerate(building_mapping)
        if source_index is not None
    }
    deleted_building_ids = {index + 1 for index in building_deleted_indices}
    building_grid_changed = plan.building_structure_changed or any(
        source is None
        or _building_grid_semantics_changed(
            old_building_records[source].raw, new_building_records[target].raw
        )
        for target, source in enumerate(building_mapping)
    )

    old_army_records_raw = _army_records(plan.original_inner, plan.original_sections["armies"])
    new_army_records_raw = _army_records(plan.modified_inner, plan.modified_sections["armies"])
    army_mapping, army_deleted_indices = army_runtime.align_armies(
        old_army_records_raw, new_army_records_raw
    )
    old_army_records = [army_runtime.parse_army_record(value) for value in old_army_records_raw]
    new_army_records = [army_runtime.parse_army_record(value) for value in new_army_records_raw]
    old_to_new_army = {
        source_index + 1: target_index + 1
        for target_index, source_index in enumerate(army_mapping)
        if source_index is not None
    }
    deleted_army_ids = {index + 1 for index in army_deleted_indices}
    has_army_structure_change = army_runtime.army_structure_changed(
        army_mapping, army_deleted_indices, plan.army_count, plan.modified_army_count
    )
    has_army_binary_change = any(item.binary_changed for item in plan.army_changes)
    modified_event_offset = plan.modified_sections["events"]["offset"]
    original_army_text = army_text_base(plan.building_count, save=False)
    modified_army_text = army_text_base(plan.modified_building_count, save=False)
    save_army_text = army_text_base(plan.modified_building_count, save=True)
    original_event_text = event_text_base(
        plan.building_count, plan.army_count, save=False
    )
    modified_event_text = event_text_base(
        plan.modified_building_count, plan.modified_army_count, save=False
    )

    has_structural_changes = bool(
        plan.building_binary_changed
        or plan.lantern_rewrite
        or plan.decoration_additions
        or plan.decoration_removals
        or plan.terrain_changes
        or plan.modified_event_count != plan.event_count
        or has_army_structure_change
        or has_army_binary_change
    )
    if has_structural_changes:
        # The save stores the same DTm text offset and section-size table in its
        # embedded 289-byte map prefix. Patch only fields affected by appends;
        # all unrelated header and runtime bytes remain from the source SAV.
        struct.pack_into(
            "<I", payload, plan.map_header_offset + 0x18,
            sav_tool.u32(plan.modified_inner, 0x18),
        )
        struct.pack_into(
            "<I", payload, plan.map_header_offset + 0x1C,
            plan.modified_sections["surface"]["size"],
        )
        struct.pack_into(
            "<I", payload, plan.map_header_offset + 0x20,
            plan.modified_sections["decorations"]["size"],
        )
        struct.pack_into(
            "<I", payload, plan.map_header_offset + 0x24,
            plan.modified_sections["buildings"]["size"],
        )
        struct.pack_into(
            "<I", payload, plan.map_header_offset + 0x28,
            plan.modified_sections["armies"]["size"],
        )
        struct.pack_into(
            "<I", payload, plan.map_header_offset + 0x2C,
            plan.modified_sections["lanterns"]["size"],
        )
        struct.pack_into(
            "<I", payload, plan.map_header_offset + 0x30,
            plan.modified_sections["events"]["size"],
        )
        struct.pack_into(
            "<4I",
            payload,
            plan.relationship["count_signature_offset"],
            plan.modified_building_count,
            plan.modified_army_count,
            plan.modified_lantern_count,
            plan.modified_event_count,
        )
        # Hero start-building IDs live in the embedded SettingsData prefix.
        # They are map semantics, not gameplay progress, so target DTm wins.
        for off in building_runtime.HERO_START_BUILDING_OFFSETS:
            payload[plan.map_header_offset + off] = plan.modified_inner[off]

    # Victory/defeat event references are explicit editor settings, not live
    # event state. They must follow the effective event namespace.
    for off in (210, 216):
        if plan.original_inner[off:off+2] != plan.modified_inner[off:off+2]:
            payload[plan.map_header_offset+off:plan.map_header_offset+off+2] = plan.modified_inner[off:off+2]

    grid_offset = plan.relationship["compiled_grid"]["offset"]
    grid_end = grid_offset + plan.relationship["compiled_grid"]["cell_layers_size"]
    surface = decode_surface(
        plan.modified_inner,
        plan.modified_sections["surface"],
        plan.width * plan.height,
    )

    def grid_u16_offset(index: int) -> int:
        byte_offset = grid_offset + index * 2
        if not grid_offset <= byte_offset <= grid_end - 2:
            raise QuestSyncError("Индекс выходит за клеточную область SAV.")
        return byte_offset

    def grid_u16(index: int) -> int:
        return struct.unpack_from("<H", payload, grid_u16_offset(index))[0]

    def set_grid_u16(index: int, value: int) -> None:
        struct.pack_into("<H", payload, grid_u16_offset(index), value)

    def property_index(grid_number: int, x: int, y: int) -> int:
        if (plan.width, plan.height) == (50, 50):
            return terrain_property_index(grid_number, x, y)
        return (
            grid_number * plan.property_ring_size_u16
            + (
                plan.property_rotation_u16 + x + plan.width * y
            ) % plan.property_ring_size_u16
        )

    def terrain_property_index(grid_number: int, x: int, y: int) -> int:
        return (
            plan.property_rotation_u16
            + grid_number * plan.property_ring_size_u16
            + x
            + plan.width * y
        )

    def update_property(
        value: int,
        item: DecorationAtlasRecord,
        tile_id: int,
        grid_number: int,
    ) -> int:
        expected_values = (
            PROPERTY_A_BASE_VALUES
            if grid_number == 0
            else PROPERTY_B_BASE_VALUES
            if grid_number == 1
            else PROPERTY_C_BASE_VALUES
        ).get(tile_id, frozenset())
        if (plan.width, plan.height) == (50, 50):
            expected_values = frozenset(((property_a_base, property_b_base, property_c_base)[grid_number][tile_id],))
        if value not in expected_values:
            return value
        if not item.passable:
            return 0
        if item.is_tree:
            if value <= 2:
                return 0
            if item.kind == 10:
                return min(65535, value + (6 if value <= 8 else 30))
            return min(65535, value + (4 if value <= 8 else 20))
        if item.kind == 4:
            return min(65535, value + (3 if value <= 8 else 15))
        return min(65535, value + (2 if value <= 8 else 10))

    def blend_visual(value: int, item: DecorationAtlasRecord) -> int:
        base = (value >> 11 & 31, value >> 5 & 63, value & 31)
        if item.is_tree or item.kind == 8:
            source = (item.color_r >> 3, item.color_g >> 2, item.color_b >> 3)
            mixed = tuple(
                (left * (255 - item.alpha) + right * item.alpha + 127) // 255
                for left, right in zip(base, source)
            )
            return (mixed[0] << 11) | (mixed[1] << 5) | mixed[2]

        # Landscape cookies store a representative color already composited
        # over neutral gray, rather than an unpremultiplied source color.
        base_8 = (
            base[0] * 255 // 31,
            base[1] * 255 // 63,
            base[2] * 255 // 31,
        )
        colors = (item.color_r, item.color_g, item.color_b)

        def trunc_div(value: int, denominator: int) -> int:
            return value // denominator if value >= 0 else -((-value) // denominator)

        mixed_8 = tuple(
            max(
                0,
                min(
                    255,
                    colors[channel]
                    + trunc_div(
                        (base_8[channel] - 127) * (255 - item.alpha),
                        255,
                    ),
                ),
            )
            for channel in range(3)
        )
        mixed = (
            min(31, (mixed_8[0] * 31 + 127) // 255),
            min(63, (mixed_8[1] * 63 + 127) // 255),
            min(31, (mixed_8[2] * 31 + 127) // 255),
        )
        return (mixed[0] << 11) | (mixed[1] << 5) | mixed[2]

    def cell_field_offset(x: int, y: int, field: int) -> int:
        index = (
            plan.cell_table_base_u16
            + 9 * (x + plan.cell_table_stride * y)
            + field
        )
        byte_offset = grid_offset + index * 2
        if not grid_offset <= byte_offset <= grid_end - 2:
            raise QuestSyncError("Координаты объекта выходят за клеточную таблицу SAV.")
        return byte_offset

    building_navigation_profiles = infer_building_navigation_profiles(
        bytes(payload),
        plan.relationship,
        old_building_records,
        plan.cell_table_base_u16,
        plan.cell_table_stride,
        plan.width,
        plan.height,
    )
    old_building_footprints = [
        building_navigation_footprint(record, building_navigation_profiles)
        for record in old_building_records
    ]
    new_building_footprints = [
        building_navigation_footprint(record, building_navigation_profiles)
        for record in new_building_records
    ]
    building_navigation_unseen_profile_ids = [
        index + 1
        for index, record in enumerate(new_building_records)
        if _building_navigation_profile_key(record) not in building_navigation_profiles
    ]

    # Army occupancy markers live in cell fields 3/4.  Structural army-ID
    # changes must be applied even when an army has moved during gameplay.
    source_army_tail = bytes(plan.payload[plan.text_end:])
    source_army_blocks: list[bytes] = []
    runtime_needed = (plan.army_count + 1) * army_runtime.RUNTIME_ARMY_SIZE
    if len(source_army_tail) >= runtime_needed:
        source_army_blocks = [
            source_army_tail[(index + 1) * army_runtime.RUNTIME_ARMY_SIZE:
                             (index + 2) * army_runtime.RUNTIME_ARMY_SIZE]
            for index in range(plan.army_count)
        ]

    if has_army_structure_change:
        for y in range(plan.height):
            for x in range(plan.width):
                marker_off = cell_field_offset(x, y, 3)
                kind_off = cell_field_offset(x, y, 4)
                marker = struct.unpack_from("<H", payload, marker_off)[0]
                kind = struct.unpack_from("<H", payload, kind_off)[0]
                old_id = army_runtime.decode_army_cell_marker(
                    marker, kind, max_old_id=plan.army_count
                )
                if old_id is None:
                    continue
                if old_id in deleted_army_ids:
                    struct.pack_into("<H", payload, marker_off, 0)
                    struct.pack_into("<H", payload, kind_off, 0)
                    continue
                new_id = old_to_new_army.get(old_id, old_id)
                if new_id != old_id:
                    struct.pack_into("<H", payload, marker_off, army_runtime.encode_army_cell_marker(new_id)[0])

    army_marker_expectations: dict[int, tuple[int, int, bool]] = {}

    def set_army_marker(x: int, y: int, army_id: int, enabled: bool) -> None:
        if not (0 <= x < plan.width and 0 <= y < plan.height):
            raise QuestSyncError(f"Координаты армии {army_id} выходят за карту: ({x}, {y}).")
        marker_off = cell_field_offset(x, y, 3)
        kind_off = cell_field_offset(x, y, 4)
        marker = struct.unpack_from("<H", payload, marker_off)[0]
        kind = struct.unpack_from("<H", payload, kind_off)[0]
        expected, army_kind = army_runtime.encode_army_cell_marker(army_id)
        if enabled:
            if (marker, kind) in ((0, 0), (expected, army_kind)):
                struct.pack_into("<H", payload, marker_off, expected)
                struct.pack_into("<H", payload, kind_off, army_kind)
            else:
                raise QuestSyncError(f"Клетка ({x}, {y}) для армии {army_id} уже занята; перенос остановлен.")
        elif marker == expected and kind == army_kind:
            struct.pack_into("<H", payload, marker_off, 0)
            struct.pack_into("<H", payload, kind_off, 0)
        army_marker_expectations[army_id] = (x, y, enabled)

    if source_army_blocks or plan.army_count == 0:
        for target_index, new_rec in enumerate(new_army_records):
            new_id = target_index + 1
            source_index = army_mapping[target_index] if target_index < len(army_mapping) else None
            if source_index is None:
                if (1 - new_rec.activity) == 1:
                    set_army_marker(new_rec.x, new_rec.y, new_id, True)
                continue
            old_rec = old_army_records[source_index]
            block = source_army_blocks[source_index]
            current_active = block[army_runtime.ACTIVE_OFF]
            current_x = struct.unpack_from("<I", block, army_runtime.CURRENT_X_OFF)[0]
            current_y = struct.unpack_from("<I", block, army_runtime.CURRENT_Y_OFF)[0]
            active_changed = new_rec.activity != old_rec.activity
            pos_changed = (new_rec.x, new_rec.y) != (old_rec.x, old_rec.y)
            if not (active_changed or pos_changed):
                continue
            can_change_active = current_active == (1 - old_rec.activity)
            desired_active = (1 - new_rec.activity) if (active_changed and can_change_active) else current_active
            desired_x, desired_y = army_runtime.merged_position((current_x, current_y), old_rec, new_rec)
            if (desired_active != current_active) or (desired_x, desired_y) != (current_x, current_y):
                set_army_marker(current_x, current_y, new_id, False)
                if desired_active == 1:
                    set_army_marker(desired_x, desired_y, new_id, True)

    decoration_property_writes = 0
    decoration_visual_writes = 0
    terrain_grid_writes = 0
    rebuilt_table_offsets: set[int] = set()

    terrain_cells = {(item.x, item.y) for item in plan.terrain_changes}
    # Rebuild cells must be forced to the modified terrain baseline even when
    # the source SAV currently contains decoration-derived property values.
    # Existing decorations are then reapplied below, and the list rebuild
    # reconstructs the final A/B + visual layers from the modified DTm.
    decorated_terrain_cells: set[tuple[int, int]] = (
        plan.decoration_rebuild_cells & terrain_cells
    )
    for item in plan.terrain_reapply_decorations:
        decorated_terrain_cells.update(_decoration_cells(item) & terrain_cells)
    for change in plan.terrain_changes:
        terrain_offset = (
            grid_offset
            + plan.terrain_grid_base_byte
            + change.y * plan.terrain_grid_stride_bytes
            + 1
            + change.x
        )
        if not grid_offset <= terrain_offset < grid_end:
            raise QuestSyncError("Клетка выходит за сетку ландшафта SAV.")
        if payload[terrain_offset] != change.new_tile:
            payload[terrain_offset] = change.new_tile
            terrain_grid_writes += 1

        visual_index = (
            plan.visual_grid_base_u16
            + change.x
            + plan.visual_grid_stride * change.y
        )
        before = grid_u16(visual_index)
        after = VISUAL_TERRAIN_BASE[change.new_tile]
        if before != after:
            set_grid_u16(visual_index, after)
            decoration_visual_writes += 1

        property_targets = [
            (terrain_property_index(0, change.x, change.y), property_a_base),
            (terrain_property_index(1, change.x, change.y), property_b_base),
        ]
        c_index = property_targets[0][0] - plan.property_ring_size_u16
        if c_index >= 0:
            property_targets.append((c_index, property_c_base))
        navigation_value = struct.unpack_from(
            "<H", payload, cell_field_offset(change.x, change.y, 5)
        )[0]
        for index, bases in property_targets:
            before = grid_u16(index)
            if (
                (change.x, change.y) not in decorated_terrain_cells
                and navigation_value
                and before != bases[change.old_tile]
            ):
                continue
            after = bases[change.new_tile]
            if before != after:
                set_grid_u16(index, after)
                decoration_property_writes += 1

    if plan.decoration_rebuild_cells:
        # Reconstruct the complete affected region from the modified terrain,
        # not only cells occupied by old decorations.  This is essential when
        # a moved/replaced decoration and the terrain change overlap: newly
        # covered cells must start from the new terrain before decorations are
        # layered back in DTm order.
        reset_cells = plan.decoration_rebuild_cells
        for x, y in sorted(reset_cells):
            tile_id = surface[x + plan.width * y]
            for grid_number, bases in (
                (0, property_a_base),
                (1, property_b_base),
            ):
                index = terrain_property_index(grid_number, x, y)
                before = grid_u16(index)
                after = bases[tile_id]
                if after != before:
                    set_grid_u16(index, after)
                    decoration_property_writes += 1
            visual_index = plan.visual_grid_base_u16 + x + plan.visual_grid_stride * y
            before = grid_u16(visual_index)
            after = VISUAL_TERRAIN_BASE[tile_id]
            if after != before:
                set_grid_u16(visual_index, after)
                decoration_visual_writes += 1

        for old_item in plan.decoration_rebuild_original:
            table_x = old_item.x if old_item.atlas.is_tree else old_item.x - 1
            table_field = 0 if old_item.atlas.is_tree else 8
            id_offset = cell_field_offset(table_x, old_item.y, table_field)
            struct.pack_into("<H", payload, id_offset, 0)
            rebuilt_table_offsets.add(id_offset)

    def apply_decoration(
        item: DecorationAddition,
        rebuilding: bool,
        allowed_cells: set[tuple[int, int]] | None = None,
        write_table: bool = True,
        terrain_reapply: bool = False,
    ) -> None:
        nonlocal decoration_property_writes, decoration_visual_writes
        footprint_width, footprint_height = item.atlas.footprint
        for dy in range(footprint_height):
            for dx in range(footprint_width):
                x = item.x - dx
                y = item.y - dy
                if allowed_cells is not None and (x, y) not in allowed_cells:
                    continue
                tile_id = surface[x + plan.width * y]
                if terrain_reapply:
                    property_grids = [
                        (terrain_property_index(0, x, y), 0),
                        (terrain_property_index(1, x, y), 1),
                    ]
                    c_index = property_grids[0][0] - plan.property_ring_size_u16
                    if c_index >= 0:
                        property_grids.append((c_index, 2))
                else:
                    property_grids = [
                        (
                            terrain_property_index(grid_number, x, y)
                            if rebuilding else property_index(grid_number, x, y),
                            property_kind,
                        )
                        for grid_number, property_kind in ((0, 0), (1, 1))
                    ]
                for index, property_kind in property_grids:
                    before = grid_u16(index)
                    after = update_property(
                        before, item.atlas, tile_id, property_kind
                    )
                    if after != before:
                        set_grid_u16(index, after)
                        decoration_property_writes += 1
                visual_index = (
                    plan.visual_grid_base_u16
                    + x
                    + plan.visual_grid_stride * y
                )
                before = grid_u16(visual_index)
                after = blend_visual(before, item.atlas)
                if after != before:
                    set_grid_u16(visual_index, after)
                    decoration_visual_writes += 1

        if not write_table:
            return
        table_x = item.x if item.atlas.is_tree else item.x - 1
        table_field = 0 if item.atlas.is_tree else 8
        id_offset = cell_field_offset(table_x, item.y, table_field)
        occupied = struct.unpack_from("<H", payload, id_offset)[0]
        if occupied and (not rebuilding or id_offset not in rebuilt_table_offsets):
            raise QuestSyncError(
                f"Служебная клетка декорации №{item.index + 1} уже занята."
            )
        struct.pack_into("<H", payload, id_offset, item.object_id)
        if rebuilding:
            rebuilt_table_offsets.add(id_offset)

    for item in plan.terrain_reapply_decorations:
        apply_decoration(item, False, terrain_cells, False, True)

    if plan.decoration_rebuild_cells:
        for item in plan.decoration_rebuild_modified:
            apply_decoration(item, True)
    else:
        for item in plan.decoration_additions:
            apply_decoration(item, False)

    building_property_writes = 0
    if building_grid_changed:
        # Building navigation has its own hidden collision/property layer.
        # Native V.4 saves use C/A/B = 3/6/3 on every navigation-footprint
        # cell.  When a building is deleted or moved, reconstruct the affected
        # property cells from the modified terrain + modified decorations
        # first, then apply target buildings on top.  Building placement does
        # not itself alter the visual-color grid.
        affected_building_cells: set[tuple[int, int]] = set()
        for footprint in old_building_footprints:
            affected_building_cells.update(footprint)
        for footprint in new_building_footprints:
            affected_building_cells.update(footprint)

        def set_building_property(index: int, value: int) -> None:
            nonlocal building_property_writes
            if index < 0:
                return
            before = grid_u16(index)
            if before != value:
                set_grid_u16(index, value)
                building_property_writes += 1

        for x, y in sorted(affected_building_cells):
            if not (0 <= x < plan.width and 0 <= y < plan.height):
                raise QuestSyncError(
                    f"Область строения выходит за карту: ({x}, {y})."
                )
            tile_id = surface[x + plan.width * y]
            a_index = terrain_property_index(0, x, y)
            b_index = terrain_property_index(1, x, y)
            c_index = a_index - plan.property_ring_size_u16
            set_building_property(c_index, property_c_base[tile_id])
            set_building_property(a_index, property_a_base[tile_id])
            set_building_property(b_index, property_b_base[tile_id])

        if plan.objects_ugs is None:
            raise QuestSyncError(
                "Для изменения области строения нужен Graphics\\Objects\\Objects.ugs."
            )
        building_atlas = parse_objects_ugs(plan.objects_ugs)
        modified_decorations_for_buildings = _parse_decorations(
            plan.modified_inner,
            plan.modified_sections["decorations"],
            building_atlas,
            plan.width,
            plan.height,
        )
        for item in modified_decorations_for_buildings:
            overlap = _decoration_cells(item) & affected_building_cells
            if not overlap:
                continue
            for x, y in overlap:
                tile_id = surface[x + plan.width * y]
                a_index = terrain_property_index(0, x, y)
                b_index = terrain_property_index(1, x, y)
                c_index = a_index - plan.property_ring_size_u16
                for index, property_kind in (
                    (c_index, 2),
                    (a_index, 0),
                    (b_index, 1),
                ):
                    if index < 0:
                        continue
                    before = grid_u16(index)
                    after = update_property(before, item.atlas, tile_id, property_kind)
                    if after != before:
                        set_grid_u16(index, after)
                        building_property_writes += 1

        for footprint in new_building_footprints:
            for x, y in footprint:
                a_index = terrain_property_index(0, x, y)
                b_index = terrain_property_index(1, x, y)
                c_index = a_index - plan.property_ring_size_u16
                set_building_property(c_index, 3)
                set_building_property(a_index, 6)
                set_building_property(b_index, 3)

    if building_grid_changed:
        # Remove the complete old footprint.  Field +1 stores Building ID only
        # at the DTm anchor; field +5 stores the anchor linear index on every
        # footprint cell (native Objects control, all building sizes).
        for source_index, (record, footprint) in enumerate(
            zip(old_building_records, old_building_footprints)
        ):
            if not (0 <= record.x < plan.width and 0 <= record.y < plan.height):
                raise QuestSyncError(
                    f"Исходное строение №{source_index + 1} находится вне карты."
                )
            id_off = cell_field_offset(record.x, record.y, 1)
            if struct.unpack_from("<H", payload, id_off)[0] == source_index + 1:
                struct.pack_into("<H", payload, id_off, 0)
            old_anchor = record.x + plan.cell_table_stride * record.y
            for x, y in footprint:
                if not (0 <= x < plan.width and 0 <= y < plan.height):
                    raise QuestSyncError(
                        f"Область исходного строения №{source_index + 1} выходит за карту."
                    )
                nav_off = cell_field_offset(x, y, 5)
                if struct.unpack_from("<H", payload, nav_off)[0] == old_anchor:
                    struct.pack_into("<H", payload, nav_off, 0)

        # Rebuild all target footprints so delete/insert/reorder and coordinate
        # edits use target Building IDs consistently. Native V.4 maps may have
        # overlapping building footprints (confirmed on the supplied large
        # shoreline control). Field +5 follows list order: the later Building
        # record wins on a shared footprint cell. Anchor IDs in field +1 remain
        # independent, so only duplicate anchor coordinates are invalid.
        anchor_cells: set[tuple[int, int]] = set()
        for target_index, (record, footprint) in enumerate(
            zip(new_building_records, new_building_footprints)
        ):
            if not (0 <= record.x < plan.width and 0 <= record.y < plan.height):
                raise QuestSyncError(
                    f"Строение №{target_index + 1} находится вне карты: ({record.x}, {record.y})."
                )
            anchor_cell = (record.x, record.y)
            if anchor_cell in anchor_cells:
                raise QuestSyncError(
                    f"Два строения имеют одну якорную клетку {anchor_cell}."
                )
            anchor_cells.add(anchor_cell)
            for x, y in footprint:
                if not (0 <= x < plan.width and 0 <= y < plan.height):
                    raise QuestSyncError(
                        f"Область строения №{target_index + 1} выходит за карту."
                    )
            id_off = cell_field_offset(record.x, record.y, 1)
            current_id = struct.unpack_from("<H", payload, id_off)[0]
            if current_id:
                raise QuestSyncError(
                    f"Якорная клетка строения №{target_index + 1} уже содержит объект ID {current_id}."
                )
            struct.pack_into("<H", payload, id_off, target_index + 1)
            anchor = record.x + plan.cell_table_stride * record.y
            for x, y in footprint:
                nav_off = cell_field_offset(x, y, 5)
                # Do not reject a different building anchor here: native maps
                # intentionally allow footprint overlap. Processing in target
                # Building order reproduces the native last-record-wins rule.
                struct.pack_into("<H", payload, nav_off, anchor)

    if plan.lantern_rewrite:
        for index in range(plan.lantern_count):
            record = _record(
                plan.original_inner,
                plan.original_sections["lanterns"],
                index,
                LANTERN_SIZE,
            )
            x, y = struct.unpack_from("<HH", record, 0)
            lantern_id = record[4]
            id_offset = cell_field_offset(x, y, 2)
            current = struct.unpack_from("<H", payload, id_offset)[0]
            if current != lantern_id:
                raise QuestSyncError(
                    f"Таблица исходного фонаря ID {lantern_id} не совпадает с SAV."
                )
            struct.pack_into("<H", payload, id_offset, 0)
        for item in plan.lantern_records:
            id_offset = cell_field_offset(item.x, item.y, 2)
            if struct.unpack_from("<H", payload, id_offset)[0] != 0:
                raise QuestSyncError(
                    f"Клетка фонаря ID {item.lantern_id} уже занята другим фонарём."
                )
            struct.pack_into("<H", payload, id_offset, item.lantern_id)

    # Structural Building-ID renumbering is independent from quest progress.
    # Only EventData.building_id[3] (+30..+32) is touched.
    if plan.building_structure_changed:
        for index in range(plan.event_count):
            dst = plan.event_start + index * EVENT_SIZE
            record = bytes(payload[dst:dst + EVENT_SIZE])
            payload[dst:dst + EVENT_SIZE] = building_runtime.remap_event_building_refs(
                record, deleted_building_ids, old_to_new_building
            )

    # Structural army-ID renumbering must also be applied to progressed event
    # records.  Only the known army-reference bytes are touched; timestamps and
    # event progress bytes remain exactly as stored in SAV.
    if has_army_structure_change:
        for index in range(plan.event_count):
            dst = plan.event_start + index * EVENT_SIZE
            record = bytes(payload[dst:dst + EVENT_SIZE])
            payload[dst:dst + EVENT_SIZE] = army_runtime.remap_event_army_refs(
                record, deleted_army_ids, old_to_new_army
            )

    for item in plan.selected:
        if item.index >= plan.event_count:
            continue
        index = item.index
        modified_record = plan.modified_inner[
            modified_event_offset + index * EVENT_SIZE:
            modified_event_offset + (index + 1) * EVENT_SIZE
        ]
        dst = plan.event_start + index * EVENT_SIZE
        payload[dst:dst + EVENT_SIZE] = modified_record

    def imported_text(value: bytes) -> bytes:
        if plan.hero_replacement is not None:
            return value.replace(HERO_TOKEN, plan.hero_replacement)
        return value

    # Rebuild the save text table by logical section.  This is essential when
    # army IDs are inserted/deleted because every event-text index after the
    # army text block shifts by 3 strings per army.
    old_save_building_start = 2
    old_save_army_start = army_text_base(plan.building_count, save=True)
    old_save_event_start = event_text_base(plan.building_count, plan.army_count, save=True)
    old_save_named_start = old_save_event_start + plan.event_count * 3

    old_map_building_start = 4
    new_map_building_start = 4
    old_map_army_start = army_text_base(plan.building_count, save=False)
    new_map_army_start = army_text_base(plan.modified_building_count, save=False)
    old_map_event_start = event_text_base(plan.building_count, plan.army_count, save=False)
    new_map_event_start = event_text_base(
        plan.modified_building_count, plan.modified_army_count, save=False
    )

    strings: list[bytes] = list(plan.save_strings[:2])

    # Building texts follow the structural mapping just like army texts.
    # Preserve runtime/player text for unchanged fields, but apply explicit
    # editor changes and import all text for genuinely new buildings.
    for target_index in range(plan.modified_building_count):
        source_index = building_mapping[target_index] if target_index < len(building_mapping) else None
        new_src = new_map_building_start + target_index * 3
        new_texts = plan.modified_strings[new_src:new_src + 3]
        if source_index is None:
            strings.extend(imported_text(v) for v in new_texts)
            continue
        save_src = old_save_building_start + source_index * 3
        old_src = old_map_building_start + source_index * 3
        saved = plan.save_strings[save_src:save_src + 3]
        old_texts = plan.original_strings[old_src:old_src + 3]
        for saved_value, old_value, new_value in zip(saved, old_texts, new_texts):
            strings.append(
                saved_value if event_texts_equal(old_value, new_value)
                else imported_text(new_value)
            )

    # Armies may be inserted/deleted in the middle.  Preserve SAV text for
    # mapped armies unless the editor actually changed that particular field.
    for target_index in range(plan.modified_army_count):
        source_index = army_mapping[target_index] if target_index < len(army_mapping) else None
        new_src = new_map_army_start + target_index * 3
        new_texts = plan.modified_strings[new_src:new_src + 3]
        if source_index is None:
            strings.extend(imported_text(v) for v in new_texts)
            continue
        save_src = old_save_army_start + source_index * 3
        old_src = old_map_army_start + source_index * 3
        saved = plan.save_strings[save_src:save_src + 3]
        old_texts = plan.original_strings[old_src:old_src + 3]
        for saved_value, old_value, new_value in zip(saved, old_texts, new_texts):
            strings.append(
                saved_value if event_texts_equal(old_value, new_value)
                else imported_text(new_value)
            )

    selected_event_ids = {item.index for item in plan.selected}
    for index in range(plan.event_count):
        save_src = old_save_event_start + index * 3
        old_src = old_map_event_start + index * 3
        new_src = new_map_event_start + index * 3
        saved = plan.save_strings[save_src:save_src + 3]
        old_texts = plan.original_strings[old_src:old_src + 3]
        new_texts = plan.modified_strings[new_src:new_src + 3]
        for saved_value, old_value, new_value in zip(saved, old_texts, new_texts):
            if index in selected_event_ids and not event_texts_equal(old_value, new_value):
                strings.append(imported_text(new_value))
            else:
                strings.append(saved_value)

    for index in range(plan.event_count, plan.modified_event_count):
        src = new_map_event_start + index * 3
        strings.extend(imported_text(v) for v in plan.modified_strings[src:src + 3])

    # Named-unit/other trailing strings are not indexed by army count; preserve
    # their current SAV contents and progress.
    strings.extend(plan.save_strings[old_save_named_start:])

    catalog = army_runtime.load_catalog()
    try:
        new_building_blob, building_runtime_report = building_runtime.build_runtime_blob(
            plan.payload,
            plan.relationship,
            old_building_records_raw,
            new_building_records_raw,
            building_mapping,
            catalog,
            strict=True,
            deleted_army_ids=deleted_army_ids if has_army_structure_change else None,
            old_to_new_army=old_to_new_army if has_army_structure_change else None,
        )
    except (ValueError, struct.error) as exc:
        raise QuestSyncError(f"Не удалось перестроить runtime строений: {exc}") from exc

    building_start = plan.relationship["buildings"]["offset"]
    lantern_start = plan.relationship["lanterns"]["offset"]
    if plan.lantern_rewrite:
        lantern_blob = b"".join(item.record for item in plan.lantern_records)
    else:
        lantern_blob = bytes(payload[lantern_start:plan.event_start])
    new_event_blob = plan.modified_inner[
        modified_event_offset + plan.event_count * EVENT_SIZE:
        modified_event_offset + plan.modified_event_count * EVENT_SIZE
    ]
    new_text_block = b"".join(value + b"\0" for value in strings)
    old_event_end = plan.event_start + plan.event_count * EVENT_SIZE

    source_tail = bytes(plan.payload[plan.text_end:])
    if has_army_binary_change or has_army_structure_change:
        try:
            synced_tail = army_runtime.sync_army_tail(
                source_tail, old_army_records, new_army_records, army_mapping, catalog
            )
            synced_tail = army_runtime.sync_army_cell_indices(
                source_tail, synced_tail, army_mapping, plan.army_count,
                plan.cell_table_stride,
            )
        except (ValueError, struct.error) as exc:
            raise QuestSyncError(f"Не удалось перестроить runtime армий: {exc}") from exc
    else:
        synced_tail = source_tail

    # Building insertion/deletion renumbers references stored inside the live
    # hero/NPC runtime slots independently of ArmyData changes.  Apply this
    # after army-tail reconstruction so the final target slot layout is known.
    if plan.building_structure_changed:
        try:
            synced_tail = building_runtime.remap_runtime_start_buildings(
                synced_tail,
                old_army_records,
                new_army_records,
                army_mapping,
                deleted_building_ids,
                old_to_new_building,
            )
        except (ValueError, struct.error) as exc:
            raise QuestSyncError(f"Не удалось перенумеровать ссылки на строения в runtime армий: {exc}") from exc

    building_verify = building_runtime.verify_runtime_blob(
        new_building_blob,
        new_building_records,
        catalog,
        rebuilt_garrison_ids=building_runtime_report.get("garrison_rebuilt_building_ids", ()),
    )
    if not building_verify["ok"]:
        raise QuestSyncError(
            "Построенный runtime строений не прошёл строгую проверку: "
            + "; ".join(building_verify["issues"])
        )

    new_payload = (
        bytes(payload[:building_start])
        + new_building_blob
        + lantern_blob
        + bytes(payload[plan.event_start:old_event_end])
        + new_event_blob
        + bytes(payload[old_event_end:plan.text_start])
        + new_text_block
        + synced_tail
    )
    # Post-build reference/grid proof.  This catches stale Building IDs after
    # structural edits before the SAV is packed.
    try:
        final_relation = sav_tool.relate(new_payload, plan.modified_inner, plan.container_info)["relationship"]
    except (sav_tool.SavError, ValueError, struct.error) as exc:
        raise QuestSyncError(f"Финальная структура SAV после синхронизации строений не распознана: {exc}") from exc

    final_event_start = final_relation["events"]["offset"]
    for army_id, (x, y, enabled) in army_marker_expectations.items():
        if not enabled:
            continue
        block = synced_tail[army_id * army_runtime.RUNTIME_ARMY_SIZE:(army_id + 1) * army_runtime.RUNTIME_ARMY_SIZE]
        runtime_xy = tuple(struct.unpack_from('<I', block, off)[0] for off in (army_runtime.CURRENT_X_OFF, army_runtime.CURRENT_Y_OFF))
        marker = struct.unpack_from('<H', new_payload, cell_field_offset(x, y, 3))[0]
        kind = struct.unpack_from('<H', new_payload, cell_field_offset(x, y, 4))[0]
        if runtime_xy != (x, y) or (marker, kind) != army_runtime.encode_army_cell_marker(army_id):
            raise QuestSyncError(f'Армия {army_id}: runtime-позиция и клеточный маркер не согласованы.')
    for event_index in range(plan.modified_event_count):
        record = new_payload[final_event_start + event_index * EVENT_SIZE:final_event_start + (event_index + 1) * EVENT_SIZE]
        for off in (30, 31, 32):
            value = record[off]
            if value not in (0, 0xFF) and value > plan.modified_building_count:
                raise QuestSyncError(
                    f"Событие №{event_index + 1}: ссылка на несуществующее строение ID {value}."
                )

    for building_index, record in enumerate(new_building_records, 1):
        id_off = cell_field_offset(record.x, record.y, 1)
        if struct.unpack_from("<H", new_payload, id_off)[0] != building_index:
            raise QuestSyncError(f"Строение №{building_index}: неверный Building ID в compiled grid.")
    expected_navigation = building_runtime.expected_navigation_owners(
        new_building_records, plan.cell_table_stride, new_building_footprints
    )
    for (x, y), (anchor, building_index) in expected_navigation.items():
        nav_off = cell_field_offset(x, y, 5)
        if struct.unpack_from("<H", new_payload, nav_off)[0] != anchor:
            raise QuestSyncError(
                f"Строение №{building_index}: неверная footprint-навигация в клетке ({x}, {y})."
            )

    report = plan_summary(plan, include_events=True)
    report.update(
        {
            "source_payload_size": len(plan.payload),
            "output_payload_size": len(new_payload),
            "text_size_delta": len(new_text_block) - (plan.text_end - plan.text_start),
            "building_runtime_size_delta": len(new_building_blob) - (lantern_start - building_start),
            "building_runtime_sync_enabled": bool(plan.building_binary_changed or has_army_structure_change),
            "building_runtime": building_runtime_report,
            "building_runtime_verifier": building_verify,
            "building_reference_verifier": {"ok": True},
            "lantern_size_delta": len(lantern_blob) - (plan.event_start - lantern_start),
            "event_size_delta": len(new_event_blob),
            "army_runtime_size_delta": len(synced_tail) - len(source_tail),
            "army_runtime_sync_enabled": bool(has_army_binary_change or has_army_structure_change),
            "terrain_grid_write_count": terrain_grid_writes,
            "building_property_write_count": building_property_writes,
            "building_navigation_profile_count": len(building_navigation_profiles),
            "building_navigation_extra_cell_count": sum(
                len(cells) for cells in building_navigation_profiles.values()
            ),
            "building_navigation_unseen_profile_building_ids": (
                building_navigation_unseen_profile_ids
            ),
            "building_navigation_uses_rectangular_fallback": bool(
                building_navigation_unseen_profile_ids
            ),
            "decoration_property_write_count": decoration_property_writes,
            "decoration_visual_write_count": decoration_visual_writes,
            "decoration_visual_values_are_experimental": bool(
                plan.decoration_additions or plan.decoration_removals
            ),
            "source_payload_sha256": sav_tool.sha256(plan.payload),
            "output_payload_sha256": sav_tool.sha256(new_payload),
        }
    )
    return new_payload, report


def atomic_write(path: Path, data: bytes, *, overwrite: bool = True) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temp_name, path)
        else:
            # Atomic no-clobber publication, including a file created after
            # convert_plan's preflight check.
            os.link(temp_name, path)
            os.unlink(temp_name)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def convert_plan(
    plan: SyncPlan,
    output_sav: Path,
    *,
    write_report: bool = True,
    allow_overwrite: bool = False,
    prepared_result: tuple[bytes, dict[str, Any]] | None = None,
    save_name: str | None = None,
) -> dict[str, Any]:
    output_sav = Path(output_sav)
    report_path = output_sav.with_suffix(output_sav.suffix + '.sync.json')
    protected = [Path(plan.source_sav), Path(plan.original_dtm), Path(plan.modified_dtm)]
    if getattr(plan, 'objects_ugs', None) is not None:
        protected.append(Path(plan.objects_ugs))
    protected.extend(Path(p) for p in army_runtime.load_catalog().get('_game_data', {}).get('files', {}).values())
    destinations = [output_sav]
    if write_report:
        destinations.append(report_path)
    for destination in destinations:
        for source in protected:
            same = destination.resolve() == source.resolve()
            if not same and destination.exists() and source.exists():
                same = destination.samefile(source)
            if same:
                raise QuestSyncError(f'Нельзя перезаписывать входной файл: {source}. Выберите другой выходной путь.')
        if destination.exists():
            if not destination.is_file():
                raise QuestSyncError(f'Выходной путь не является файлом: {destination}')
            if not allow_overwrite:
                raise QuestSyncError(f'Выходной файл уже существует: {destination}')
    if output_sav.exists() and not allow_overwrite:
        raise QuestSyncError(f"Выходной файл уже существует: {output_sav}")

    payload, report = prepared_result if prepared_result is not None else build_synced_payload(plan)
    report = dict(report)
    if save_name is not None:
        report['source_save_name'] = sav_tool.save_metadata(payload)['slot_name']
        payload = sav_tool.rename_save_slot(payload, save_name)
        report.update(save_name=save_name, output_payload_size=len(payload),
                      output_payload_sha256=sav_tool.sha256(payload))
    packed = sav_tool.pack_sav_bytes(payload, plan.save_raw[4:8])
    verified_payload, verified_info = sav_tool.unpack_sav_bytes(packed)
    if verified_payload != payload:
        raise QuestSyncError("Внутренняя проверка упаковки SAV не пройдена.")
    report.update(
        {
            "output_sav": str(output_sav),
            "output_file_size": len(packed),
            "output_sha256": sav_tool.sha256(packed),
            "verified_chunk_count": verified_info["chunk_count"],
            "container_roundtrip_verified": True,
            "output_status": "written",
            "report_status": "written" if write_report else "disabled",
        }
    )
    # Serialize before committing the SAV: a JSON error must not leave a
    # successful save disguised as a failed conversion.
    report_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode('utf-8') if write_report else None
    atomic_write(output_sav, packed, overwrite=allow_overwrite)
    if write_report:
        try:
            atomic_write(report_path, report_bytes, overwrite=allow_overwrite)
        except OSError as exc:
            report['report_status'] = 'failed'
            report['report_error'] = str(exc)
            report['report_attempted_path'] = str(report_path)
        else:
            report["report_file"] = str(report_path)
    return report


def command_analyze(args: argparse.Namespace) -> None:
    import safe_sync
    prepared = safe_sync.prepare_paths(args.save, args.original, args.modified, args.objects_ugs,
                                       delete_event_ids=args.delete_event)
    report = dict(prepared.report)
    if args.summary_only:
        report.pop('events', None)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        # Analysis is read-only with respect to the inputs and existing files.
        atomic_write(args.output, (text + '\n').encode('utf-8'), overwrite=False)
    else:
        print(text)


def command_convert(args: argparse.Namespace) -> None:
    import safe_sync
    prepared = safe_sync.prepare_paths(args.save, args.original, args.modified, args.objects_ugs,
                                       delete_event_ids=args.delete_event)
    if prepared.blocked and not args.skip_blocked:
        print(json.dumps({'blocked_changes':prepared.blocked,
                          'suggestion':'Повторите с --skip-blocked, чтобы сохранить без перечисленных изменений.'},
                         ensure_ascii=False, indent=2))
    report = safe_sync.save_prepared(
        prepared,
        args.output,
        accept_partial=args.skip_blocked,
        write_report=not args.no_report,
        allow_overwrite=args.overwrite,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("analyze", "convert"):
        p = sub.add_parser(name)
        p.add_argument("--save", type=Path, required=True, help="source SAV")
        p.add_argument("--original", type=Path, required=True, help="original DTm used by SAV")
        p.add_argument("--modified", type=Path, required=True, help="modified DTm")
        p.add_argument('--delete-event', type=int, action='append', default=[],
                       help='event ID in modified DTm to delete; repeat for several events')
        p.add_argument(
            "--objects-ugs",
            type=Path,
            help="optional Objects.ugs path; auto-detected near the game folder",
        )
        if name == "analyze":
            p.add_argument("-o", "--output", type=Path)
            p.add_argument("--summary-only", action="store_true")
            p.set_defaults(func=command_analyze)
        else:
            p.add_argument("--output", type=Path, required=True, help="new SAV")
            p.add_argument("--overwrite", action="store_true")
            p.add_argument("--no-report", action="store_true")
            p.add_argument('--skip-blocked', action='store_true', help='explicitly accept the verified partial result')
            p.set_defaults(func=command_convert)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        # Direct CLI execution follows the same production rule as the GUI:
        # live game data must be beside the application, and Objects.ugs comes
        # from the fixed Graphics/Objects path rather than the selected map.
        import game_resources
        resources = game_resources.load_game_resources(objects_parser=parse_objects_ugs)
        game_resources.install_game_resources(resources)
        args.objects_ugs = resources.objects_ugs
        args.func(args)
    except (OSError, ValueError, struct.error, game_resources.GameDataError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
