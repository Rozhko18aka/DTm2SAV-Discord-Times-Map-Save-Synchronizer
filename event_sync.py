"""Event identity, reference maintenance and physical event removal (MapLDV V.4).

Reference schema cross-checked against the supplied native map and
https://github.com/LedinecMing/DiscordTimes_Remastered/blob/dev-master/dt_lib/src/map/convert.rs
Lantern bindings are 16 little-endian u16 values, not 32 independent u8 IDs.
"""
from collections import Counter
from dataclasses import dataclass
import copy
import struct

import sav_tool as st

EVENT_REFS = (57, 59, 62, 64, 70, 72, 77, 124, 138)
SETTING_REFS = (210, 216)


@dataclass
class MapData:
    header: bytearray
    sections: dict
    gap: bytes
    texts: dict
    suffix: bytes

    @classmethod
    def read(cls, inner):
        sections, _ = st.dtm_layout(inner)
        strings, text_end = st.parse_texts_from_map(inner, sections)
        start = st.u32(inner, 24)
        if start < sections['events']['end']:
            raise st.SavError('Тексты DTm пересекаются с секциями карты.')
        blocks = {key: inner[s['offset']:s['end']] for key, s in sections.items()}
        texts = {'prefix': strings[:4]}
        pos = 4
        for key in ('buildings', 'armies', 'events'):
            n = sections[key]['count'] * 3
            texts[key] = strings[pos:pos+n]
            pos += n
        texts['suffix'] = strings[pos:]
        return cls(bytearray(inner[:st.DTM_SECTION_BASE]), blocks,
                   inner[sections['events']['end']:start], texts, inner[text_end:])

    def clone(self):
        return copy.deepcopy(self)

    def records(self, key):
        size = dict(st.SECTION_SPECS)[key]
        b = self.sections[key]
        return [b[i:i+size] for i in range(0, len(b), size)]

    def encode(self):
        header = bytearray(self.header)
        blocks = [self.sections[key] for key, _ in st.SECTION_SPECS]
        struct.pack_into('<6I', header, 28, *(len(b) for b in blocks))
        struct.pack_into('<I', header, 24, len(header)+sum(map(len, blocks))+len(self.gap))
        strings = sum((self.texts[k] for k in ('prefix', 'buildings', 'armies', 'events', 'suffix')), [])
        return bytes(header) + b''.join(blocks) + self.gap + b''.join(v+b'\0' for v in strings) + self.suffix


def remap_record(raw, offsets, mapping, *, bindings=False):
    out = bytearray(raw)
    values = []
    for off in offsets:
        value = struct.unpack_from('<H', raw, off)[0]
        if value == 0:
            values.append(0)
        elif value not in mapping:
            if not bindings:
                raise ValueError(f'Ссылка на удаляемое или неизвестное событие ID {value} (поле +{off}).')
            values.append(0)
        else:
            values.append(mapping[value])
    if bindings:
        values = [v for v in values if v] + [0]*values.count(0)
    for off, value in zip(offsets, values):
        struct.pack_into('<H', out, off, value)
    return bytes(out)


def rebase_map_refs(doc, mapping, *, preserved_event_indices=()):
    try:
        doc.header[:] = remap_record(doc.header, SETTING_REFS, mapping)
    except ValueError as exc:
        raise ValueError(f'Условие победы/поражения: {exc}') from exc
    events=[]
    for i,raw in enumerate(doc.records('events'),1):
        if i-1 in preserved_event_indices:
            events.append(raw)
            continue
        try:
            events.append(remap_record(raw,EVENT_REFS,mapping))
        except ValueError as exc:
            raise ValueError(f'Событие ID {i}: {exc}') from exc
    doc.sections['events'] = b''.join(events)
    buildings = []
    for raw in doc.records('buildings'):
        rec = bytearray(remap_record(raw, range(8, 136, 2), mapping, bindings=True))
        if rec != raw:
            rec[288] = sum(struct.unpack_from('<H', rec, o)[0] != 0 for o in range(8,136,2))
        buildings.append(bytes(rec))
    doc.sections['buildings'] = b''.join(buildings)
    doc.sections['lanterns'] = b''.join(remap_record(r, range(6,38,2), mapping, bindings=True)
                                         for r in doc.records('lanterns'))


