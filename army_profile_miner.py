from __future__ import annotations

import argparse
import json
import struct
from collections import defaultdict
from pathlib import Path

import army_runtime as ar
import sav_tool


def _decode_unit(u: bytes) -> dict:
    raw_spell = struct.unpack_from('<I', u, 0x24)[0]
    items = tuple(struct.unpack_from('<I', u, off)[0] for off in (0xCD, 0xD1, 0xD5))
    return {
        'uid': struct.unpack_from('<I', u, 0)[0] + 1,
        'level': struct.unpack_from('<I', u, 0x10)[0],
        'named_id': struct.unpack_from('<I', u, 0x14)[0],
        'spell_id': 0 if raw_spell == 0xFFFFFFFF else raw_spell + 1,
        'items': list(items),
        'is_main': u[0x19D] == 0,
        'v1': struct.unpack_from('<I', u, 0x11D + 56)[0],
        'v2': struct.unpack_from('<I', u, 0x11D + 60)[0],
        'base_strength': struct.unpack_from('<I', u, 0x1AA)[0],
        'modified_strength': struct.unpack_from('<I', u, 0x1AE)[0],
        'class': struct.unpack_from('<I', u, 0x1B2)[0],
        'base_stats_hex': u[0x11D:0x15D].hex(),
        'modified_stats_hex': u[0x0DD:0x11D].hex(),
    }


