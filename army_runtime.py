from __future__ import annotations

import json, math, struct, sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ARMY_RECORD_SIZE = 89
RUNTIME_ARMY_SIZE = 14375
UNIT_STRIDE = 475
UNIT_COUNT_OFF = 0x0800
UNIT_BASE_OFF = 0x0804
UNIT_AREA_END = 0x1E48
FORMATION_OFF = 0x1E4C
AGG_STRENGTH_OFF = 0x1E48
ACTIVE_OFF = 0x1EA1
GROUP_OFF = 0x1EAB
RELATIONS_OFF = 0x1EAF
START_BUILDING_OFF = 0x1EB3
PATROL_EXISTS_OFF = 0x1EBB
PATROL_RADIUS_OFF = 0x1EBC
PATROL_MODE_OFF = 0x1F10
LEADER_RUNTIME_CODE_OFF = 0x1E9D
AGGRESSION_OFF = 0x1ED0
REVIVE_OFF = 0x1ED4
GOLD_INCOME_OFF = 0x1ED8
PAYMENT_OFF = 0x1EE0
HP_SUM_OFF = 0x1EF0
HOME_X_OFF = 0x1F1C
HOME_Y_OFF = 0x1F20
CURRENT_X_OFF = 0x1F24
CURRENT_Y_OFF = 0x1F28
SLOT_AI_OFF = 0x7DF
PREV_COST_OFF = 0x7E3
PREV_XP_CORR_OFF = 0x7FC
SLOT_POINTER_OFF = 0x74B

@dataclass(frozen=True)
class ArmyRuntimeVersionProfile:
    name: str
    map_magic: bytes
    army_record_size: int
    runtime_army_size: int
    unit_stride: int
    max_units: int
    unit_count_off: int
    unit_base_off: int


LEGACY_V4_PROFILE = ArmyRuntimeVersionProfile(
    name="MapLDV V.4 / AEpf legacy runtime",
    map_magic=b"MapLDV V.4\r\n",
    army_record_size=ARMY_RECORD_SIZE,
    runtime_army_size=RUNTIME_ARMY_SIZE,
    unit_stride=UNIT_STRIDE,
    max_units=12,
    unit_count_off=UNIT_COUNT_OFF,
    unit_base_off=UNIT_BASE_OFF,
)
SUPPORTED_RUNTIME_PROFILES = {LEGACY_V4_PROFILE.map_magic: LEGACY_V4_PROFILE}

_ACTIVE_CATALOG: dict | None = None

def set_active_catalog(catalog: dict | None) -> None:
    global _ACTIVE_CATALOG
    _ACTIVE_CATALOG = catalog

def clear_active_catalog() -> None:
    set_active_catalog(None)

def _compat_ids(catalog: dict | None, key: str) -> set[int]:
    raw = (catalog or {}).get('_compat', {}).get(key, ())
    try:
        return {int(v) for v in raw}
    except (TypeError, ValueError):
        return set()

def catalog_profile_untested(uid: int, item_ids: Iterable[int] = (), spell_id: int = 0, catalog: dict | None = None) -> bool:
    catalog = catalog or {}
    if uid in _compat_ids(catalog, 'changed_units'):
        return True
    changed_items = _compat_ids(catalog, 'changed_artifacts')
    if any(int(i) in changed_items for i in item_ids if i):
        return True
    return bool(spell_id and int(spell_id) in _compat_ids(catalog, 'changed_spells'))

# Exhaustive army-ID bytes in the 171-byte EventData record for MapLDV V.4.
# Names follow the legacy EventData layout; keeping names here makes the proof
# auditable instead of relying on an opaque tuple of offsets.
EVENT_ARMY_REF_FIELDS = {
    15: "army_unactive_id",
    16: "army_id_change_patrol",
    54: "army_defeat_id[0]",
    55: "army_defeat_id[1]",
    67: "defeat_army_id[0]",
    68: "defeat_army_id[1]",
    74: "army_meet_id",
    75: "army_active_id",
    121: "army_activate_id[0]",
    122: "army_activate_id[1]",
    123: "army_deactivate_id",
    136: "army_unit_leave_id",
    142: "army_from_unit_leave_id",
    144: "shown_army_id",
    146: "army_in_native_building_id",
    147: "army_from_start_fight_id",
}
EVENT_ARMY_REF_OFFSETS = tuple(EVENT_ARMY_REF_FIELDS)
FORMATION_ENTRIES = (0,1,2,3,4,5,7,8,9,10,14,15)
FORMATION_SENTINELS = (6,11,12,13,16,17)
BUILDING_OWNER_ARMY_OFF = 292
CELL_ARMY_KIND = 18

# Empirically exact profiles needed by the supplied regression maps.  Other
# units reuse a same-id/level native record from the source SAV when possible.
PROFILE = {
    # Legacy hand-certified v1/v2/strength values.  Daily payment is no longer
    # stored here: it is calculated exactly by calculate_upkeep() from Cost,
    # CostGoldDiv and CostRecruitDiv, and does not depend on unit level.
    (4,0): {'v1':27,'v2':139,'strength':63},
    (4,1): {'v1':33,'v2':160,'strength':86},
    (19,0): {'v1':65,'v2':148,'strength':156},
    (24,0): {'v1':138,'v2':253,'strength':562},
}
MOD_STRENGTH = {
    (4,1,(18,76),18): 144,
    (4,0,(14,),18): 102,
    (19,0,(),18): 149,
}
BONUS_CODES = {
    # Native runtime bonus byte (stat-block +55), proven by the 102-unit corpus.
    'SpearDefense':1, 'HorseAtack':2, 'ArmorIgnore':3, 'ArmyMedic':4,
    'Merchant':5, 'DeathCurse':6, 'GodAnger':7, 'GodStrike':8,
    'Unvulnerabe':9, 'VampirsGist':10, 'OldVampirsGist':11,
    'Evasive':12, 'Ghost':13, 'Artillery':14, 'Garrison':15,
    'Poison':17, 'Dead':18, 'FastDead':19, 'Counterblow':20,
}
NATURE_CODES = {'Undead':1, 'Elemental':2, 'Rogue':3, 'Animal':4, 'Hero':5, 'People':0, 'Mecha':6}
MAGIC_CODES = {'LifeMagic':1,'ElementalMagic':2,'DeathMagic':3}
MAGIC_DIR_CODES = {'ToAll':0,'ToEnemy':1,'ToAlly':2,'CurseOnly':3,'StrikeOnly':4,'BlessOnly':5,'CureOnly':6}

STAT_FIELDS = {
    'Hits': 1, 'AttackBlow':5, 'DefenceBlow':9, 'AttackShot':13,
    'DefenceShot':17, 'MagicPower':22, 'ProtectLife':27,
    'ProtectDeath':31, 'ProtectElemental':35, 'Regen':39,
    'Vampirizm':43, 'Initiative':47, 'Manevres':51,
}
PERCENT_POINT_STATS = {'ProtectLife','ProtectDeath','ProtectElemental','Regen','Vampirizm'}

@dataclass(frozen=True)
class ArmyRecord:
    raw: bytes
    x:int; y:int; army_id:int; map_model:int; tactic_cost:int; speed_correction:int
    xp_like_player:int; gold_income:int; xp_add:int; start_building_id:int
    main_id:int; main_level:int; troops:tuple[tuple[int,int,int],...]
    items:tuple[int,int,int]; named_unit_id:int; patrol_exists:int; patrol_radius:int
    units_without_money:int; activity:int; group_type:int; relations:tuple[int,int,int,int]
    aggression:int; revive_time:int; xp_correction:int; ship_type:int
    tactic_cost_part2:int; ignores_ai:int; goes_towards_player:int
    forbid_random_targets:int; forbid_talks:int; known:int; not_interested_buildings:int
    garrison_power:int; revive_everyone:int; applied_spell:int; action_model:int

    def expanded(self):
        out=[]
        if self.main_id:
            out.append((self.main_id,self.main_level,True))
        for uid,lvl,amount in self.troops:
            out.extend((uid,lvl,False) for _ in range(amount))
        return out


def parse_army_record(raw:bytes)->ArmyRecord:
    if len(raw)!=89: raise ValueError('ArmyData must be 89 bytes')
    troops=tuple(tuple(raw[0x1C+i*3:0x1F+i*3]) for i in range(6))
    return ArmyRecord(
        raw, *struct.unpack_from('<HH',raw,0), raw[4],raw[5],struct.unpack_from('<H',raw,6)[0],raw[0x0D],raw[0x0E],
        struct.unpack_from('<H',raw,0x11)[0],struct.unpack_from('<H',raw,0x13)[0],raw[0x19],raw[0x1A],raw[0x1B],troops,
        tuple(raw[0x32:0x35]),raw[0x3A],raw[0x3C],raw[0x3D],raw[0x3E],raw[0x3F],raw[0x40],tuple(raw[0x41:0x45]),
        raw[0x45],raw[0x46],raw[0x47],raw[0x48],struct.unpack_from('<H',raw,0x4A)[0],raw[0x4C],raw[0x4D],raw[0x4E],raw[0x4F],
        raw[0x50],raw[0x51],raw[0x52],raw[0x53],raw[0x54],raw[0x55]
    )


def _sig(raw:bytes)->bytes:
    # ID is renumbered by the editor after an insertion/deletion.
    return raw[:4] + raw[5:]