def signature(raw):
    data = bytearray(raw)
    for off in EVENT_REFS:
        data[off:off+2] = b'\0\0'
    return bytes(data)


def align_events(old, new):
    """Use unique complete records/texts, then stable nonempty titles.

    Positional edits are allowed only in equal-sized gaps between anchors.
    Never infer which duplicate was deleted from a changed-size gap.
    """
    a, b = old.records('events'), new.records('events')
    at, bt = old.texts['events'], new.texts['events']
    if a == b and at == bt:
        return list(range(len(a)))
    result = [None] * len(b)
    used = set()
    for keys_a, keys_b in (
        ([(signature(r), tuple(at[i*3:i*3+3])) for i,r in enumerate(a)],
         [(signature(r), tuple(bt[i*3:i*3+3])) for i,r in enumerate(b)]),
        ([at[i*3] for i in range(len(a))], [bt[i*3] for i in range(len(b))]),
    ):
        ca, cb = Counter(keys_a), Counter(keys_b)
        lookup = {key:i for i,key in enumerate(keys_a) if ca[key] == 1 and key}
        for j, key in enumerate(keys_b):
            i = lookup.get(key)
            if result[j] is None and i is not None and i not in used and cb[key] == 1:
                result[j] = i
                used.add(i)
    anchors = [(-1,-1)] + [(j,i) for j,i in enumerate(result) if i is not None] + [(len(b),len(a))]
    if any(i2 < i1 for (_,i1),(_,i2) in zip(anchors, anchors[1:])):
        if any(i is None for i in result):
            raise ValueError('Неоднозначная перестановка событий: сохраните уникальные названия и повторите анализ.')
        return result
    for (j1,i1),(j2,i2) in zip(anchors, anchors[1:]):
        old_gap, new_gap = list(range(i1+1,i2)), list(range(j1+1,j2))
        if len(old_gap) == len(new_gap):
            for j,i in zip(new_gap,old_gap):
                result[j] = i
        elif old_gap and new_gap:
            raise ValueError('Нельзя однозначно сопоставить изменённые и удалённые события. Сохраните их уникальные названия.')
    return result


def normalize_events(old, requested, *, progressed_indices=()):
    """Rebase editor event IDs into original slots; removals commit last."""
    mapping = align_events(old, requested)
    doc = requested.clone()
    old_records, requested_records = old.records('events'), requested.records('events')
    records = list(old_records)
    texts = list(old.texts['events'])
    target_to_stable = {}
    for target, source in enumerate(mapping):
        stable = source if source is not None else len(records)
        target_to_stable[target+1] = stable+1
        if source is None:
            records.append(requested_records[target])
            texts.extend(requested.texts['events'][target*3:target*3+3])
        else:
            records[source] = requested_records[target]
            texts[source*3:source*3+3] = requested.texts['events'][target*3:target*3+3]
    # Rebase only records actually present in the editor; restored slots already
    # use original IDs and must not be interpreted in the editor namespace.
    # Editor changes to progressed events are discarded by the caller. Their
    # references must not be validated in the editor namespace first: even a
    # dangling editor reference is irrelevant to the preserved SAV record.
    preserved = {target for target, source in enumerate(mapping)
                 if source is not None and source in progressed_indices}
    rebase_map_refs(doc, target_to_stable, preserved_event_indices=preserved)
    rebased = doc.records('events')
    for target, stable in target_to_stable.items():
        records[stable-1] = rebased[target-1]
    doc.sections['events'] = b''.join(records)
    doc.texts['events'] = texts
    removed = sorted(set(range(len(old_records))) - {v for v in mapping if v is not None})
    return doc, removed, target_to_stable


def delete_from_map(doc, indices):
    deleted = set(indices)
    records = doc.records('events')
    if not deleted <= set(range(len(records))):
        raise ValueError('Неверный ID события для удаления.')
    keep = [i for i in range(len(records)) if i not in deleted]
    mapping = {old+1:new+1 for new,old in enumerate(keep)}
    out = doc.clone()
    out.sections['events'] = b''.join(records[i] for i in keep)
    out.texts['events'] = sum((doc.texts['events'][i*3:i*3+3] for i in keep), [])
    rebase_map_refs(out, mapping)
    return out, mapping