def scan(save_path: Path, dtm_path: Path) -> dict:
    raw = save_path.read_bytes()
    payload, outer = sav_tool.unpack_sav_bytes(raw)
    inner = sav_tool.read_dtm(dtm_path)
    relation = sav_tool.relate(payload, inner, outer)
    sections, _ = sav_tool.dtm_layout(inner)
    army_count = sections['armies']['count']
    dtm_recs = [
        ar.parse_army_record(inner[sections['armies']['offset'] + i * ar.ARMY_RECORD_SIZE:
                                   sections['armies']['offset'] + (i + 1) * ar.ARMY_RECORD_SIZE])
        for i in range(army_count)
    ]
    tail_off = relation['relationship']['dynamic_tail']['offset']
    tail = payload[tail_off:]
    need = (army_count + 1) * ar.RUNTIME_ARMY_SIZE
    if len(tail) < need:
        raise ValueError(f'dynamic tail too short: {len(tail)} < {need}')

    observations = []
    payment_candidates = defaultdict(set)
    leader_code_candidates = defaultdict(set)
    army_code_candidates = defaultdict(set)
    army_reports = []
    for ai, rec in enumerate(dtm_recs):
        a = (ai + 1) * ar.RUNTIME_ARMY_SIZE
        block = tail[a:a + ar.RUNTIME_ARMY_SIZE]
        # Packed runtime field +0x1E9D is leader-dependent.  Across native
        # controls it is stable by ordinary (non-named) main Unit ID, while a
        # named unit can legitimately override it.  Mine it as native evidence
        # rather than guessing a formula.
        runtime_code = struct.unpack_from('<I', block, ar.LEADER_RUNTIME_CODE_OFF)[0]
        if rec.main_id and not rec.named_unit_id:
            # Keep the old per-main-unit view as diagnostic evidence only.
            # It is intentionally not trusted by production synthesis because
            # the same main Unit ID can have different values in other army
            # contexts.
            leader_code_candidates[rec.main_id].add(runtime_code)
        context_key = ar._army_runtime_context_key(rec)
        if context_key:
            army_code_candidates[context_key].add(runtime_code)
        report = ar.verify_runtime_army_block(block, ar.load_catalog())
        army_reports.append({'army_id': ai + 1, **report})
        cnt = report.get('unit_count', 0)
        paid_keys = []
        for ui in range(cnt):
            u = block[ar.UNIT_BASE_OFF + ui * ar.UNIT_STRIDE:ar.UNIT_BASE_OFF + (ui + 1) * ar.UNIT_STRIDE]
            item = _decode_unit(u)
            item['army_id'] = ai + 1
            item['unit_index'] = ui
            observations.append(item)
            if not item['is_main']:
                paid_keys.append((item['uid'], item['level']))
        # Payment is provable for homogeneous paid troops when the DTm has no
        # units-without-money exemption. Mixed equations are intentionally not
        # guessed here.
        if not rec.units_without_money and paid_keys and len(set(paid_keys)) == 1:
            total = struct.unpack_from('<I', block, ar.PAYMENT_OFF)[0]
            if total % len(paid_keys) == 0:
                payment_candidates[paid_keys[0]].add(total // len(paid_keys))

    clean = defaultdict(lambda: defaultdict(set))
    combos = defaultdict(lambda: defaultdict(set))
    for o in observations:
        key = (o['uid'], o['level'])
        sem = (tuple(o['items']), o['spell_id'])
        modified_block = bytes.fromhex(o['modified_stats_hex'])
        mv1, mv2 = struct.unpack_from('<II', modified_block, 56)
        combos[(key, sem)]['v1'].add(mv1)
        combos[(key, sem)]['v2'].add(mv2)
        combos[(key, sem)]['strength'].add(o['modified_strength'])
        # The B/base runtime copy is independent of equipped artifacts and
        # applied spell.  Therefore every observation is valid evidence for
        # base v1/v2/strength/class, not only semantically clean units.
        clean[key]['v1'].add(o['v1'])
        clean[key]['v2'].add(o['v2'])
        clean[key]['strength'].add(o['base_strength'])
        clean[key]['class'].add(o['class'])

    exact_profiles = {}
    conflicts = {}
    for key, fields in clean.items():
        out = {}
        bad = {}
        for name, values in fields.items():
            if len(values) == 1:
                out[name] = next(iter(values))
            elif values:
                bad[name] = sorted(values)
        # Daily payment is formula-derived (CalculateUpkeep) and does not
        # belong in per-level native profiles.
        k = f'{key[0]}:{key[1]}'
        if out:
            exact_profiles[k] = out
        if bad:
            conflicts[k] = bad

    mod_strength = {}
    mod_profiles = {}
    mod_conflicts = {}
    for (key, sem), fields in combos.items():
        items, spell = sem
        if not any(items) and not spell:
            continue
        k = f'{key[0]}:{key[1]}:{",".join(map(str, items))}:{spell}'
        out = {}; bad = {}
        for name, values in fields.items():
            if len(values) == 1:
                out[name] = next(iter(values))
            elif values:
                bad[name] = sorted(values)
        if out:
            mod_profiles[k] = out
            if 'strength' in out:
                mod_strength[k] = out['strength']
        if bad:
            mod_conflicts[k] = bad

    leader_codes = {}
    leader_code_conflicts = {}
    for uid, values in leader_code_candidates.items():
        if len(values) == 1:
            leader_codes[str(uid)] = next(iter(values))
        elif values:
            leader_code_conflicts[str(uid)] = sorted(values)

    army_codes = {}
    army_code_conflicts = {}
    for key, values in army_code_candidates.items():
        if len(values) == 1:
            army_codes[key] = next(iter(values))
        elif values:
            army_code_conflicts[key] = sorted(values)

    catalog = ar.load_catalog()
    observed_ids = sorted({o['uid'] for o in observations})
    return {
        'schema': 1,
        'source': {
            'sav': str(save_path),
            'dtm': str(dtm_path),
            'sav_sha256': outer['sha256'],
            'payload_sha256': outer['payload_sha256'],
            'dtm_sha256': sav_tool.sha256(inner),
            'army_count': army_count,
        },
        'coverage': {
            'catalog_unit_count': len(catalog.get('units', {})),
            'observed_unit_ids': observed_ids,
            'observed_unit_id_count': len(observed_ids),
            'clean_exact_profile_count': len(exact_profiles),
        },
        'profiles': exact_profiles,
        'modified_strengths': mod_strength,
        'modified_profiles': mod_profiles,
        'leader_runtime_codes': leader_codes,
        'leader_runtime_code_conflicts': leader_code_conflicts,
        'army_runtime_codes': army_codes,
        'army_runtime_code_conflicts': army_code_conflicts,
        'conflicts': conflicts,
        'modified_strength_conflicts': mod_conflicts,
        'army_verifier': army_reports,
        'observations': observations,
    }


def merge_reports(reports: list[dict]) -> dict:
    profile_values = defaultdict(lambda: defaultdict(set))
    mod_values = defaultdict(set)
    mod_profile_values = defaultdict(lambda: defaultdict(set))
    leader_code_values = defaultdict(set)
    army_code_values = defaultdict(set)
    sources = []
    observed = set()
    for report in reports:
        sources.append(report.get('source', {}))
        observed.update(report.get('coverage', {}).get('observed_unit_ids', []))
        for key, p in report.get('profiles', {}).items():
            for field, value in p.items():
                profile_values[key][field].add(value)
        for key, value in report.get('modified_strengths', {}).items():
            mod_values[key].add(value)
        for key, profile in report.get('modified_profiles', {}).items():
            if isinstance(profile, dict):
                for field, value in profile.items():
                    mod_profile_values[key][field].add(value)
        for key, value in report.get('leader_runtime_codes', {}).items():
            leader_code_values[str(key)].add(int(value))
        # Preserve conflict evidence from an individual scan.  Otherwise a
        # later report with one value could accidentally make a previously
        # contradictory leader look exact.
        for key, values in report.get('leader_runtime_code_conflicts', {}).items():
            for value in values:
                leader_code_values[str(key)].add(int(value))
        for key, value in report.get('army_runtime_codes', {}).items():
            army_code_values[str(key)].add(int(value))
        for key, values in report.get('army_runtime_code_conflicts', {}).items():
            for value in values:
                army_code_values[str(key)].add(int(value))

    profiles = {}; conflicts = {}
    for key, fields in profile_values.items():
        ok = {}; bad = {}
        for field, values in fields.items():
            if len(values) == 1: ok[field] = next(iter(values))
            else: bad[field] = sorted(values)
        if ok: profiles[key] = ok
        if bad: conflicts[key] = bad
    mods = {}; mod_conflicts = {}
    for key, values in mod_values.items():
        if len(values) == 1: mods[key] = next(iter(values))
        else: mod_conflicts[key] = sorted(values)
    modified_profiles = {}; modified_profile_conflicts = {}
    for key, fields in mod_profile_values.items():
        ok = {}; bad = {}
        for field, values in fields.items():
            if len(values) == 1: ok[field] = next(iter(values))
            else: bad[field] = sorted(values)
        if ok: modified_profiles[key] = ok
        if bad: modified_profile_conflicts[key] = bad
    leader_codes = {}; leader_code_conflicts = {}
    for key, values in leader_code_values.items():
        if len(values) == 1: leader_codes[key] = next(iter(values))
        else: leader_code_conflicts[key] = sorted(values)
    army_codes = {}; army_code_conflicts = {}
    for key, values in army_code_values.items():
        if len(values) == 1: army_codes[key] = next(iter(values))
        else: army_code_conflicts[key] = sorted(values)
    return {
        'schema': 1,
        'sources': sources,
        'coverage': {
            'catalog_unit_count': 102,
            'observed_unit_ids': sorted(observed),
            'observed_unit_id_count': len(observed),
            'clean_exact_profile_count': len(profiles),
        },
        'profiles': profiles,
        'modified_strengths': mods,
        'modified_profiles': modified_profiles,
        'leader_runtime_codes': leader_codes,
        'leader_runtime_code_conflicts': leader_code_conflicts,
        'army_runtime_codes': army_codes,
        'army_runtime_code_conflicts': army_code_conflicts,
        'conflicts': conflicts,
        'modified_strength_conflicts': mod_conflicts,
        'modified_profile_conflicts': modified_profile_conflicts,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='Mine exact Discord Times army runtime profiles from native SAV/DTm pairs.')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('scan', help='scan one native SAV/DTm pair')
    p.add_argument('sav', type=Path)
    p.add_argument('dtm', type=Path)
    p.add_argument('-o', '--output', type=Path)
    p = sub.add_parser('merge', help='merge scan JSON files and retain only conflict-free fields')
    p.add_argument('reports', type=Path, nargs='+')
    p.add_argument('-o', '--output', type=Path, required=True)
    args = ap.parse_args()
    if args.cmd == 'scan':
        result = scan(args.sav, args.dtm)
        text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output: args.output.write_text(text, encoding='utf-8')
        else: print(text, end='')
    else:
        result = merge_reports([json.loads(p.read_text(encoding='utf-8')) for p in args.reports])
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
