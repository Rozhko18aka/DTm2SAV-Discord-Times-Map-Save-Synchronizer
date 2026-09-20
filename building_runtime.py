"""Native MapLDV V.4 building runtime support for QuestSync.

The 358-byte BuildingData core is stored in SAV with a few runtime-only fields.
Building types 1/3/4/12 additionally own a 14375-byte garrison state block.
This module keeps those details isolated from the quest/event synchronizer.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import random
import struct
from typing import Iterable

import army_runtime

BUILDING_SIZE = 358
RUNTIME_GARRISON_SIZE = 14375
GARRISON_TYPES = frozenset((1, 3, 4, 12))
MARKET_TYPES = frozenset((1, 6, 7))

# V.4 BuildingData offsets proven against Objects/Objects2 native controls.
X_OFF = 0
Y_OFF = 2
PICTURE_NUMBER_OFF = 4
PICTURE_VARIANT_OFF = 5
TYPE_OFF = 6
ARTIFACTS_OFF = 136
MARKET_RUNTIME_SLOT_COUNT = 12
RECRUITS_OFF = 264
GOLD_INCOME_OFF = 282
MAX_GOLD_INCOME_OFF = 284
RUNTIME_CURRENT_GOLD_OFF = 286
EVENT_AMOUNT_OFF = 288
SIZE_X_OFF = 289
SIZE_Y_OFF = 290
RUNTIME_FLAG_OFF = 291
OWNER_ARMY_OFF = 292
BARRACKS_OFF = 294
MARKET_COUNT_OFF = 295
SPELLS_OFF = 308
GARRISON_OFF = 314
GARRISON_DEFENCE_OFF = 332
MIN_ARTIFACT_PRICE_OFF = 333
MAX_ARTIFACT_PRICE_OFF = 335
GROUP_OFF = 337
RELATIONS_OFF = 338
RUNTIME_OPAQUE_OFF = 342
RUNTIME_OPAQUE_END = 350
MANA_INCOME_OFF = 350
MAX_MANA_INCOME_OFF = 351
RUNTIME_CURRENT_MANA_OFF = 352
KNIGHT_START_OFF = 353
MAGE_START_OFF = 354
RANGER_START_OFF = 355
ALL_START_OFF = 356
GARRISON_ONLY_PC_OFF = 357

# Attached garrison: army-like state without the ordinary +0x800 header.
G_COUNT_OFF = 0x0000
G_UNIT_BASE_OFF = 0x0004
G_UNIT_STRIDE = army_runtime.UNIT_STRIDE
G_UNIT_MAX = 12
G_AGG_STRENGTH_OFF = army_runtime.AGG_STRENGTH_OFF - 0x800  # 0x1648
G_FORMATION_OFF = army_runtime.FORMATION_OFF - 0x800          # 0x164C
G_GOLD_INCOME_OFF = army_runtime.GOLD_INCOME_OFF - 0x800      # 0x16D8
G_PAYMENT_OFF = army_runtime.PAYMENT_OFF - 0x800              # 0x16E0
G_HP_SUM_OFF = army_runtime.HP_SUM_OFF - 0x800                # 0x16F0
G_DEFENCE_OFF = 0x378C
G_RUINS_ITEM_OFF = 0x37B0
G_TOTAL_COST_OFF = 0x380A

# Native sentinels in every empty/populated attached garrison block.
G_SENTINELS = (
    (0x1664, b"\xff" * 4),
    (0x1678, b"\xff" * 12),
    (0x168C, b"\xff" * 8),
)

# SettingsData hero start-building IDs inside the 289-byte embedded map prefix.
HERO_START_BUILDING_OFFSETS = (0x4C, 0x7E, 0xB0)


@dataclass(frozen=True)
class BuildingRecord:
    raw: bytes
    x: int
    y: int
    picture_number: int
    picture_variant: int
    building_type: int
    size_x: int
    size_y: int
    owner_army_id: int
    additional_defence: int

    @property
    def runtime_garrison(self) -> bool:
        return self.building_type in GARRISON_TYPES

    @property
    def runtime_coords(self) -> tuple[int, int]:
        # Native SAV core stores the top-left footprint cell while DTm stores
        # the bottom-right/anchor cell.
        return (
            self.x - max(0, self.size_x - 1),
            self.y - max(0, self.size_y - 1),
        )

    @property
    def footprint(self) -> set[tuple[int, int]]:
        left, top = self.runtime_coords
        return {
            (xx, yy)
            for yy in range(top, self.y + 1)
            for xx in range(left, self.x + 1)
        }

    @property
    def garrison(self) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for slot in range(6):
            uid, level, count = self.raw[GARRISON_OFF + 3 * slot:GARRISON_OFF + 3 * slot + 3]
            if uid and count:
                out.extend((uid, level) for _ in range(count))
        return out

    @property
    def static_artifacts(self) -> list[int]:
        return [
            struct.unpack_from("<H", self.raw, ARTIFACTS_OFF + 2 * i)[0]
            for i in range(6)
            if struct.unpack_from("<H", self.raw, ARTIFACTS_OFF + 2 * i)[0]
        ]


def parse_building_record(raw: bytes) -> BuildingRecord:
    if len(raw) != BUILDING_SIZE:
        raise ValueError("BuildingData must be 358 bytes")
    x, y = struct.unpack_from("<HH", raw, 0)
    return BuildingRecord(
        raw=bytes(raw),
        x=x,
        y=y,
        picture_number=raw[PICTURE_NUMBER_OFF],
        picture_variant=raw[PICTURE_VARIANT_OFF],
        building_type=raw[TYPE_OFF],
        size_x=raw[SIZE_X_OFF],
        size_y=raw[SIZE_Y_OFF],
        owner_army_id=raw[OWNER_ARMY_OFF],
        additional_defence=raw[GARRISON_DEFENCE_OFF],
    )


def expected_navigation_owners(
    records: Iterable[BuildingRecord], row_stride: int
) -> dict[tuple[int, int], tuple[int, int]]:
    """Return native field+5 ownership for building footprints.

    MapLDV V.4 permits overlapping building footprints.  The compiled-grid
    navigation cell is owned by the later BuildingData record, while field +1
    at each anchor remains the building's own ID.  The supplied large shoreline
    native save proves this precedence rule (#108 overlapped by later #158).
    Values are ``(anchor_linear_index, one_based_building_id)``.
    """
    out: dict[tuple[int, int], tuple[int, int]] = {}
    for building_id, record in enumerate(records, 1):
        anchor = record.x + row_stride * record.y
        for cell in record.footprint:
            out[cell] = (anchor, building_id)
    return out


def _sig(raw: bytes) -> bytes:
    return bytes(raw)


def _global_exact_pairs(old_records: list[bytes], new_records: list[bytes]) -> list[tuple[int, int]]:
    old_by: dict[bytes, list[int]] = {}
    new_by: dict[bytes, list[int]] = {}
    for i, raw in enumerate(old_records):
        old_by.setdefault(_sig(raw), []).append(i)
    for j, raw in enumerate(new_records):
        new_by.setdefault(_sig(raw), []).append(j)
    pairs: list[tuple[int, int]] = []
    for signature in old_by.keys() & new_by.keys():
        for oi, nj in zip(old_by[signature], new_by[signature]):
            pairs.append((oi, nj))
    return sorted(pairs, key=lambda p: p[1])


def _pair_crosses_exact(old_index: int, new_index: int, exact_pairs: list[tuple[int, int]]) -> bool:
    return any((oi - old_index) * (nj - new_index) < 0 for oi, nj in exact_pairs)


def align_buildings(old_records: list[bytes], new_records: list[bytes]) -> tuple[list[int | None], list[int]]:
    """Conservative target->source building alignment.

    Exact survivors are reserved globally (there is no persistent Building GUID).
    Remaining records in the same structural lane are treated as edits; records
    that would cross an exact survivor remain add/delete operations.
    """
    n, m = len(old_records), len(new_records)
    mapping: list[int | None] = [None] * m
    exact_pairs = _global_exact_pairs(old_records, new_records)
    used_old: set[int] = set()
    for oi, nj in exact_pairs:
        mapping[nj] = oi
        used_old.add(oi)

    old_left = [i for i in range(n) if i not in used_old]
    new_left = [j for j in range(m) if mapping[j] is None]
    on, nn = len(old_left), len(new_left)
    if not on:
        return mapping, []
    if not nn:
        return mapping, old_left

    INF = 10**9
    dp = [[INF] * (nn + 1) for _ in range(on + 1)]
    prev = [[None] * (nn + 1) for _ in range(on + 1)]
    dp[0][0] = 0
    for i in range(on + 1):
        for j in range(nn + 1):
            value = dp[i][j]
            if value >= INF:
                continue
            if i < on and value + 3 < dp[i + 1][j]:
                dp[i + 1][j] = value + 3
                prev[i + 1][j] = (i, j, "del")
            if j < nn and value + 3 < dp[i][j + 1]:
                dp[i][j + 1] = value + 3
                prev[i][j + 1] = (i, j, "add")
            if i < on and j < nn:
                oi, nj = old_left[i], new_left[j]
                if not _pair_crosses_exact(oi, nj, exact_pairs) and value + 1 < dp[i + 1][j + 1]:
                    dp[i + 1][j + 1] = value + 1
                    prev[i + 1][j + 1] = (i, j, "map")
    i, j = on, nn
    while i or j:
        p = prev[i][j]
        if p is None:
            break
        pi, pj, op = p
        if op == "map":
            mapping[new_left[j - 1]] = old_left[i - 1]
        i, j = pi, pj
    mapped = {x for x in mapping if x is not None}
    return mapping, sorted(set(range(n)) - mapped)


def structure_changed(mapping: list[int | None], deleted: list[int], old_count: int, new_count: int) -> bool:
    if old_count != new_count or deleted or any(v is None for v in mapping):
        return True
    return any(src != dst for dst, src in enumerate(mapping))


def remap_id(value: int, deleted_ids: set[int], old_to_new: dict[int, int], *, deleted_value: int = 0) -> int:
    if value in (0, 0xFF):
        return value
    if value in deleted_ids:
        return deleted_value
    return old_to_new.get(value, value)


def remap_event_building_refs(record: bytes, deleted_ids: set[int], old_to_new: dict[int, int]) -> bytes:
    if len(record) != 171:
        raise ValueError("EventData must be 171 bytes")
    out = bytearray(record)
    for off in (30, 31, 32):
        out[off] = remap_id(out[off], deleted_ids, old_to_new, deleted_value=0)
    return bytes(out)


def _artifact_assign_all(comp: list[tuple[int, int]], item_ids: list[int], catalog: dict) -> tuple[list[list[int]], list[int]]:
    assigned: list[list[int]] = [[] for _ in comp]
    leftovers: list[int] = []
    arts = catalog.get("artifacts", {})
    units = catalog.get("units", {})
    for item_id in item_ids:
        item = arts.get(str(item_id), {})
        choices: list[tuple[int, int, int]] = []
        for i, (uid, level) in enumerate(comp):
            if not army_runtime._artifact_compatible(units.get(str(uid), {}), item):
                continue
            if not army_runtime._artifact_slot_available(assigned[i], item, arts):
                continue
            before = army_runtime._artifact_assignment_strength(uid, level, assigned[i], 0, catalog)
            after = army_runtime._artifact_assignment_strength(uid, level, assigned[i] + [item_id], 0, catalog)
            choices.append((after - before, -i, i))
        if choices:
            _, _, idx = max(choices)
            assigned[idx].append(item_id)
        else:
            leftovers.append(item_id)
    return assigned, leftovers


def _garrison_unit_record(
    uid: int,
    level: int,
    item_ids: list[int],
    defence: int,
    catalog: dict,
    template: bytes | None = None,
    *,
    strict: bool = True,
) -> bytes:
    """Build one native garrison Unit record.

    Objects2 proves three legacy differences from an army Unit record:
    C-copy stats remain base/unmodified, A/B derived v1/v2 remain base-profile
    values, and building defence changes only the modified tactical strength
    plus byte +0x1B7. Artifacts still modify the A-copy stats.
    """
    buf = bytearray(
        army_runtime.make_unit_record(
            uid,
            level,
            False,
            0,
            0,
            item_ids,
            catalog,
            template=template,
            cold_added_unit=True,
            strict_derived=False,
        )
    )
    unit = catalog.get("units", {}).get(str(uid), {})
    if not unit:
        raise ValueError(f"Нет данных для гарнизонного юнита ID {uid}")
    profile = army_runtime._profile_for(uid, level, catalog)
    v1 = int(profile.get("v1", 0) or 0)
    v2 = int(profile.get("v2", 0) or 0)
    base_strength = profile.get("strength")
    profile_untested = army_runtime.catalog_profile_untested(uid, item_ids, 0, catalog)

    base = army_runtime._base_stats(unit, level)
    base_components = army_runtime.calculate_tactical_components(
        base, unit, unit.get("Bonus") or "", runtime_compat=True
    )
    if not v1:
        v1 = int(base_components["v1"])
    if not v2:
        v2 = int(base_components["v2"])
    base_formula = int(base_components["tactical_cost"])
    if strict and not profile_untested and (not profile or profile.get("strength") is None):
        raise ValueError(f"Гарнизонный юнит ID {uid}, уровень {level}: нет exact native profile")
    if base_strength is None:
        base_strength = base_formula
    with_defence = dict(base)
    with_defence["DefenceBlow"] += defence
    with_defence["DefenceShot"] += defence
    defence_formula = army_runtime.calculate_tactical_components(
        with_defence, unit, unit.get("Bonus") or "", runtime_compat=True
    )["tactical_cost"]
    if base_formula:
        modified_strength = round(int(base_strength) * defence_formula / base_formula)
    else:
        modified_strength = int(base_strength)

    # Garrison keeps base derived tails in A and B regardless of equipment.
    struct.pack_into("<II", buf, 0x0DD + 56, v1, v2)
    struct.pack_into("<II", buf, 0x11D + 56, v1, v2)
    # C-copy is fully base and its derived tail is cold/uninitialized.
    army_runtime._write_stat_block(
        buf,
        0x15D,
        unit,
        base,
        army_runtime.BONUS_CODES.get(unit.get("Bonus", ""), 0),
        0,
        0,
        True,
    )
    struct.pack_into("<II", buf, 0x1AA, int(base_strength), int(modified_strength))
    buf[0x1B7] = defence & 0xFF
    return bytes(buf)


def _native_comp_from_garrison(state: bytes) -> list[tuple[int, int]]:
    if len(state) != RUNTIME_GARRISON_SIZE:
        return []
    count = struct.unpack_from("<I", state, G_COUNT_OFF)[0]
    if count > G_UNIT_MAX:
        raise ValueError(f"Повреждён runtime-гарнизон: count={count}")
    out = []
    for i in range(count):
        u = state[G_UNIT_BASE_OFF + i * G_UNIT_STRIDE:G_UNIT_BASE_OFF + (i + 1) * G_UNIT_STRIDE]
        out.append((struct.unpack_from("<I", u, 0)[0] + 1, struct.unpack_from("<I", u, 0x10)[0]))
    return out


def synthesize_garrison_state(
    record: BuildingRecord,
    catalog: dict,
    source_state: bytes | None = None,
    old_record: BuildingRecord | None = None,
    *,
    strict: bool = True,
) -> bytes:
    if not record.runtime_garrison:
        return b""
    comp = record.garrison
    if len(comp) > G_UNIT_MAX:
        raise ValueError("В runtime-гарнизоне помещается не более 12 персонажей")

    base_state = bytearray(source_state if source_state and len(source_state) == RUNTIME_GARRISON_SIZE else b"\0" * RUNTIME_GARRISON_SIZE)
    old_native = _native_comp_from_garrison(source_state) if source_state else []
    if old_record is not None and source_state is not None:
        # Composition may be damaged/depleted only in runtime; refuse a semantic
        # rewrite if the source no longer corresponds to the original DTm.
        if old_native != old_record.garrison and old_record.garrison != record.garrison:
            raise ValueError("Текущий runtime-гарнизон уже отличается от исходной DTm; состав нельзя безопасно заменить")

    # Preserve matching unit templates/opaque progress where possible.
    old_records = []
    if source_state:
        for i in range(len(old_native)):
            old_records.append(source_state[G_UNIT_BASE_OFF + i * G_UNIT_STRIDE:G_UNIT_BASE_OFF + (i + 1) * G_UNIT_STRIDE])
    used: set[int] = set()

    static_items = record.static_artifacts if record.building_type == 12 else []
    item_dist, leftovers = _artifact_assign_all(comp, static_items, catalog) if static_items else ([[] for _ in comp], [])

    records: list[bytes] = []
    for idx, (uid, level) in enumerate(comp):
        chosen = None
        if idx < len(old_native) and old_native[idx] == (uid, level) and idx not in used:
            chosen = idx
        if chosen is None:
            for j, pair in enumerate(old_native):
                if j not in used and pair == (uid, level):
                    chosen = j
                    break
        template = old_records[chosen] if chosen is not None else None
        if chosen is not None:
            used.add(chosen)
        records.append(
            _garrison_unit_record(
                uid,
                level,
                item_dist[idx],
                record.additional_defence,
                catalog,
                template=template,
                strict=strict,
            )
        )

    # Clear semantic unit/aggregate area but preserve opaque far-tail state.
    base_state[G_COUNT_OFF:G_UNIT_BASE_OFF + G_UNIT_MAX * G_UNIT_STRIDE] = b"\0" * (G_UNIT_BASE_OFF + G_UNIT_MAX * G_UNIT_STRIDE)
    struct.pack_into("<I", base_state, G_COUNT_OFF, len(records))
    for i, u in enumerate(records):
        start = G_UNIT_BASE_OFF + i * G_UNIT_STRIDE
        base_state[start:start + G_UNIT_STRIDE] = u
    struct.pack_into("<I", base_state, G_AGG_STRENGTH_OFF, sum(struct.unpack_from("<I", u, 0x1AE)[0] for u in records))
    form = army_runtime._formation(records)
    for i, value in enumerate(form):
        struct.pack_into("<i", base_state, G_FORMATION_OFF + i * 4, value)
    for off, data in G_SENTINELS:
        base_state[off:off + len(data)] = data

    payment = sum(army_runtime._payment_guess(uid, level, catalog, strict=strict) for uid, level in comp)
    struct.pack_into("<I", base_state, G_PAYMENT_OFF, payment)
    hp = sum(max(0, army_runtime._base_stats(catalog["units"][str(uid)], level)["Hits"]) for uid, level in comp)
    struct.pack_into("<I", base_state, G_HP_SUM_OFF, hp)
    base_state[G_DEFENCE_OFF] = record.additional_defence & 0xFF
    struct.pack_into("<I", base_state, G_GOLD_INCOME_OFF, 0)
    for i in range(6):
        struct.pack_into("<I", base_state, G_RUINS_ITEM_OFF + 4 * i, 0)

    recruit_div = army_runtime._catalog_cost_recruit_div(catalog)
    total_cost = sum(int(catalog["units"][str(uid)].get("Cost", 0) or 0) for uid, _ in comp)
    struct.pack_into("<I", base_state, G_TOTAL_COST_OFF, round(total_cost / recruit_div) if recruit_div else 0)

    # Ruins: native Objects2 control stores max artifact price in the shifted
    # gold-income field and leftover, non-equipped loot as u32 IDs from +0x37B0.
    if record.building_type == 12:
        struct.pack_into("<I", base_state, G_GOLD_INCOME_OFF, struct.unpack_from("<H", record.raw, MAX_ARTIFACT_PRICE_OFF)[0])
        for i, item_id in enumerate(leftovers[:6]):
            struct.pack_into("<I", base_state, G_RUINS_ITEM_OFF + 4 * i, item_id)
    return bytes(base_state)


def verify_garrison_state(record: BuildingRecord, state: bytes, catalog: dict) -> list[str]:
    errors: list[str] = []
    if not record.runtime_garrison:
        return errors
    if len(state) != RUNTIME_GARRISON_SIZE:
        return [f"garrison size={len(state)}"]
    try:
        comp = _native_comp_from_garrison(state)
    except ValueError as exc:
        return [str(exc)]
    if comp != record.garrison:
        errors.append(f"composition {comp!r} != {record.garrison!r}")
    count = len(comp)
    records = [state[G_UNIT_BASE_OFF + i * G_UNIT_STRIDE:G_UNIT_BASE_OFF + (i + 1) * G_UNIT_STRIDE] for i in range(count)]
    strength = sum(struct.unpack_from("<I", u, 0x1AE)[0] for u in records)
    if struct.unpack_from("<I", state, G_AGG_STRENGTH_OFF)[0] != strength:
        errors.append("aggregate strength")
    if [struct.unpack_from("<i", state, G_FORMATION_OFF + i * 4)[0] for i in range(18)] != army_runtime._formation(records):
        errors.append("formation")
    if state[G_DEFENCE_OFF] != record.additional_defence:
        errors.append("additional defence")
    expected_payment = sum(army_runtime._payment_guess(uid, lvl, catalog, strict=True) for uid, lvl in comp)
    if struct.unpack_from("<I", state, G_PAYMENT_OFF)[0] != expected_payment:
        errors.append("payment")
    expected_hp = sum(army_runtime._base_stats(catalog["units"][str(uid)], lvl)["Hits"] for uid, lvl in comp)
    if struct.unpack_from("<I", state, G_HP_SUM_OFF)[0] != expected_hp:
        errors.append("hp sum")
    return errors


def _static_market_ids(record: BuildingRecord) -> list[int]:
    return [struct.unpack_from("<H", record.raw, ARTIFACTS_OFF + 2 * i)[0] for i in range(6)]


def _eligible_market_items(record: BuildingRecord, catalog: dict) -> list[int]:
    lo = struct.unpack_from("<H", record.raw, MIN_ARTIFACT_PRICE_OFF)[0]
    hi = struct.unpack_from("<H", record.raw, MAX_ARTIFACT_PRICE_OFF)[0]
    result: list[int] = []
    for key, item in catalog.get("artifacts", {}).items():
        try:
            item_id = int(key)
            cost = int(item.get("Cost", 0) or 0)
        except (TypeError, ValueError):
            continue
        if lo <= cost <= hi:
            result.append(item_id)
    return sorted(result)


def _deterministic_random_market(record: BuildingRecord, catalog: dict, count: int) -> list[int]:
    eligible = _eligible_market_items(record, catalog)
    if not eligible or count <= 0:
        return []
    # Native uses runtime RNG; exact stock is intentionally not claimed.  This
    # deterministic fallback creates valid functional stock in an already-live
    # SAV while making repeated QuestSync runs stable.
    seed_material = record.raw[:136] + record.raw[264:342] + record.raw[350:358]
    seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "little")
    rng = random.Random(seed)
    if count <= len(eligible):
        return rng.sample(eligible, count)
    return [rng.choice(eligible) for _ in range(count)]


def initialize_market_slots(record: BuildingRecord, catalog: dict) -> tuple[list[int], bool]:
    """Return twelve runtime int16 slots and whether native RNG was approximated."""
    slots = [0] * MARKET_RUNTIME_SLOT_COUNT
    static = _static_market_ids(record)
    for i, item_id in enumerate(static):
        if item_id:
            slots[i] = -int(item_id)
    random_count = min(MARKET_RUNTIME_SLOT_COUNT, record.raw[MARKET_COUNT_OFF])
    approximated = False
    if random_count:
        # Random stock is right-aligned by the native loader.  If a hand-picked
        # fixed item occupies one of those slots, never overwrite it: fixed DTm
        # semantics take precedence and the remaining random slots are filled.
        tail_positions = list(range(MARKET_RUNTIME_SLOT_COUNT - random_count, MARKET_RUNTIME_SLOT_COUNT))
        open_positions = [pos for pos in tail_positions if slots[pos] == 0]
        generated = _deterministic_random_market(record, catalog, len(open_positions))
        approximated = bool(generated)
        for pos, item_id in zip(open_positions, generated):
            slots[pos] = int(item_id)
    return slots, approximated


def _write_market_slots(core: bytearray, slots: Iterable[int]) -> None:
    vals = list(slots)[:MARKET_RUNTIME_SLOT_COUNT]
    vals += [0] * (MARKET_RUNTIME_SLOT_COUNT - len(vals))
    for i, value in enumerate(vals):
        struct.pack_into("<h", core, ARTIFACTS_OFF + 2 * i, int(value))


def _market_config_key(record: BuildingRecord) -> tuple:
    return (
        record.building_type,
        tuple(_static_market_ids(record)),
        record.raw[MARKET_COUNT_OFF],
        struct.unpack_from("<H", record.raw, MIN_ARTIFACT_PRICE_OFF)[0],
        struct.unpack_from("<H", record.raw, MAX_ARTIFACT_PRICE_OFF)[0],
    )



def _runtime_income_defaults(record: BuildingRecord) -> tuple[int, int]:
    """Native fresh current-income counters.

    Objects2 proves these counters are initialized from configured income for
    Village (type 2) only; Castle/Fort income fields do not seed +286/+352.
    """
    if record.building_type == 2:
        return (
            struct.unpack_from("<H", record.raw, GOLD_INCOME_OFF)[0],
            record.raw[MANA_INCOME_OFF],
        )
    return 0, 0

def synthesize_core(
    record: BuildingRecord,
    catalog: dict,
    template_core: bytes | None = None,
) -> tuple[bytes, bool]:
    core = bytearray(record.raw)
    rx, ry = record.runtime_coords
    struct.pack_into("<HH", core, 0, rx, ry)
    # Runtime-only fields never come from DTm.
    if template_core and len(template_core) == BUILDING_SIZE:
        # A template is used only for opaque/volatile loader state.  Runtime
        # income accumulators belong to the *new* building and must not inherit
        # another building's gameplay progress.
        core[RUNTIME_FLAG_OFF] = template_core[RUNTIME_FLAG_OFF]
        core[293] = template_core[293]
        core[RUNTIME_OPAQUE_OFF:RUNTIME_OPAQUE_END] = template_core[RUNTIME_OPAQUE_OFF:RUNTIME_OPAQUE_END]
    else:
        core[RUNTIME_FLAG_OFF] = 1

    # Fresh native buildings start their current accumulators at one income
    # tick.  This is independent of whichever opaque core supplied the loader
    # template above.
    current_gold, current_mana = _runtime_income_defaults(record)
    struct.pack_into("<H", core, RUNTIME_CURRENT_GOLD_OFF, current_gold)
    core[RUNTIME_CURRENT_MANA_OFF] = current_mana

    # Native all-types controls: fixed market list initializes marker 1; random
    # stock markets initialize a 721 refresh marker.
    if record.building_type in MARKET_TYPES:
        if record.raw[MARKET_COUNT_OFF]:
            struct.pack_into("<H", core, 346, 721)
        elif any(_static_market_ids(record)):
            struct.pack_into("<H", core, 346, 1)
        else:
            struct.pack_into("<H", core, 346, 0)

    approximated = False
    if record.building_type in MARKET_TYPES:
        slots, approximated = initialize_market_slots(record, catalog)
        _write_market_slots(core, slots)
    elif record.building_type == 12:
        # Ruins keep their literal positive loot IDs.
        core[ARTIFACTS_OFF:ARTIFACTS_OFF + 12] = record.raw[ARTIFACTS_OFF:ARTIFACTS_OFF + 12]
        core[ARTIFACTS_OFF + 12:ARTIFACTS_OFF + 24] = b"\0" * 12
    else:
        # Other BuildingData variants may reuse these editor bytes for their
        # own UI, but the native SAV runtime stock area is empty (Objects2
        # Altar control).
        core[ARTIFACTS_OFF:ARTIFACTS_OFF + 24] = b"\0" * 24
    return bytes(core), approximated


def merge_core(
    source_core: bytes,
    old_record: BuildingRecord,
    new_record: BuildingRecord,
    catalog: dict,
) -> tuple[bytes, bool]:
    if len(source_core) != BUILDING_SIZE:
        raise ValueError("runtime building core must be 358 bytes")
    core = bytearray(source_core)
    old = old_record.raw
    new = new_record.raw

    # Generic DTm semantic changes, excluding runtime-only/progress fields and
    # fields with special three-way representations below.
    special = set(range(0, 4))
    special.update(range(ARTIFACTS_OFF, ARTIFACTS_OFF + 24))
    special.update(range(RECRUITS_OFF, RECRUITS_OFF + 18))
    special.update(range(RUNTIME_CURRENT_GOLD_OFF, RUNTIME_CURRENT_GOLD_OFF + 2))
    special.add(RUNTIME_FLAG_OFF)
    special.add(OWNER_ARMY_OFF)
    special.add(293)
    special.update(range(RUNTIME_OPAQUE_OFF, RUNTIME_OPAQUE_END))
    special.add(RUNTIME_CURRENT_MANA_OFF)
    for i, (ov, nv) in enumerate(zip(old, new)):
        if i in special:
            continue
        if ov != nv:
            core[i] = nv

    if old[:4] != new[:4] or old[SIZE_X_OFF:SIZE_Y_OFF + 1] != new[SIZE_X_OFF:SIZE_Y_OFF + 1]:
        rx, ry = new_record.runtime_coords
        struct.pack_into("<HH", core, 0, rx, ry)

    # Runtime owner can reflect gameplay capture.  Preserve it unless editor
    # changed the desired owner in DTm.
    if old[OWNER_ARMY_OFF] != new[OWNER_ARMY_OFF]:
        core[OWNER_ARMY_OFF] = new[OWNER_ARMY_OFF]

    # Recruitment current amount is gameplay state.  Keep it when the editor
    # only changes unrelated fields; explicit amount edits reset that slot.
    for slot in range(6):
        off = RECRUITS_OFF + 3 * slot
        old_id, old_amount, old_max = old[off:off + 3]
        new_id, new_amount, new_max = new[off:off + 3]
        sav_id, sav_amount, sav_max = core[off:off + 3]
        if (old_id, old_amount, old_max) == (new_id, new_amount, new_max):
            continue
        if old_id != new_id:
            core[off:off + 3] = bytes((new_id, new_amount, new_max))
        else:
            amount = new_amount if old_amount != new_amount else min(sav_amount, new_max)
            core[off:off + 3] = bytes((new_id, amount, new_max))

    # Income accumulators initialize to one income tick when untouched, but
    # preserve accumulated gameplay state otherwise.
    old_gold_default, old_mana_default = _runtime_income_defaults(old_record)
    new_gold_default, new_mana_default = _runtime_income_defaults(new_record)
    current_gold = struct.unpack_from("<H", core, RUNTIME_CURRENT_GOLD_OFF)[0]
    if (old_gold_default, old_mana_default) != (new_gold_default, new_mana_default):
        if current_gold == old_gold_default:
            struct.pack_into("<H", core, RUNTIME_CURRENT_GOLD_OFF, new_gold_default)
        if core[RUNTIME_CURRENT_MANA_OFF] == old_mana_default:
            core[RUNTIME_CURRENT_MANA_OFF] = new_mana_default

    approximated = False
    if new_record.building_type in MARKET_TYPES:
        if _market_config_key(old_record) != _market_config_key(new_record):
            slots, approximated = initialize_market_slots(new_record, catalog)
            _write_market_slots(core, slots)
            if new_record.raw[MARKET_COUNT_OFF]:
                struct.pack_into("<H", core, 346, 721)
            elif any(_static_market_ids(new_record)):
                struct.pack_into("<H", core, 346, 1)
            else:
                struct.pack_into("<H", core, 346, 0)
    else:
        artifact_semantics_changed = (
            old_record.building_type != new_record.building_type
            or old[ARTIFACTS_OFF:ARTIFACTS_OFF + 12] != new[ARTIFACTS_OFF:ARTIFACTS_OFF + 12]
        )
        if artifact_semantics_changed:
            if new_record.building_type == 12:
                # Ruins keep literal positive loot IDs.
                core[ARTIFACTS_OFF:ARTIFACTS_OFF + 12] = new[ARTIFACTS_OFF:ARTIFACTS_OFF + 12]
                core[ARTIFACTS_OFF + 12:ARTIFACTS_OFF + 24] = b"\0" * 12
            else:
                # Other variants do not expose this region as runtime stock.
                core[ARTIFACTS_OFF:ARTIFACTS_OFF + 24] = b"\0" * 24
        if old_record.building_type in MARKET_TYPES:
            struct.pack_into("<H", core, 346, 0)

    return bytes(core), approximated


def extract_runtime_entries(payload: bytes, relationship: dict, old_records: list[BuildingRecord]) -> list[tuple[bytes, bytes]]:
    offsets = relationship["buildings"]["record_offsets"]
    state_indices = set(relationship["buildings"]["runtime_garrison_building_indices"])
    result: list[tuple[bytes, bytes]] = []
    for i, _record in enumerate(old_records):
        off = offsets[i]
        core = bytes(payload[off:off + BUILDING_SIZE])
        state = bytes(payload[off + BUILDING_SIZE:off + BUILDING_SIZE + RUNTIME_GARRISON_SIZE]) if i in state_indices else b""
        result.append((core, state))
    return result


def choose_template_core(new_record: BuildingRecord, source_records: list[BuildingRecord], source_entries: list[tuple[bytes, bytes]]) -> bytes | None:
    tiers = (
        lambda r: (r.building_type, r.picture_variant, r.picture_number, r.size_x, r.size_y) == (new_record.building_type, new_record.picture_variant, new_record.picture_number, new_record.size_x, new_record.size_y),
        lambda r: (r.building_type, r.picture_variant, r.picture_number) == (new_record.building_type, new_record.picture_variant, new_record.picture_number),
        lambda r: r.building_type == new_record.building_type,
    )
    for predicate in tiers:
        for r, (core, _state) in zip(source_records, source_entries):
            if predicate(r):
                return core
    return None



def remap_runtime_start_buildings(
    tail: bytes,
    old_army_records: list[army_runtime.ArmyRecord],
    new_army_records: list[army_runtime.ArmyRecord],
    army_mapping: list[int | None],
    deleted_building_ids: set[int],
    old_to_new_building: dict[int, int],
) -> bytes:
    """Remap current/start-building references in hero and mapped NPC runtime slots.

    The hero slot has no DTm ArmyData peer, so its current byte is always
    structurally remapped.  For NPCs we remap only when the editor did not
    explicitly change ArmyData.start_building_id; explicit target semantics are
    already handled by army_runtime.patch_direct_fields().
    """
    need = (len(new_army_records) + 1) * army_runtime.RUNTIME_ARMY_SIZE
    if len(tail) < need:
        raise ValueError("Динамический хвост SAV короче массива runtime-армий")
    out = bytearray(tail)

    # Hero/current player runtime slot.
    hero_off = army_runtime.START_BUILDING_OFF
    out[hero_off] = remap_id(
        out[hero_off], deleted_building_ids, old_to_new_building, deleted_value=0
    )

    for target, new_rec in enumerate(new_army_records):
        source = army_mapping[target] if target < len(army_mapping) else None
        if source is None:
            continue
        old_rec = old_army_records[source]
        if old_rec.start_building_id != new_rec.start_building_id:
            continue
        off = (target + 1) * army_runtime.RUNTIME_ARMY_SIZE + army_runtime.START_BUILDING_OFF
        out[off] = remap_id(
            out[off], deleted_building_ids, old_to_new_building, deleted_value=0
        )
    return bytes(out)


def split_runtime_blob(blob: bytes, records: list[BuildingRecord]) -> list[tuple[bytes, bytes]]:
    """Split a rebuilt building region into (core, optional attached state)."""
    pos = 0
    out: list[tuple[bytes, bytes]] = []
    for record in records:
        end = pos + BUILDING_SIZE
        if end > len(blob):
            raise ValueError("runtime building blob is truncated")
        core = blob[pos:end]
        pos = end
        state = b""
        if record.runtime_garrison:
            end = pos + RUNTIME_GARRISON_SIZE
            if end > len(blob):
                raise ValueError("runtime garrison block is truncated")
            state = blob[pos:end]
            pos = end
        out.append((core, state))
    if pos != len(blob):
        raise ValueError(f"runtime building blob has {len(blob) - pos} trailing bytes")
    return out


def verify_runtime_blob(
    blob: bytes,
    records: list[BuildingRecord],
    catalog: dict,
    *,
    rebuilt_garrison_ids: Iterable[int] = (),
) -> dict:
    """Strict structural verifier for a generated runtime building region."""
    issues: list[str] = []
    rebuilt = set(int(x) for x in rebuilt_garrison_ids)
    try:
        entries = split_runtime_blob(blob, records)
    except ValueError as exc:
        return {"ok": False, "issues": [str(exc)]}

    for index, (record, (core, state)) in enumerate(zip(records, entries), 1):
        rx, ry = record.runtime_coords
        if struct.unpack_from("<HH", core, 0) != (rx, ry):
            issues.append(f"building {index}: runtime coords")
        for off in (PICTURE_NUMBER_OFF, PICTURE_VARIANT_OFF, TYPE_OFF, SIZE_X_OFF, SIZE_Y_OFF):
            if core[off] != record.raw[off]:
                issues.append(f"building {index}: semantic byte +0x{off:X}")
        if index in rebuilt:
            errors = verify_garrison_state(record, state, catalog)
            issues.extend(f"building {index}: {item}" for item in errors)
    return {"ok": not issues, "issues": issues}

def build_runtime_blob(
    payload: bytes,
    relationship: dict,
    old_raw_records: list[bytes],
    new_raw_records: list[bytes],
    mapping: list[int | None],
    catalog: dict,
    *,
    strict: bool = True,
    deleted_army_ids: set[int] | None = None,
    old_to_new_army: dict[int, int] | None = None,
) -> tuple[bytes, dict]:
    old_records = [parse_building_record(x) for x in old_raw_records]
    new_records = [parse_building_record(x) for x in new_raw_records]
    source_entries = extract_runtime_entries(payload, relationship, old_records)
    parts: list[bytes] = []
    approximated_markets: list[int] = []
    garrison_rebuilds: list[int] = []
    for target, new_record in enumerate(new_records):
        source = mapping[target] if target < len(mapping) else None
        if source is None:
            template_core = choose_template_core(new_record, old_records, source_entries)
            core, approx = synthesize_core(new_record, catalog, template_core=template_core)
            state = synthesize_garrison_state(new_record, catalog, strict=strict) if new_record.runtime_garrison else b""
            if state:
                garrison_rebuilds.append(target + 1)
        else:
            old_record = old_records[source]
            source_core, source_state = source_entries[source]
            core, approx = merge_core(source_core, old_record, new_record, catalog)
            if new_record.runtime_garrison:
                garrison_changed = (
                    old_record.building_type != new_record.building_type
                    or old_record.raw[GARRISON_OFF:GARRISON_OFF + 19] != new_record.raw[GARRISON_OFF:GARRISON_OFF + 19]
                    or (new_record.building_type == 12 and old_record.raw[ARTIFACTS_OFF:ARTIFACTS_OFF + 12] != new_record.raw[ARTIFACTS_OFF:ARTIFACTS_OFF + 12])
                    or (new_record.building_type == 12 and old_record.raw[MAX_ARTIFACT_PRICE_OFF:MAX_ARTIFACT_PRICE_OFF + 2] != new_record.raw[MAX_ARTIFACT_PRICE_OFF:MAX_ARTIFACT_PRICE_OFF + 2])
                )
                if source_state and not garrison_changed:
                    state = source_state
                else:
                    state = synthesize_garrison_state(
                        new_record,
                        catalog,
                        source_state=source_state or None,
                        old_record=old_record if source_state else None,
                        strict=strict,
                    )
                    garrison_rebuilds.append(target + 1)
            else:
                state = b""
        if source is not None and deleted_army_ids is not None and old_to_new_army is not None:
            old_record = old_records[source]
            # Only remap gameplay/runtime owner when the editor did not
            # explicitly change the owner in the target DTm.
            if old_record.raw[OWNER_ARMY_OFF] == new_record.raw[OWNER_ARMY_OFF]:
                core_mut = bytearray(core)
                core_mut[OWNER_ARMY_OFF] = remap_id(
                    core_mut[OWNER_ARMY_OFF],
                    deleted_army_ids,
                    old_to_new_army,
                    deleted_value=0xFF,
                )
                core = bytes(core_mut)
        if approx:
            approximated_markets.append(target + 1)
        parts.append(core + state)
    return b"".join(parts), {
        "market_runtime_rng_approximated_building_ids": approximated_markets,
        "garrison_rebuilt_building_ids": garrison_rebuilds,
    }