def _lcs_exact_anchors(old_records:list[bytes], new_records:list[bytes]):
    """Return monotonic exact-signature anchors as (old_index, new_index).

    Army byte 4 (ID) is intentionally ignored by _sig(), because the editor
    renumbers it after insertion/deletion.  An LCS provides stable structural
    anchors and, unlike a pure edit-distance substitution pass, can distinguish
    a same-count delete+insert from an in-place modification.
    """
    a=[_sig(x) for x in old_records]; b=[_sig(x) for x in new_records]
    n,m=len(a),len(b)
    dp=[[0]*(m+1) for _ in range(n+1)]
    for i in range(n-1,-1,-1):
        for j in range(m-1,-1,-1):
            if a[i]==b[j]:
                dp[i][j]=1+dp[i+1][j+1]
            else:
                dp[i][j]=max(dp[i+1][j],dp[i][j+1])
    anchors=[]; i=j=0
    while i<n and j<m:
        if a[i]==b[j] and dp[i][j]==1+dp[i+1][j+1]:
            anchors.append((i,j)); i+=1; j+=1
        elif dp[i+1][j]>=dp[i][j+1]:
            i+=1
        else:
            j+=1
    return anchors


def _align_gap(old_records:list[bytes], new_records:list[bytes], old_base:int, new_base:int):
    """Align one gap between exact anchors.

    Equal-sized gaps are treated as in-place edits.  Unequal gaps use a small
    edit-distance DP where a very similar record is cheaper to substitute, but
    unrelated records prefer delete+add.
    """
    n,m=len(old_records),len(new_records)
    if not n:
        return [None]*m, list(range(old_base, old_base+n))
    if not m:
        return [], list(range(old_base, old_base+n))
    if n==m:
        return [old_base+i for i in range(m)], []
    INF=10**9
    dp=[[INF]*(m+1) for _ in range(n+1)]
    prev=[[None]*(m+1) for _ in range(n+1)]
    dp[0][0]=0
    for i in range(n+1):
        for j in range(m+1):
            v=dp[i][j]
            if v>=INF: continue
            if i<n and v+3<dp[i+1][j]:
                dp[i+1][j]=v+3; prev[i+1][j]=(i,j,'del')
            if j<m and v+3<dp[i][j+1]:
                dp[i][j+1]=v+3; prev[i][j+1]=(i,j,'add')
            if i<n and j<m:
                aa,bb=_sig(old_records[i]),_sig(new_records[j])
                h=sum(x!=y for x,y in zip(aa,bb))
                # exact/small semantic edits map; a mostly unrelated pair costs
                # more than delete+add and is therefore structural.
                cost=0 if aa==bb else min(8, 1+h//8)
                if v+cost<dp[i+1][j+1]:
                    dp[i+1][j+1]=v+cost; prev[i+1][j+1]=(i,j,'map')
    mapping=[None]*m; deleted=[]; i,j=n,m
    while i or j:
        p=prev[i][j]
        if p is None:
            # Defensive fallback: preserve monotonicity and report leftovers.
            if i: deleted.extend(range(old_base,old_base+i))
            break
        pi,pj,op=p
        if op=='map': mapping[j-1]=old_base+i-1
        elif op=='del': deleted.append(old_base+i-1)
        i,j=pi,pj
    deleted.reverse()
    return mapping, deleted


def _global_exact_pairs(old_records:list[bytes], new_records:list[bytes]):
    """Reserve every exact survivor before interpreting changed records.

    The editor rewrites byte 4 (Army ID), so equality is based on ``_sig``.
    Exact matching is global rather than LCS-only: this is essential for
    reorders and for several insert/delete operations in one edit.  Duplicate
    exact records are intrinsically indistinguishable, therefore occurrences
    are paired in their original order to preserve runtime progress as
    conservatively as possible.
    """
    old_by={}; new_by={}
    for i,raw in enumerate(old_records):
        old_by.setdefault(_sig(raw),[]).append(i)
    for j,raw in enumerate(new_records):
        new_by.setdefault(_sig(raw),[]).append(j)
    pairs=[]
    for signature in old_by.keys() & new_by.keys():
        oi=old_by[signature]; nj=new_by[signature]
        for i,j in zip(oi,nj):
            pairs.append((i,j))
    return sorted(pairs, key=lambda pair: pair[1])


def _pair_crosses_exact(old_index:int, new_index:int, exact_pairs:list[tuple[int,int]])->bool:
    """Whether a tentative edited-record match would cross an exact survivor."""
    return any(
        (oi-old_index)*(nj-new_index) < 0
        for oi,nj in exact_pairs
    )


def align_armies(old_records:list[bytes], new_records:list[bytes]):
    """Return target->source mapping (0-based), plus deleted source indices.

    Identity has three evidence levels:

    * exact record (ignoring the renumbered ID byte): always preserved, even
      across an explicit reorder;
    * changed record occupying the same structural lane between exact
      survivors: conservatively treated as an edit so gameplay runtime state is
      retained;
    * unmatched record that would have to cross an exact survivor: structural
      add/delete, never allowed to consume that exact survivor.

    A delete+add that replaces one army in exactly the same structural lane is
    information-theoretically indistinguishable from an in-place edit because
    MapLDV V.4 has no persistent army GUID.  The conservative choice is to
    preserve the old runtime body and treat it as an edit.
    """
    n,m=len(old_records),len(new_records)
    mapping=[None]*m
    exact_pairs=_global_exact_pairs(old_records,new_records)
    used_old=set()
    for oi,nj in exact_pairs:
        mapping[nj]=oi
        used_old.add(oi)

    old_left=[i for i in range(n) if i not in used_old]
    new_left=[j for j in range(m) if mapping[j] is None]
    on,nn=len(old_left),len(new_left)
    if not on:
        return mapping, []
    if not nn:
        return mapping, old_left

    # Residual sequence alignment.  Mapping one changed record is cheaper than
    # delete+add, but it is forbidden when doing so would cross an already
    # proven exact survivor.  This maximizes progress preservation without ever
    # sacrificing exact identity to a heuristic substitution.
    INF=10**9
    dp=[[INF]*(nn+1) for _ in range(on+1)]
    prev=[[None]*(nn+1) for _ in range(on+1)]
    dp[0][0]=0
    for i in range(on+1):
        for j in range(nn+1):
            value=dp[i][j]
            if value>=INF:
                continue
            if i<on and value+3<dp[i+1][j]:
                dp[i+1][j]=value+3; prev[i+1][j]=(i,j,'del')
            if j<nn and value+3<dp[i][j+1]:
                dp[i][j+1]=value+3; prev[i][j+1]=(i,j,'add')
            if i<on and j<nn:
                oi=old_left[i]; nj=new_left[j]
                if not _pair_crosses_exact(oi,nj,exact_pairs):
                    # Cost 1 deliberately does not depend on byte similarity:
                    # a legitimate editor change can rewrite most semantic
                    # fields.  Structural position is the only reliable
                    # identity evidence left once exact matches are reserved.
                    if value+1<dp[i+1][j+1]:
                        dp[i+1][j+1]=value+1; prev[i+1][j+1]=(i,j,'map')

    i,j=on,nn
    while i or j:
        p=prev[i][j]
        if p is None:
            # Defensive fallback: leave the unresolved records structural.
            break
        pi,pj,op=p
        if op=='map':
            mapping[new_left[j-1]]=old_left[i-1]
        i,j=pi,pj

    mapped_old={value for value in mapping if value is not None}
    deleted=sorted(set(range(n))-mapped_old)
    return mapping, deleted


def army_structure_changed(mapping:list[int|None], deleted:list[int], old_count:int, new_count:int)->bool:
    """True when army IDs/slots change, even if total count stays equal."""
    if old_count!=new_count or deleted or any(v is None for v in mapping):
        return True
    return any(src!=dst for dst,src in enumerate(mapping))


def load_catalog(path:Path|None=None):
    if path is None and _ACTIVE_CATALOG is not None:
        return _ACTIVE_CATALOG
    if path is None:
        base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
        path = base / 'army_catalog.json'
    else:
        base = path.resolve().parent
    if not path.exists():
        return {'units':{},'artifacts':{},'spells':[],'_native_profiles':{}}
    catalog=json.loads(path.read_text(encoding='utf-8'))
    profile_path=base / 'army_native_profiles.json'
    if profile_path.exists():
        try:
            catalog['_native_profiles']=json.loads(profile_path.read_text(encoding='utf-8'))
        except (OSError,ValueError,TypeError):
            catalog['_native_profiles']={}
    else:
        catalog['_native_profiles']={}
    return catalog


def _profile_for(uid:int, level:int, catalog:dict|None=None):
    if uid in _compat_ids(catalog, 'changed_units'):
        return {}
    value=dict(PROFILE.get((uid,level),{}))
    db=(catalog or {}).get('_native_profiles',{})
    ext=db.get('profiles',{}).get(f'{uid}:{level}',{}) if isinstance(db,dict) else {}
    if isinstance(ext,dict):
        value.update({k:int(v) for k,v in ext.items() if k in {'v1','v2','strength','payment','class'}})
    return value


def _army_runtime_context_key(rec:ArmyRecord|None):
    """Stable semantic key for the opaque runtime dword at +0x1E9D.

    Native controls prove that +0x1E9D is *not* a pure Unit-ID/leader field:
    the same main unit can produce different values in different army
    contexts.  Coordinates and editor-assigned Army ID do not affect it, so
    the key uses the remaining 84 bytes of ArmyData verbatim.  This is
    intentionally conservative: an unseen context is left untouched rather
    than guessed from a misleading per-unit table.
    """
    if rec is None:
        return None
    return rec.raw[5:].hex()


def _leader_runtime_code(rec:ArmyRecord|None, catalog:dict|None=None):
    """Return exact native +0x1E9D only for a byte-certified army context.

    Older builds treated this dword as `main Unit ID -> code`.  The 102-army
    control plus the earlier multi-unit maps disproves that assumption for at
    least 17 Unit IDs.  Production therefore consults only contextual native
    profiles.  `leader_runtime_codes` remains in the JSON as diagnostic
    evidence, but is deliberately not used for synthesis.
    """
    if rec is None or not rec.main_id or rec.named_unit_id:
        return None
    if any(uid in _compat_ids(catalog, 'changed_units') for uid, _lvl, _main in rec.expanded()):
        return None
    if any(i in _compat_ids(catalog, 'changed_artifacts') for i in rec.items if i):
        return None
    if rec.applied_spell and rec.applied_spell in _compat_ids(catalog, 'changed_spells'):
        return None
    db=(catalog or {}).get('_native_profiles',{})
    if not isinstance(db,dict):
        return None
    key=_army_runtime_context_key(rec)
    value=db.get('army_runtime_codes',{}).get(key) if key else None
    return int(value) if value is not None else None


def _native_modified_strength(uid:int, level:int, item_ids:list[int], spell_id:int, catalog:dict):
    if catalog_profile_untested(uid,item_ids,spell_id,catalog):
        return None
    key=(uid,level,tuple(i for i in item_ids if i),spell_id)
    if key in MOD_STRENGTH:
        return MOD_STRENGTH[key]
    db=catalog.get('_native_profiles',{})
    if not isinstance(db,dict): return None
    full=(item_ids+[0,0,0])[:3]
    skey=f'{uid}:{level}:{full[0]},{full[1]},{full[2]}:{spell_id}'
    value=db.get('modified_strengths',{}).get(skey)
    return int(value) if value is not None else None


def _native_modified_profile(uid:int, level:int, item_ids:list[int], spell_id:int, catalog:dict):
    """Return exact modified v1/v2/strength mined from native saves, if known."""
    if catalog_profile_untested(uid,item_ids,spell_id,catalog):
        return {}
    full=(list(item_ids)+[0,0,0])[:3]
    db=catalog.get('_native_profiles',{})
    value={}
    if isinstance(db,dict):
        skey=f'{uid}:{level}:{full[0]},{full[1]},{full[2]}:{spell_id}'
        raw=db.get('modified_profiles',{}).get(skey,{})
        if isinstance(raw,dict):
            value={k:int(v) for k,v in raw.items() if k in {'v1','v2','strength'}}
    # Preserve the three earlier byte-certified controls which predate the
    # 102-unit corpus and therefore have only strength recorded in JSON.
    legacy_key=(uid,level,tuple(i for i in full if i),spell_id)
    if legacy_key in MOD_STRENGTH:
        value.setdefault('strength',int(MOD_STRENGTH[legacy_key]))
        value.setdefault('_legacy_strength_only',1)
    return value


def _num(d,k,default=0):
    try: return int(d.get(k,default) or default)
    except (TypeError,ValueError): return default


def _trunc_div(n,d): return math.trunc(n/d)

def _apply_modifier(v:int, d:dict, name:str)->int:
    if f'f-{name}' in d and d[f'f-{name}']!='': v=_num(d,f'f-{name}',v)
    if f'd-{name}' in d and d[f'd-{name}']!='': v += _num(d,f'd-{name}')
    if f'p-{name}' in d and d[f'p-{name}']!='':
        p=_num(d,f'p-{name}')
        if name in PERCENT_POINT_STATS: v += p
        else: v += _trunc_div(v*p,100)
    return v


def _base_stats(unit:dict, level:int):
    """Native level growth for one UnitData stat set.

    Ordinary integer stats grow linearly by d-*.  Percentage-point stats use
    the legacy asymptotic formula seen in the 102-unit native corpus; a simple
    linear add diverges at higher levels.
    """
    stats={name:_num(unit,name) for name in STAT_FIELDS}
    for name in STAT_FIELDS:
        base=stats[name]
        delta=_num(unit,'d-'+name)
        if name in PERCENT_POINT_STATS:
            # round() is intentionally Python's ties-to-even, matching the
            # native values observed in the corpus.
            stats[name]=round(100 - (100-base) * ((100-delta)/100.0)**level)
        else:
            stats[name]=base + delta*level
    return stats


def _merged_modifier(mods:Iterable[dict], name:str):
    """Merge legacy f/d/p modifiers before applying them once."""
    have_set=False; set_value=0; add=0; percent=0
    for mod in mods:
        fk=f'f-{name}'; dk=f'd-{name}'; pk=f'p-{name}'
        if fk in mod and mod[fk] not in ('',None):
            have_set=True; set_value=_num(mod,fk)
        if dk in mod and mod[dk] not in ('',None):
            add += _num(mod,dk)
        if pk in mod and mod[pk] not in ('',None):
            percent += _num(mod,pk)
    return have_set,set_value,add,percent


def _apply_mods(stats:dict, mods:Iterable[dict]):
    """Apply spell/artifact modifiers using native legacy semantics.

    For ordinary stats the engine merges f/d/p channels and applies the merged
    modifier once.  Initiative is the one proven exception: modifiers are
    applied sequentially (spell first, then artifacts) and each percentage is
    evaluated as trunc(value * (100+p) / 100).  Percentage-point defences,
    regen and vampirism treat p-* as direct points.
    """
    mods=list(mods)
    out=dict(stats)
    for name in STAT_FIELDS:
        # Attack channels which do not exist on the base archetype cannot be
        # created by an artifact/spell modifier (legacy ModifyPower rule).
        if name in ('AttackBlow','AttackShot') and stats.get(name,0)<=0:
            out[name]=0
            continue
        if name=='Initiative':
            v=int(stats.get(name,0))
            for mod in mods:
                fk=f'f-{name}'; dk=f'd-{name}'; pk=f'p-{name}'
                if fk in mod and mod[fk] not in ('',None): v=_num(mod,fk,v)
                if dk in mod and mod[dk] not in ('',None): v += _num(mod,dk)
                if pk in mod and mod[pk] not in ('',None):
                    p=_num(mod,pk)
                    v=_trunc_div(v*(100+p),100)
                v=max(0,v)
            out[name]=v
            continue
        have_set,set_value,add,percent=_merged_modifier(mods,name)
        v=set_value if have_set else int(stats.get(name,0))
        v += add
        if percent:
            if name in PERCENT_POINT_STATS:
                v += percent
            else:
                # Legacy arithmetic is value += trunc(value*p/100), not a
                # single trunc(value*(100+p)/100).  The distinction matters
                # for negative percentages and exactly reproduces native
                # half cases such as 65 -10% => 59 and 25 -50% => 13.
                v += _trunc_div(v*percent,100)
        if name in ('ProtectLife','ProtectDeath','ProtectElemental'):
            v=max(0,min(99,v))
        out[name]=v
    return out


def calculate_tactical_components(stats:dict, unit:dict, bonus:str|None=None, *, runtime_compat:bool=True):
    """Reproduce UnitEditor.CalculateTacticalStrength/CalculateTacticCost.

    The arithmetic and branching are translated from Editor1.3.10.1.exe IL.
    ``runtime_compat`` applies the two bonus deltas proven reversed in the
    original game runtime by the 102-unit native SAV corpus: GodStrike +20 and
    GodAnger +10 (the editor executable uses +10 and +20 respectively).

    Returns rounded runtime candidates (v1=Attack, v2=DefVamp, tactical_cost)
    together with the unrounded intermediates for diagnostics.
    """
    def f(name):
        try: return float(stats.get(name,0) or 0)
        except (TypeError,ValueError): return 0.0

    hits=f('Hits')
    if hits<=0.0:
        hits=1.0
    attack_blow=f('AttackBlow'); attack_shot=f('AttackShot')
    defence_blow=f('DefenceBlow'); defence_shot=f('DefenceShot')
    magic_power=f('MagicPower'); initiative=f('Initiative'); manevres=f('Manevres')
    protect_death=f('ProtectDeath'); protect_life=f('ProtectLife'); protect_elemental=f('ProtectElemental')
    vampirizm=f('Vampirizm'); regen=f('Regen')
    bonus=(bonus if bonus is not None else (unit.get('Bonus') or '')) or ''
    magic=(unit.get('Magic') or '')
    magic_direction=(unit.get('MagicDirection') or '')

    # Attack-class selector from the editor: 4=melee, 7=ranged, 17=magic.
    attack_class=4
    if attack_shot>attack_blow and attack_shot>magic_power:
        attack_class=7
    if magic_power>attack_blow*0.5 and magic_power>attack_shot*0.5:
        attack_class=17

    # DefencePower branch reconstructed from IL_0690..IL_07BC.
    if attack_class==4:
        x=defence_blow/21.5
        blow_term=math.exp(x)/1.17 if x<100.0 else 999999.0
        shot_regen=regen*hits/100.0 + defence_shot
        defence_power=round((math.exp(shot_regen/30.3)/1.07 + blow_term) * hits)
    else:
        adjusted_blow=defence_blow-5.0 if defence_blow>5.0 else 0.0
        x=adjusted_blow/21.5
        blow_term=math.exp(x)/1.17 if x<100.0 else 999999.0
        shot_regen=regen*hits/100.0 + defence_shot + 5.0
        defence_power=round((math.exp(shot_regen/30.3)/1.07 + blow_term) * hits)

    if bonus=='Garrison':
        defence_power*=2.0
    elif bonus=='SpearDefense':
        defence_power*=1.15
    elif bonus in ('Unvulnerabe','Ghost'):
        defence_power=hits*10.0
    elif bonus in ('VampirsGist','OldVampirsGist','Evasive'):
        defence_power*=1.5

    total_defence=defence_power + hits
    if bonus in ('Dead','FastDead'):
        total_defence*=1.7

    protection_coefficient=(protect_life + protect_death + 1.5*protect_elemental)/560.0
    effective_defence=(1.0 + protection_coefficient) * total_defence

    # Base Attack selection.
    if attack_blow>=attack_shot and attack_blow>=magic_power:
        attack=attack_blow
    elif attack_shot>=attack_blow and attack_shot>=magic_power:
        attack=attack_shot*1.4
    else:
        attack=magic_power

    if bonus in ('ArmorIgnore','VampirsGist','OldVampirsGist','Artillery'):
        attack+=18.0

    if attack_class==17 and manevres>0.0:
        if magic_direction=='ToEnemy':
            attack=attack*0.8*(manevres-1.0)/manevres + attack*0.2
            if magic=='ElementalMagic':
                attack+=25.0
        elif magic_direction=='ToAll':
            attack*=1.2

    if bonus=='Garrison':
        attack*=2.0
    if bonus=='GodStrike':
        attack += 20.0 if runtime_compat else 10.0
    if bonus=='GodAnger':
        attack += 10.0 if runtime_compat else 20.0
    if bonus=='Counterblow':
        attack += attack_blow
    if bonus=='FlankStrike':
        attack += attack_blow/3.0

    flank=attack*0.15 if bonus in ('HorseAtack','OldVampirsGist','FastDead') else 0.0
    attack=attack*manevres + flank
    if initiative>0.0:
        attack += initiative/100.0 * attack

    def_vamp=effective_defence + vampirizm/100.0 * effective_defence * attack/hits
    tactical_strength=def_vamp * 3.2 * (attack + 1.0) / 200.0
    if bonus in ('DeathCurse','Ghost'):
        tactical_strength += 150.0
    if bonus=='Poison':
        tactical_strength *= 1.1
    if math.isnan(tactical_strength) or math.isinf(tactical_strength):
        tactical_strength=2147483647.0

    cost_multiplier=_num(unit,'CostMultipler',100)
    tactical_cost=round(tactical_strength * cost_multiplier / 100.0)
    return {
        'attack':attack,
        'def_vamp':def_vamp,
        'defence_power':defence_power,
        'total_defence':total_defence,
        'effective_defence':effective_defence,
        'tactical_strength':tactical_strength,
        'tactical_cost':int(tactical_cost),
        'v1':int(round(attack)),
        'v2':int(round(def_vamp)),
        'attack_class':attack_class,
    }


def calculate_upkeep(unit:dict, cost_recruit_div:int|float|None=2)->int:
    """Exact UnitEditor.CalculateUpkeep formula (level-independent).

    CostGoldDiv <= 0 and invalid/missing CostRecruitDiv both fall back to 2.
    Python round() implements the same ties-to-even rule as .NET Math.Round().
    """
    cost=_num(unit,'Cost')
    gold_div=_num(unit,'CostGoldDiv',2)
    if gold_div<=0:
        gold_div=2
    try:
        recruit_div=float(cost_recruit_div)
    except (TypeError,ValueError):
        recruit_div=2.0
    if not math.isfinite(recruit_div) or recruit_div<=0.0:
        recruit_div=2.0
    if cost<=50: coefficient=0.25
    elif cost<=100: coefficient=0.50
    elif cost<=150: coefficient=0.75
    else: coefficient=1.00
    return int(round((float(cost)/float(gold_div))/recruit_div * coefficient))


def _catalog_cost_recruit_div(catalog:dict):
    """Read optional _Global.ini-derived divisor from catalog; default is 2."""
    for key in ('_global','global','Global'):
        d=catalog.get(key)
        if isinstance(d,dict) and 'CostRecruitDiv' in d:
            return d.get('CostRecruitDiv')
    return 2


def _bonus_for(unit:dict, items:list[dict]):
    bonus=unit.get('Bonus','')
    for item in items:
        if item.get('Bonus'): bonus=item['Bonus']
    return BONUS_CODES.get(bonus,0)


def _write_stat_block(buf:bytearray, off:int, unit:dict, stats:dict, bonus:int, v1:int, v2:int, clamp=False):
    nature=NATURE_CODES.get(unit.get('Nature','People') or 'People',0)
    buf[off]=nature
    for name,rel in STAT_FIELDS.items():
        val=int(stats.get(name,0));
        if clamp and name not in ('Regen','Vampirizm'): val=max(0,val)
        struct.pack_into('<i',buf,off+rel,val)
    buf[off+21]=MAGIC_CODES.get(unit.get('Magic',''),0)
    buf[off+26]=MAGIC_DIR_CODES.get(unit.get('MagicDirection',''),0)
    buf[off+55]=bonus
    struct.pack_into('<II',buf,off+56,max(0,int(v1)),max(0,int(v2)))


def _class_code(stats:dict, unit:dict):
    if _num(unit,'MagicPower') or unit.get('Magic'): return 17
    if stats.get('AttackShot',0)>0: return 7
    return 4


def _unit_model_code(uid:int):
    # Native main-unit runtime model observed in supplied saves.
    if uid in {4,9}: return 2
    if uid in {61,62,68,69}: return 3
    if uid in {66,77,52}: return 1
    return 0


def _template_fields(template:bytes|None, uid:int, level:int, catalog:dict|None=None):
    if template and len(template)==UNIT_STRIDE:
        b=template[0x11D:0x15D]
        return struct.unpack_from('<II',b,56)
    p=_profile_for(uid,level,catalog)
    return p.get('v1',0),p.get('v2',0)


def make_unit_record(uid:int, level:int, is_main:bool, named_id:int, spell_id:int, item_ids:list[int], catalog:dict,
                     template:bytes|None=None, *, cold_added_unit:bool=False, strict_derived:bool=False)->bytes:
    units=catalog.get('units',{}); arts=catalog.get('artifacts',{}); spells=catalog.get('spells',[])
    unit=units.get(str(uid),{})
    if not unit: raise ValueError(f'Нет данных Rus_Units.ini для юнита ID {uid}')
    profile_untested=catalog_profile_untested(uid,item_ids,spell_id,catalog)
    enforce_strict=bool(strict_derived and not profile_untested)
    buf=bytearray(template if template and len(template)==UNIT_STRIDE else b'\0'*UNIT_STRIDE)
    # Clear runtime-varying semantic record while retaining obscure template bytes later in record.
    struct.pack_into('<I',buf,0,uid-1); struct.pack_into('<I',buf,0x10,level)
    struct.pack_into('<I',buf,0x14,named_id if is_main else 0)
    template_same_role=(
        template is not None and len(template)==UNIT_STRIDE and
        ((template[0x19D]==0) == bool(is_main))
    )
    if not template_same_role:
        struct.pack_into('<I',buf,0x18,_unit_model_code(uid) if is_main else 0)
    struct.pack_into('<I',buf,0x1C,uid if is_main else 3)
    struct.pack_into('<I',buf,0x20,0xFFFFFFFF)
    struct.pack_into('<I',buf,0x24,(spell_id-1) if spell_id else 0xFFFFFFFF)
    struct.pack_into('<I',buf,0x28,0x3DCC5000 if spell_id else 0)
    for o,v in ((0x2C,0xFFFFFFFF),(0x30,0),(0x34,0xFFFFFFFF),(0x38,0),(0x3C,0xFFFFFFFF)):
        struct.pack_into('<I',buf,o,v)
    # Artifacts are 1-based IDs.
    for ix,o in enumerate((0xCD,0xD1,0xD5)):
        struct.pack_into('<I',buf,o,item_ids[ix] if ix<len(item_ids) else 0)
    item_defs=[arts.get(str(i),{}) for i in item_ids if i]
    base=_base_stats(unit,level)
    # Initiative proves native order is spell first, then equipped artifacts.
    mod_defs=[]
    if spell_id and 1<=spell_id<=len(spells): mod_defs.append(spells[spell_id-1])
    mod_defs.extend(item_defs)
    modified=_apply_mods(base,mod_defs)
    bonus=_bonus_for(unit,item_defs)
    prof=_profile_for(uid,level,catalog)
    base_formula=calculate_tactical_components(base,unit,unit.get('Bonus') or '',runtime_compat=True)
    template_v1,template_v2=_template_fields(template,uid,level,catalog)
    v1=template_v1 or prof.get('v1',0)
    v2=template_v2 or prof.get('v2',0)
    if enforce_strict and (not v1 or not v2):
        raise ValueError(
            f'Юнит ID {uid}, уровень {level}: нет нативно подтверждённых derived v1/v2. '
            'Формула редактора доступна только как fallback; добавьте native profile для byte-exact strict режима.'
        )
    if not v1: v1=base_formula['v1']
    if not v2: v2=base_formula['v2']
    _write_stat_block(buf,0x11D,unit,base,BONUS_CODES.get(unit.get('Bonus',''),0),v1,v2,False)

    # The live A-copy uses the modified runtime profile.  Exact observed
    # combinations override the editor formula because a few legacy runtime
    # bonuses differ from Editor1.3.10.1 by one point or by bonus semantics.
    mod_prof=_native_modified_profile(uid,level,item_ids,spell_id,catalog)
    mod_formula=calculate_tactical_components(modified,unit,next((k for k,v in BONUS_CODES.items() if v==bonus),unit.get('Bonus') or ''),runtime_compat=True)
    if item_ids or spell_id:
        if enforce_strict and not mod_prof:
            raise ValueError(
                f'Юнит ID {uid}, уровень {level}, artifacts={item_ids}, spell={spell_id}: '
                'нет нативно подтверждённого modified profile.'
            )
        if mod_prof.get('_legacy_strength_only') and 'v1' not in mod_prof and 'v2' not in mod_prof:
            # Older native controls proved the complete runtime body before we
            # began mining modified v1/v2 explicitly; retain their base tails.
            mv1,mv2=v1,v2
        else:
            mv1=mod_prof.get('v1',mod_formula['v1'])
            mv2=mod_prof.get('v2',mod_formula['v2'])
    else:
        mv1,mv2=v1,v2

    # The C-copy keeps base derived tails; a genuinely new runtime unit starts
    # with the C-tail uninitialized, as proven by native append/new-army saves.
    _write_stat_block(buf,0x0DD,unit,modified,bonus,mv1,mv2,False)
    _write_stat_block(buf,0x15D,unit,modified,bonus,0 if cold_added_unit else v1,0 if cold_added_unit else v2,True)
    buf[0x19D]=0 if is_main else 1
    # Constant marker proven identical in all 105 native unit observations.
    buf[0x1A5]=1; struct.pack_into('<I',buf,0x1A6,1)

    base_strength=prof.get('strength')
    if base_strength is None and template:
        base_strength=struct.unpack_from('<I',template,0x1AA)[0]
    if base_strength is None:
        if enforce_strict:
            raise ValueError(
                f'Юнит ID {uid}, уровень {level}: нет нативно подтверждённой base strength. '
                'Формула редактора доступна только как fallback; добавьте native profile для byte-exact strict режима.'
            )
        base_strength=base_formula['tactical_cost']
    mod_strength=mod_prof.get('strength') if mod_prof else _native_modified_strength(uid,level,item_ids,spell_id,catalog)
    if mod_strength is None:
        if not item_ids and not spell_id:
            mod_strength=base_strength
        elif template and tuple(struct.unpack_from('<I',template,o)[0] for o in (0xCD,0xD1,0xD5))==tuple((item_ids+[0,0,0])[:3]) and struct.unpack_from('<I',template,0x24)[0]==((spell_id-1) if spell_id else 0xFFFFFFFF):
            mod_strength=struct.unpack_from('<I',template,0x1AE)[0]
        elif enforce_strict:
            raise ValueError(
                f'Юнит ID {uid}, уровень {level}, artifacts={item_ids}, spell={spell_id}: '
                'нет нативно подтверждённой modified strength.'
            )
        else:
            mod_strength=mod_formula['tactical_cost']
    struct.pack_into('<II',buf,0x1AA,int(base_strength),int(mod_strength))
    cls=prof.get('class')
    if cls is None:
        # Class is archetype-stable in the native corpus.  If this level was
        # not observed, reuse the unique class proven for the same Unit ID.
        db=(catalog or {}).get('_native_profiles',{})
        classes=set()
        if uid in _compat_ids(catalog, 'changed_units'):
            db={}
        if isinstance(db,dict):
            for key,value in db.get('profiles',{}).items():
                try: puid=int(key.split(':',1)[0])
                except (ValueError,TypeError): continue
                if puid==uid and isinstance(value,dict) and 'class' in value:
                    classes.add(int(value['class']))
        if len(classes)==1: cls=next(iter(classes))
    if cls is None: cls=_class_code(base,unit)
    struct.pack_into('<I',buf,0x1B2,int(cls))
    return bytes(buf)


def _artifact_compatible(unit:dict,item:dict):
    """Legacy equip predicate reconstructed from the native/remaster rules.

    This is deliberately stricter than the old weapon-only check: mechs cannot
    equip artifacts, magic-restricted artifacts must match the unit school, and
    generic Item records are inventory objects rather than equipment.
    """
    if not unit or not item:
        return False
    if (unit.get('Nature') or 'People') == 'Mecha':
        return False
    required_magic=(item.get('Magic') or '').strip()
    unit_magic=(unit.get('Magic') or '').strip()
    if required_magic and required_magic != unit_magic:
        return False
    t=(item.get('Type') or '').strip()
    hand=_num(unit,'AttackBlow')>0
    shot=_num(unit,'AttackShot')>0
    magic=_num(unit,'MagicPower')>0 or bool(unit_magic)
    if t=='Item': return False
    if t=='BlowWeapon': return hand
    if t=='ShotWeapon': return shot
    if t in ('Staff','MagicWeapon'): return magic
    return t in {'Armor','Shield','Helm','Ring','Amulet','Potion'}


_ARTIFACT_WEAPON_TYPES={'BlowWeapon','ShotWeapon','Staff','MagicWeapon'}
_ARTIFACT_SINGLETON_TYPES={'Armor','Shield','Helm','Ring','Amulet'}


def _artifact_slot_available(current_ids:list[int], item:dict, arts:dict)->bool:
    """Whether the next artifact can occupy one of the three runtime slots.

    Native controls plus the remaster equip predicate prove one weapon total,
    one artifact of each ordinary equipment type, and freely repeatable Potion
    items (up to the three legacy runtime slots).
    """
    if len(current_ids)>=3:
        return False
    typ=(item.get('Type') or '').strip()
    current_types=[(arts.get(str(i),{}).get('Type') or '').strip() for i in current_ids]
    if typ in _ARTIFACT_WEAPON_TYPES:
        return not any(t in _ARTIFACT_WEAPON_TYPES for t in current_types)
    if typ in _ARTIFACT_SINGLETON_TYPES:
        return typ not in current_types
    if typ=='Potion':
        return True
    return False


def _artifact_assignment_strength(uid:int, level:int, item_ids:list[int], spell_id:int, catalog:dict)->int:
    """Runtime tactical cost used by the native greedy item allocator."""
    units=catalog.get('units',{}); arts=catalog.get('artifacts',{}); spells=catalog.get('spells',[])
    unit=units.get(str(uid),{})
    if not unit:
        return -2**31
    base=_base_stats(unit,level)
    mod_defs=[]
    if spell_id and 1<=spell_id<=len(spells):
        mod_defs.append(spells[spell_id-1])
    item_defs=[arts.get(str(i),{}) for i in item_ids if i]
    mod_defs.extend(item_defs)
    modified=_apply_mods(base,mod_defs)
    bonus_code=_bonus_for(unit,item_defs)
    bonus_name=next((k for k,v in BONUS_CODES.items() if v==bonus_code),unit.get('Bonus') or '')
    return calculate_tactical_components(modified,unit,bonus_name,runtime_compat=True)['tactical_cost']


def distribute_items(comp:list[tuple[int,int,bool]], army_items:tuple[int,int,int], catalog:dict, spell_id:int=0):
    """Distribute DTm army artifacts exactly as the legacy loader does.

    The native ``Другой берег4 -> Другой берег3`` control contains four
    independent armies and twelve artifacts.  In all cases the loader handles
    ``items_ids`` from left to right and greedily equips the compatible unit
    which gives the largest *increment* in the army's current tactical cost.
    Unit order is the deterministic tie-break.
    """
    units=catalog.get('units',{}); arts=catalog.get('artifacts',{})
    result=[[] for _ in comp]
    current_scores=[
        _artifact_assignment_strength(uid,lvl,[],spell_id,catalog)
        for uid,lvl,_ in comp
    ]
    for iid in army_items:
        if not iid:
            continue
        item=arts.get(str(iid),{})
        if not item:
            continue
        best_i=None; best_score=None; best_delta=None
        for i,(uid,lvl,_) in enumerate(comp):
            unit=units.get(str(uid),{})
            if not _artifact_compatible(unit,item):
                continue
            if not _artifact_slot_available(result[i],item,arts):
                continue
            score=_artifact_assignment_strength(uid,lvl,result[i]+[iid],spell_id,catalog)
            delta=score-current_scores[i]
            if best_i is None or delta>best_delta:
                best_i=i; best_score=score; best_delta=delta
        if best_i is not None:
            result[best_i].append(iid)
            current_scores[best_i]=int(best_score)
    return result


def native_comp_from_block(block:bytes):
    if len(block) < RUNTIME_ARMY_SIZE:
        raise ValueError(f'Runtime-блок армии имеет {len(block)} байт вместо {RUNTIME_ARMY_SIZE}.')
    cnt=struct.unpack_from('<I',block,UNIT_COUNT_OFF)[0]
    if cnt > LEGACY_V4_PROFILE.max_units:
        raise ValueError(f'Runtime-блок содержит недопустимое число персонажей: {cnt} > {LEGACY_V4_PROFILE.max_units}.')
    out=[]
    for i in range(cnt):
        u=block[UNIT_BASE_OFF+i*UNIT_STRIDE:UNIT_BASE_OFF+(i+1)*UNIT_STRIDE]
        out.append((struct.unpack_from('<I',u,0)[0]+1,struct.unpack_from('<I',u,0x10)[0]))
    return out


def _find_templates(source_blocks:list[bytes]):
    d={}
    for block in source_blocks:
        cnt=min(12,struct.unpack_from('<I',block,UNIT_COUNT_OFF)[0]) if len(block)>=UNIT_COUNT_OFF+4 else 0
        for i in range(cnt):
            u=block[UNIT_BASE_OFF+i*UNIT_STRIDE:UNIT_BASE_OFF+(i+1)*UNIT_STRIDE]
            uid=struct.unpack_from('<I',u,0)[0]+1; lvl=struct.unpack_from('<I',u,0x10)[0]
            # Prefer clean/no spell/no item records.
            clean=(struct.unpack_from('<I',u,0x24)[0]==0xFFFFFFFF and all(struct.unpack_from('<I',u,o)[0]==0 for o in (0xCD,0xD1,0xD5)))
            k=(uid,lvl)
            if k not in d or clean: d[k]=bytes(u)
    return d


def _formation_prefers_front(unit_record:bytes)->bool:
    """Return the native preferred battle line for a rebuilt one-cell unit.

    Runtime class alone is insufficient: several class-17 hybrids are placed
    on the front line when their hand attack is their dominant attack.  The
    12-unit native append control proves that the effective (modified) attack
    channels, not the broad class code, drive this choice.
    """
    base=0xDD
    blow=struct.unpack_from('<i',unit_record,base+STAT_FIELDS['AttackBlow'])[0]
    shot=struct.unpack_from('<i',unit_record,base+STAT_FIELDS['AttackShot'])[0]
    magic=struct.unpack_from('<i',unit_record,base+STAT_FIELDS['MagicPower'])[0]
    return blow>0 and blow>=shot and blow>=magic


def _formation(records:list[bytes]):
    """Build the 18-int legacy battle formation table.

    Native fresh-army controls show three rules for ordinary one-cell units:
    units are considered in descending *modified* tactical strength; the
    dominant modified attack selects front vs back; and when a preferred line
    is full, the remaining units spill into the free cells of the other line.
    Ties keep DTm/runtime unit order.  A one-unit army is always centered on
    the front line, matching native saves even for a pure ranged/magic unit.

    The front/back priority orders below reproduce the supplied native
    12-unit append control exactly, including the seventh front-preferring
    unit which must spill to the back line instead of being dropped.
    """
    vals=[0]*18
    for ix in FORMATION_SENTINELS:
        vals[ix]=-1
    logical=[0]*12
    if not records:
        for j,e in enumerate(FORMATION_ENTRIES):
            vals[e]=logical[j]
        return vals
    if len(records)==1:
        logical[3]=1
        for j,e in enumerate(FORMATION_ENTRIES):
            vals[e]=logical[j]
        return vals

    front_pref=[3,2,4,1,5,0]
    back_pref=[8,7,9,6,11,10]
    front_i=back_i=0

    # Python sort is stable, which is significant for equal-strength duplicate
    # troops: native keeps their original DTm/runtime sequence.
    ordered=sorted(
        enumerate(records,start=1),
        key=lambda pair: -struct.unpack_from('<I',pair[1],0x1AE)[0],
    )
    for unit_num,u in ordered:
        front=_formation_prefers_front(u)
        if front:
            if front_i<len(front_pref):
                pos=front_pref[front_i]; front_i+=1
            elif back_i<len(back_pref):
                pos=back_pref[back_i]; back_i+=1
            else:
                raise ValueError('formation has more than 12 units')
        else:
            if back_i<len(back_pref):
                pos=back_pref[back_i]; back_i+=1
            elif front_i<len(front_pref):
                pos=front_pref[front_i]; front_i+=1
            else:
                raise ValueError('formation has more than 12 units')
        logical[pos]=unit_num

    for j,e in enumerate(FORMATION_ENTRIES):
        vals[e]=logical[j]
    return vals


def _resort_existing_formation(block:bytes, records:list[bytes]):
    """Re-sort only the native primary lane after item/spell changes.

    The legacy loader does not rebuild an existing formation from scratch when
    composition is unchanged.  It keeps overflow/reserve placements and only
    reorders troops already occupying their natural six-slot lane by current
    modified tactical strength.  This is proven by the four artifact controls
    in Другой берег4 -> Другой берег3.
    """
    vals=[struct.unpack_from('<i',block,FORMATION_OFF+i*4)[0] for i in range(18)]
    logical=[vals[e] for e in FORMATION_ENTRIES]
    lanes=((True,(3,2,4,1,5,0)),(False,(8,7,9,6,11,10)))
    for prefers_front,slots in lanes:
        occupied=[]; unit_nums=[]
        for slot in slots:
            num=logical[slot]
            if not (1<=num<=len(records)):
                continue
            if _formation_prefers_front(records[num-1]) != prefers_front:
                continue
            occupied.append(slot); unit_nums.append(num)
        unit_nums.sort(key=lambda n:-struct.unpack_from('<I',records[n-1],0x1AE)[0])
        for slot,num in zip(occupied,unit_nums):
            logical[slot]=num
    for j,e in enumerate(FORMATION_ENTRIES):
        vals[e]=logical[j]
    return vals


def verify_runtime_army_block(block:bytes, catalog:dict|None=None, *, expected_comp=None, rebuilt=False, verify_formation=True):
    """Verify structural invariants of one 0x3827 NPC runtime block.

    ``rebuilt`` enables stronger invariants which are safe only for blocks this
    converter has just rebuilt (zeroed unused unit area, regenerated formation,
    aggregate strength and HP sum).  Existing progressed blocks are checked only
    for bounds/decodability so gameplay state is never mistaken for corruption.
    """
    issues=[]
    if len(block)!=RUNTIME_ARMY_SIZE:
        return {'ok':False,'issues':[f'block_size={len(block)} expected={RUNTIME_ARMY_SIZE}']}
    cnt=struct.unpack_from('<I',block,UNIT_COUNT_OFF)[0]
    if cnt>LEGACY_V4_PROFILE.max_units:
        issues.append(f'unit_count={cnt} > {LEGACY_V4_PROFILE.max_units}')
        cnt=min(cnt,LEGACY_V4_PROFILE.max_units)
    records=[]; comp=[]
    units=(catalog or {}).get('units',{})
    arts=(catalog or {}).get('artifacts',{})
    spells=(catalog or {}).get('spells',[])
    for i in range(cnt):
        a=UNIT_BASE_OFF+i*UNIT_STRIDE; b=a+UNIT_STRIDE
        u=block[a:b]
        if len(u)!=UNIT_STRIDE:
            issues.append(f'unit[{i}] truncated')
            continue
        records.append(u)
        uid=struct.unpack_from('<I',u,0)[0]+1
        lvl=struct.unpack_from('<I',u,0x10)[0]
        comp.append((uid,lvl))
        if units and str(uid) not in units:
            issues.append(f'unit[{i}] unknown id={uid}')
        raw_spell=struct.unpack_from('<I',u,0x24)[0]
        if raw_spell!=0xFFFFFFFF and spells and raw_spell>=len(spells):
            issues.append(f'unit[{i}] invalid spell raw={raw_spell}')
        if arts:
            for off in (0xCD,0xD1,0xD5):
                iid=struct.unpack_from('<I',u,off)[0]
                if iid and str(iid) not in arts:
                    issues.append(f'unit[{i}] invalid artifact id={iid}')
    if expected_comp is not None:
        exp=[(u,l) for u,l,*_ in expected_comp]
        if comp!=exp:
            issues.append(f'composition={comp!r} expected={exp!r}')
    if rebuilt:
        used_end=UNIT_BASE_OFF+cnt*UNIT_STRIDE
        if any(block[used_end:UNIT_AREA_END]):
            issues.append('unused unit area is not zero-filled')
        strength=sum(struct.unpack_from('<I',u,0x1AE)[0] for u in records)
        actual_strength=struct.unpack_from('<I',block,AGG_STRENGTH_OFF)[0]
        if strength!=actual_strength:
            issues.append(f'aggregate_strength={actual_strength} expected={strength}')
        hp=sum(max(0,struct.unpack_from('<i',u,0xDE)[0]) for u in records)
        actual_hp=struct.unpack_from('<I',block,HP_SUM_OFF)[0]
        if hp!=actual_hp:
            issues.append(f'hp_sum={actual_hp} expected={hp}')
        if verify_formation:
            expected_form=_formation(records)
            actual_form=[struct.unpack_from('<i',block,FORMATION_OFF+i*4)[0] for i in range(18)]
            if actual_form!=expected_form:
                issues.append(f'formation={actual_form!r} expected={expected_form!r}')
    return {
        'ok':not issues,
        'issues':issues,
        'unit_count':cnt,
        'composition':comp,
        'aggregate_strength':struct.unpack_from('<I',block,AGG_STRENGTH_OFF)[0],
        'payment':struct.unpack_from('<I',block,PAYMENT_OFF)[0],
    }


def assert_runtime_army_block(block:bytes, catalog:dict|None=None, *, expected_comp=None, rebuilt=False, verify_formation=True, label='army'):
    report=verify_runtime_army_block(block,catalog,expected_comp=expected_comp,rebuilt=rebuilt,verify_formation=verify_formation)
    if not report['ok']:
        raise ValueError(f"{label}: runtime verifier: " + '; '.join(report['issues']))
    return report


def verify_synced_tail(tail:bytes, new_recs:list[ArmyRecord], catalog:dict, *, rebuilt_indices=(), formation_relaxed_indices=()):
    """Strict post-build verifier for hero + NPC slots + residual dynamic tail."""
    need=(len(new_recs)+1)*RUNTIME_ARMY_SIZE
    issues=[]
    if len(tail)<need:
        return {'ok':False,'issues':[f'tail_size={len(tail)} < required={need}'],'armies':[]}
    rebuilt=set(rebuilt_indices)
    formation_relaxed=set(formation_relaxed_indices)
    reports=[]
    for i,rec in enumerate(new_recs):
        a=(i+1)*RUNTIME_ARMY_SIZE; b=a+RUNTIME_ARMY_SIZE
        r=verify_runtime_army_block(
            tail[a:b],catalog,expected_comp=rec.expanded(),rebuilt=(i in rebuilt),verify_formation=(i not in formation_relaxed)
        )
        reports.append(r)
        if not r['ok']:
            issues.extend(f'army#{i+1}: {x}' for x in r['issues'])
    return {'ok':not issues,'issues':issues,'armies':reports,'residual_size':len(tail)-need}


def _learn_payment_profiles(source_blocks:list[bytes], source_recs:list[ArmyRecord], catalog:dict|None=None):
    """Extract payment values that are algebraically unambiguous in this SAV.

    Only armies with no units-without-money exemption and a homogeneous paid
    non-main composition are used.  This turns many existing runtime records
    into exact payment profiles without guessing a global formula.
    """
    candidates={}
    conflicts=set()
    for block,rec in zip(source_blocks,source_recs):
        if rec.units_without_money:
            continue
        try:
            cnt=struct.unpack_from('<I',block,UNIT_COUNT_OFF)[0]
            if cnt>LEGACY_V4_PROFILE.max_units:
                continue
            paid=[]
            for i in range(cnt):
                u=block[UNIT_BASE_OFF+i*UNIT_STRIDE:UNIT_BASE_OFF+(i+1)*UNIT_STRIDE]
                if u[0x19D]==0:
                    continue
                paid.append((struct.unpack_from('<I',u,0)[0]+1,struct.unpack_from('<I',u,0x10)[0]))
            if not paid or len(set(paid))!=1:
                continue
            total=struct.unpack_from('<I',block,PAYMENT_OFF)[0]
            if total % len(paid):
                continue
            key=paid[0]; value=total//len(paid)
            if key in candidates and candidates[key]!=value:
                conflicts.add(key)
            else:
                candidates[key]=value
        except (struct.error,IndexError):
            continue
    for key in conflicts:
        candidates.pop(key,None)
    keys=set(PROFILE)
    db=(catalog or {}).get('_native_profiles',{})
    if isinstance(db,dict):
        for text in db.get('profiles',{}):
            try:
                uid,lvl=(int(x) for x in text.split(':',1)); keys.add((uid,lvl))
            except (ValueError,TypeError):
                pass
    for key in keys:
        p=_profile_for(key[0],key[1],catalog)
        if 'payment' in p:
            candidates[key]=int(p['payment'])
    return candidates


def exact_profile_coverage(catalog:dict, templates:dict|None=None):
    """Report what can be synthesized without v1/v2/base-strength heuristics."""
    templates=templates or {}
    unit_ids=sorted(int(x) for x in catalog.get('units',{}))
    exact_ids=[]; heuristic_ids=[]
    for uid in unit_ids:
        profiles=[_profile_for(uid,lvl,catalog) for (u,lvl) in set(PROFILE) if u==uid]
        db=catalog.get('_native_profiles',{})
        if isinstance(db,dict):
            for text in db.get('profiles',{}):
                try:
                    u,lvl=(int(x) for x in text.split(':',1))
                except (ValueError,TypeError):
                    continue
                if u==uid: profiles.append(_profile_for(uid,lvl,catalog))
        exact=any('v1' in v and 'v2' in v and 'strength' in v for v in profiles)
        exact = exact or any(k[0]==uid for k in templates)
        (exact_ids if exact else heuristic_ids).append(uid)
    return {
        'catalog_units':len(unit_ids),
        'exact_unit_ids':exact_ids,
        'heuristic_unit_ids':heuristic_ids,
        'exact_count':len(exact_ids),
        'heuristic_count':len(heuristic_ids),
    }


def _prev_cost(rec:ArmyRecord,catalog:dict):
    total=0
    for uid,_,_ in rec.expanded():
        cost=_num(catalog.get('units',{}).get(str(uid),{}),'Cost')
        # Python round = bankers, matching native .5-to-even behavior observed.
        total += round(cost/2)
    return max(0,min(65535,total))


def patch_direct_fields(block:bytearray, old:ArmyRecord|None, new:ArmyRecord, preserve_progress=True, catalog:dict|None=None):
    def patch_u8(off, oldv, newv):
        if old is None or not preserve_progress or block[off]==oldv: block[off]=newv & 0xff
    def patch_i32(off, oldv, newv):
        cur=struct.unpack_from('<i',block,off)[0]
        if old is None or not preserve_progress or cur==oldv: struct.pack_into('<i',block,off,newv)
    def patch_u32(off, oldv, newv):
        cur=struct.unpack_from('<I',block,off)[0]
        if old is None or not preserve_progress or cur==oldv: struct.pack_into('<I',block,off,newv)
    patch_u8(ACTIVE_OFF, (1-old.activity) if old else 0, 1-new.activity)
    patch_u8(GROUP_OFF, old.group_type if old else 0, new.group_type)
    for i in range(4): patch_u8(RELATIONS_OFF+i, old.relations[i] if old else 0, new.relations[i])
    patch_u8(START_BUILDING_OFF, old.start_building_id if old else 0, new.start_building_id)
    patch_u8(PATROL_EXISTS_OFF, old.patrol_exists if old else 0, new.patrol_exists)
    patch_u8(PATROL_RADIUS_OFF, old.patrol_radius if old else 0, new.patrol_radius)
    # Native MapLDV V.4 loader initializes this companion patrol mode to 8
    # only for the special "patrol exists, radius 0" form; every other army
    # in the 62-army native control uses 5.
    def patrol_mode(rec):
        return 8 if rec is not None and rec.patrol_exists and rec.patrol_radius==0 else 5
    patch_u32(PATROL_MODE_OFF, patrol_mode(old), patrol_mode(new))
    old_leader_code=_leader_runtime_code(old,catalog)
    new_leader_code=_leader_runtime_code(new,catalog)
    if new_leader_code is not None:
        if old is None or not preserve_progress:
            struct.pack_into('<I',block,LEADER_RUNTIME_CODE_OFF,new_leader_code)
        elif old_leader_code is not None:
            patch_u32(LEADER_RUNTIME_CODE_OFF,old_leader_code,new_leader_code)
    def patrol_bounds(rec):
        if rec is None or not rec.patrol_exists:
            return (0,0,0,0)
        r=rec.patrol_radius
        return (max(0,rec.x-r), max(0,rec.y-r), rec.x+r, rec.y+r)
    old_bounds=patrol_bounds(old); new_bounds=patrol_bounds(new)
    for off,ov,nv in zip((0x1EC0,0x1EC4,0x1EC8,0x1ECC),old_bounds,new_bounds):
        patch_u32(off,ov,nv)
    oldagg=struct.unpack('<b',bytes([old.aggression]))[0] if old else 0; newagg=struct.unpack('<b',bytes([new.aggression]))[0]
    patch_i32(AGGRESSION_OFF,oldagg,newagg)
    patch_u32(REVIVE_OFF,(old.revive_time*1440) if old else 0,new.revive_time*1440)
    oldgold=struct.unpack('<h',struct.pack('<H',old.gold_income))[0] if old else 0; newgold=struct.unpack('<h',struct.pack('<H',new.gold_income))[0]
    patch_i32(GOLD_INCOME_OFF,oldgold,newgold)
    # DTM byte 0x50 is expanded by the legacy loader as value*10.
    old_known=(old.known*10) if old else 0
    new_known=new.known*10
    patch_u32(0x1EDC,old_known,new_known)
    if old is None and new.known==0:
        # Fresh control armies with no value here initialize the companion field
        # to zero; copying it from an arbitrary representative would leak state.
        struct.pack_into('<I',block,0x1EE4,0)
    # Home/current position are separately progress-sensitive.
    oldxy=(old.x,old.y) if old else (0,0)
    for off,ov,nv in ((HOME_X_OFF,oldxy[0],new.x),(HOME_Y_OFF,oldxy[1],new.y),(CURRENT_X_OFF,oldxy[0],new.x),(CURRENT_Y_OFF,oldxy[1],new.y)):
        patch_u32(off,ov,nv)


def _payment_guess(uid:int, level:int, catalog:dict, learned:dict|None=None, *, strict:bool=False)->int:
    # Level intentionally does not participate.  This is the exact editor/game
    # upkeep formula supplied by the original editor source and confirmed by
    # the native controls (50->6, 90->22, 260->130, 70->18 at defaults).
    unit=catalog.get('units',{}).get(str(uid),{})
    if not unit:
        if strict:
            raise ValueError(f'Юнит ID {uid}: нет данных для расчёта daily payment.')
        return 0
    return max(0,calculate_upkeep(unit,_catalog_cost_recruit_div(catalog)))


def rebuild_composition(block:bytearray, old_rec:ArmyRecord|None, new_rec:ArmyRecord, source_templates:dict, catalog:dict,
                        source_payment:int|None=None, payment_profiles:dict|None=None, *, strict_derived:bool=False, preserve_existing_formation:bool=False):
    comp=new_rec.expanded()
    if len(comp)>12: raise ValueError('В runtime армии помещается не более 12 персонажей.')
    old_native=native_comp_from_block(bytes(block))
    old_records=[bytes(block[UNIT_BASE_OFF+i*UNIT_STRIDE:UNIT_BASE_OFF+(i+1)*UNIT_STRIDE]) for i in range(len(old_native))]
    old_spell=old_rec.applied_spell if old_rec is not None else None
    old_items=old_rec.items if old_rec is not None else None
    old_named=old_rec.named_unit_id if old_rec is not None else None
    semantic_army_effect_changed=(
        old_rec is not None and
        (old_spell!=new_rec.applied_spell or old_items!=new_rec.items or old_named!=new_rec.named_unit_id)
    )

    # Sequence reuse of unchanged units preserves XP/HP and opaque runtime
    # fields.  Only synthesize/rewrite a record when it is new or an army-wide
    # item/spell/named-unit change requires it.
    used=set(); records=[]
    item_dist=distribute_items(comp,new_rec.items,catalog,new_rec.applied_spell)
    for idx,(uid,lvl,is_main) in enumerate(comp):
        chosen=None
        if idx<len(old_native) and old_native[idx]==(uid,lvl) and idx not in used: chosen=idx
        if chosen is None:
            for j,pair in enumerate(old_native):
                if j not in used and pair==(uid,lvl): chosen=j; break
        template=None
        if chosen is not None:
            used.add(chosen); template=old_records[chosen]
            if not semantic_army_effect_changed:
                records.append(template)
                continue
        else:
            template=source_templates.get((uid,lvl))
        records.append(make_unit_record(
            uid,lvl,is_main,new_rec.named_unit_id,new_rec.applied_spell,item_dist[idx],catalog,template,
            cold_added_unit=(chosen is None), strict_derived=strict_derived,
        ))

    block[UNIT_BASE_OFF:UNIT_AREA_END]=b'\0'*(UNIT_AREA_END-UNIT_BASE_OFF)
    struct.pack_into('<I',block,UNIT_COUNT_OFF,len(records))
    for i,u in enumerate(records): block[UNIT_BASE_OFF+i*UNIT_STRIDE:UNIT_BASE_OFF+(i+1)*UNIT_STRIDE]=u
    struct.pack_into('<I',block,AGG_STRENGTH_OFF,sum(struct.unpack_from('<I',u,0x1AE)[0] for u in records))
    form=(_resort_existing_formation(bytes(block),records) if preserve_existing_formation else _formation(records))
    for i,v in enumerate(form): struct.pack_into('<i',block,FORMATION_OFF+i*4,v)
    struct.pack_into('<I',block,HP_SUM_OFF,sum(max(0,struct.unpack_from('<i',u,0xDD+1)[0]) for u in records))

    new_keys=[(uid,lvl) for uid,lvl,_ in comp]
    if source_payment is not None and old_rec is not None:
        old_keys=[(u,l) for u,l,_ in old_rec.expanded()]
        payment=source_payment
        remaining=list(old_keys)
        # Main troop is free in the aggregate payment.  Compare only non-main
        # troop multisets for structural add/remove deltas.
        old_nonmain=old_keys[1:] if old_keys else []
        new_nonmain=new_keys[1:] if new_keys else []
        remaining=list(old_nonmain)
        for k in new_nonmain:
            if k in remaining: remaining.remove(k)
            else: payment += _payment_guess(k[0],k[1],catalog,payment_profiles,strict=strict_derived)
        for k in remaining:
            payment -= _payment_guess(k[0],k[1],catalog,payment_profiles,strict=strict_derived)
    else:
        payment=sum(_payment_guess(u,l,catalog,payment_profiles,strict=strict_derived) for u,l in new_keys[1:])
    struct.pack_into('<I',block,PAYMENT_OFF,max(0,payment))
    return records


def choose_representative(source_blocks:list[bytes], source_recs:list[ArmyRecord], new:ArmyRecord):
    desired=1-new.activity
    for b,r in zip(source_blocks,source_recs):
        if r.map_model==new.map_model and b[ACTIVE_OFF]==desired: return b
    for b,r in zip(source_blocks,source_recs):
        if b[ACTIVE_OFF]==desired: return b
    return source_blocks[0] if source_blocks else bytes(RUNTIME_ARMY_SIZE)


def sync_army_tail(source_tail:bytes, old_recs:list[ArmyRecord], new_recs:list[ArmyRecord], mapping:list[int|None], catalog:dict, *, strict_derived:bool=True):
    old_count=len(old_recs); new_count=len(new_recs)
    need=(old_count+1)*RUNTIME_ARMY_SIZE
    if len(source_tail)<need: raise ValueError('Динамический хвост SAV короче массива runtime-армий.')
    hero=source_tail[:RUNTIME_ARMY_SIZE]
    source_blocks=[source_tail[(i+1)*RUNTIME_ARMY_SIZE:(i+2)*RUNTIME_ARMY_SIZE] for i in range(old_count)]
    residual=bytearray(source_tail[(old_count+1)*RUNTIME_ARMY_SIZE:])
    templates=_find_templates(source_blocks)
    payment_profiles=_learn_payment_profiles(source_blocks,old_recs,catalog)
    out_blocks=[]
    rebuilt_indices=set()
    formation_relaxed_indices=set()
    for ti,newr in enumerate(new_recs):
        si=mapping[ti] if ti<len(mapping) else None
        if si is not None:
            moved=source_blocks[si]
            # Destination slot header stays with slot when records shift; body moves with army.
            dest_header=(source_blocks[ti][:0x800] if ti<old_count else (residual[:0x800] if len(residual)>=0x800 else moved[:0x800]))
            block=bytearray(dest_header + moved[0x800:])
            oldr=old_recs[si]
            oldcomp=[(u,l) for u,l,_ in oldr.expanded()]
            curcomp=native_comp_from_block(bytes(block))
            newcomp=[(u,l) for u,l,_ in newr.expanded()]
            unit_semantics_changed=(
                newcomp!=oldcomp or oldr.items!=newr.items or
                oldr.applied_spell!=newr.applied_spell or
                oldr.named_unit_id!=newr.named_unit_id
            )
            if unit_semantics_changed:
                if curcomp!=oldcomp:
                    raise ValueError(f'Армия ID {oldr.army_id}: состав в SAV уже отличается от исходной DTm; изменение состава/экипировки пропущено для безопасности.')
                source_payment=struct.unpack_from('<I',block,PAYMENT_OFF)[0]
                preserve_formation=(newcomp==oldcomp)
                rebuild_composition(
                    block,oldr,newr,templates,catalog,source_payment,payment_profiles,
                    strict_derived=strict_derived,preserve_existing_formation=preserve_formation,
                )
                rebuilt_indices.add(ti)
                if preserve_formation:
                    formation_relaxed_indices.add(ti)
            patch_direct_fields(block,oldr,newr,True,catalog)
        else:
            # New army: would-be slot header from source residual; post-unit defaults from a similar native army.
            header=(
                source_blocks[ti][:0x800] if ti<old_count else
                (bytes(residual[:0x800]) if len(residual)>=0x800 else bytes(0x800))
            )
            rep=choose_representative(source_blocks,old_recs,newr)
            block=bytearray(header + rep[0x800:])
            block[UNIT_BASE_OFF:UNIT_AREA_END]=b'\0'*(UNIT_AREA_END-UNIT_BASE_OFF)
            # Representative body may advertise units we just cleared.
            struct.pack_into('<I',block,UNIT_COUNT_OFF,0)
            rebuild_composition(block,None,newr,templates,catalog,None,payment_profiles,strict_derived=strict_derived)
            rebuilt_indices.add(ti)
            patch_direct_fields(block,None,newr,False,catalog)
            # Fresh native NPCs use this dormant timer before activation; keep representative when available.
            struct.pack_into('<I',block,0x1F14,2500)
        out_blocks.append(block)
    # Cross-slot header fields describe previous army, update them after body construction.
    for i,b in enumerate(out_blocks):
        if i>0:
            prev=new_recs[i-1]
            struct.pack_into('<H',b,PREV_COST_OFF,_prev_cost(prev,catalog))
            b[PREV_XP_CORR_OFF]=prev.xp_correction
    if residual:
        # Residual begins with the next unused slot header. Update its previous-army descriptors.
        if new_recs and len(residual)>PREV_XP_CORR_OFF:
            struct.pack_into('<H',residual,PREV_COST_OFF,_prev_cost(new_recs[-1],catalog))
            residual[PREV_XP_CORR_OFF]=new_recs[-1].xp_correction
        # When appending, native advances the pointer-like next-slot address.
        if new_count>old_count and len(residual)>=SLOT_POINTER_OFF+4:
            v=struct.unpack_from('<I',residual,SLOT_POINTER_OFF)[0]
            if v>=0x2730: struct.pack_into('<I',residual,SLOT_POINTER_OFF,v-0x2730*(new_count-old_count))
    result=hero + b''.join(bytes(b) for b in out_blocks) + bytes(residual)
    verified=verify_synced_tail(
        result,new_recs,catalog,rebuilt_indices=rebuilt_indices,
        formation_relaxed_indices=formation_relaxed_indices,
    )
    if not verified['ok']:
        raise ValueError('Построенный runtime-хвост не прошёл строгую проверку: ' + '; '.join(verified['issues']))
    return result


def remap_id(value:int, deleted_old_ids:set[int], old_to_new:dict[int,int], *, deleted_value=0):
    if value==0 or value==0xFF: return value
    if value in deleted_old_ids: return deleted_value
    return old_to_new.get(value,value)


def remap_event_army_refs(record:bytes, deleted_old_ids:set[int], old_to_new:dict[int,int])->bytes:
    """Remap every and only known Army-ID byte in one 171-byte EventData."""
    if len(record)!=171:
        raise ValueError(f'EventData must be 171 bytes, got {len(record)}')
    out=bytearray(record)
    for off in EVENT_ARMY_REF_OFFSETS:
        out[off]=remap_id(out[off],deleted_old_ids,old_to_new,deleted_value=0)
    return bytes(out)


def decode_army_cell_marker(marker:int, kind:int, *, max_old_id:int|None=None):
    """Return 1-based army ID from compiled cell fields 3/4, or None."""
    lo=marker & 0xFF; hi=(marker >> 8) & 0xFF
    if kind!=CELL_ARMY_KIND or lo!=hi or lo<2:
        return None
    army_id=lo-1
    if max_old_id is not None and not (1<=army_id<=max_old_id):
        return None
    return army_id


def encode_army_cell_marker(army_id:int):
    if not (1<=army_id<=254):
        raise ValueError(f'army_id out of marker range: {army_id}')
    return (army_id+1)*0x0101, CELL_ARMY_KIND