def remove_from_payload(plan, payload, indices):
    """Compact the event/text arrays and maintain known references atomically."""
    indices = set(indices)
    if not indices:
        return payload, plan.modified_inner
    doc = MapData.read(plan.modified_inner)
    rel = st.relate(payload, plan.modified_inner, plan.container_info)['relationship']
    start = rel['events']['offset']
    count = len(doc.records('events'))
    records = [payload[start+i*171:start+(i+1)*171] for i in range(count)]
    old_events = plan.original_sections['events']['offset']
    for i in sorted(indices):
        if not 0 <= i < count:
            raise ValueError(f'Событие ID {i+1} не существует.')
        if i < plan.event_count:
            before = plan.payload[plan.event_start+i*171:plan.event_start+(i+1)*171]
            original = plan.original_inner[old_events+i*171:old_events+(i+1)*171]
            if before != original:
                raise ValueError(f'Событие ID {i+1} уже затронуто игрой; удаление потеряет его прогресс.')
        if struct.unpack_from('<H', records[i], 164)[0]:
            raise ValueError(f'Событие ID {i+1} содержит встроенную картинку; удаление её данных не поддерживается.')
    final_doc, mapping = delete_from_map(doc, indices)
    out = bytearray(payload)
    # Verify incoming dependencies in actual SAV, including progressed records.
    kept = []
    for i, raw in enumerate(records):
        if i not in indices:
            try:
                kept.append(remap_record(raw, EVENT_REFS, mapping))
            except ValueError as exc:
                raise ValueError(f'Событие ID {i+1}: {exc}') from exc
    header = plan.map_header_offset
    out[header:header+289] = remap_record(out[header:header+289], SETTING_REFS, mapping)
    for off in rel['buildings']['record_offsets']:
        raw = out[off:off+358]
        rec = bytearray(remap_record(raw, range(8,136,2), mapping, bindings=True))
        if rec != raw:
            rec[288] = sum(struct.unpack_from('<H',rec,o)[0] != 0 for o in range(8,136,2))
        out[off:off+358] = rec
    for i in range(plan.modified_lantern_count):
        off = rel['lanterns']['offset']+i*99
        out[off:off+99] = remap_record(out[off:off+99], range(6,38,2), mapping, bindings=True)
    final_map = final_doc.encode()
    struct.pack_into('<I', out, header+24, st.u32(final_map,24))
    struct.pack_into('<I', out, header+48, len(kept)*171)
    struct.pack_into('<I', out, rel['count_signature_offset']+12, len(kept))
    strings, _ = st.read_cstrings_raw(payload, rel['texts']['offset'], len(plan.save_strings)+
        3*(plan.modified_building_count-plan.building_count+plan.modified_army_count-plan.army_count+count-plan.event_count))
    base = 2+3*(plan.modified_building_count+plan.modified_army_count)
    filtered = strings[:base] + sum((strings[base+i*3:base+i*3+3] for i in range(count) if i not in indices), []) + strings[base+count*3:]
    result = (bytes(out[:start])+b''.join(kept)+bytes(out[start+count*171:rel['texts']['offset']])+
              b''.join(v+b'\0' for v in filtered)+payload[rel['texts']['end']:])
    st.relate(result, final_map, plan.container_info)
    return result, final_map


def validate_payload_references(plan, payload, inner):
    rel = st.relate(payload,inner,plan.container_info)['relationship']
    count = rel['events']['count']
    records=[]
    records.append(('Условия победы/поражения',payload[plan.map_header_offset:plan.map_header_offset+289],SETTING_REFS))
    for i in range(count):
        off=rel['events']['offset']+i*171
        records.append((f'Событие ID {i+1}',payload[off:off+171],EVENT_REFS))
    for i,off in enumerate(rel['buildings']['record_offsets'],1):
        records.append((f'Строение ID {i}',payload[off:off+358],range(8,136,2)))
    sections,_=st.dtm_layout(inner)
    for i in range(sections['lanterns']['count']):
        off=rel['lanterns']['offset']+i*99
        records.append((f'Фонарь ID {payload[off+4]}',payload[off:off+99],range(6,38,2)))
    for label,raw,offsets in records:
        for off in offsets:
            value=struct.unpack_from('<H',raw,off)[0]
            if value>count:
                raise ValueError(f'{label}: ссылка на отсутствующее событие ID {value} (поле +{off}).')
    return rel
