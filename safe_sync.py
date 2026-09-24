"""Preview and explicitly commit a verified subset of independent map edits."""
from dataclasses import dataclass, field
from pathlib import Path
import struct

import army_runtime as ar
import building_runtime as br
import dtm_sav_quest_sync as qs
import event_sync as es
import sav_tool as st


@dataclass
class Change:
    key: str
    label: str
    details: str
    sections: dict = field(default_factory=dict)
    texts: dict = field(default_factory=dict)
    patches: list = field(default_factory=list)
    header: list = field(default_factory=list)
    deletion: int | None = None
    unsupported: str | None = None
    terrain: dict = field(default_factory=dict)

    def apply(self, doc):
        doc.sections.update(self.sections)
        for key, values in self.texts.items():
            if isinstance(key, tuple):
                name, index = key
                doc.texts[name][index*3:index*3+3] = values
            else:
                doc.texts[key] = list(values)
        for name, offset, value in self.patches:
            data = bytearray(doc.sections[name])
            data[offset:offset+len(value)] = value
            doc.sections[name] = bytes(data)
        for offset, value in self.header:
            doc.header[offset:offset+len(value)] = value

    def blocked(self, reason):
        return {'key': self.key, 'data': self.label, 'changes': self.details,
                'reason': str(reason), 'suggestion': 'Сохранить остальные изменения; эти данные оставить из исходного SAV.'}


@dataclass
class PreparedConversion:
    plan: qs.SyncPlan
    payload: bytes
    report: dict
    final_map: bytes
    blocked: list
    applied: list


def _record_groups(key, size):
    if key == 'armies':
        semantic = [(26,46),(50,53),(58,59),(84,85)]
        groups=[('состав, экипировка, именной боец и заклинание',semantic),
                ('координаты',[(0,4)]),('патруль',[(60,62)]),
                ('доход золота',[(17,19)]),('активность',[(63,64)]),
                ('группа и отношения',[(64,69)]),('агрессия',[(69,70)]),
                ('возрождение',[(70,71)]),('стартовое строение',[(25,26)])]
        return groups+[('прочие параметры',_complement(size,[r for _,ranges in groups for r in ranges]))]
    if key == 'buildings':
        # Type, dimensions and visual footprint are interdependent. Garrison
        # semantics form a separate transaction when the type stays unchanged.
        groups=[('гарнизон, защита и добыча',[(136,148),(314,337)]),
                ('положение и вид',[(0,8),(289,291)]),
                ('привязанные события',[(8,136),(288,289)]),
                ('доход',[(282,286),(350,352)]),('найм',[(264,282)])]
        return groups+[('прочие параметры',_complement(size,[r for _,ranges in groups for r in ranges]))]
    return [('параметры', [(0,size)])]


def _complement(size, ranges):
    occupied = {i for a,b in ranges for i in range(a,b)}
    result = []
    for i in range(size):
        if i not in occupied:
            if result and result[-1][1] == i:
                result[-1] = (result[-1][0],i+1)
            else:
                result.append((i,i+1))
    return result


def collect_changes(old, new):
    changes = []
    labels = {'buildings':'Строение', 'armies':'Армия', 'events':'Событие', 'lanterns':'Фонарь'}
    structural = any(len(old.sections[k]) != len(new.sections[k]) for k in ('buildings','armies'))
    # Reordered IDs also require one transaction for dependent references.
    if not structural:
        am, deleted = ar.align_armies(old.records('armies'),new.records('armies'))
        bm, bd = br.align_buildings(old.records('buildings'),new.records('buildings'))
        structural = am != list(range(len(am))) or bm != list(range(len(bm))) or bool(deleted or bd)
    grouped = set()
    if structural:
        grouped = {'buildings','armies','lanterns','events'}
        changes.append(Change('structure', 'Структура объектов и связанные события',
            'Добавление/удаление/перенумерация армий или строений и их зависимые ссылки (единая группа).',
            sections={k:new.sections[k] for k in grouped},
            texts={k:new.texts[k] for k in grouped if k in new.texts},
            header=[(o,bytes(new.header[o:o+n])) for o,n in [(76,1),(126,1),(176,1),(210,2),(216,2)]]))
    for key,size in (('buildings',358),('armies',89),('lanterns',99),('events',171)):
        if key in grouped:
            continue
        before, after = old.records(key), new.records(key)
        if key == 'lanterns' and len(before) != len(after):
            changes.append(Change('lanterns:structure','Список фонарей','Добавление/удаление и связанные изменения фонарей.',sections={key:new.sections[key]}))
            continue
        for i, raw in enumerate(after[:len(before)]):
            oldraw = before[i]
            groups = _record_groups(key,size)
            if key == 'buildings' and raw[6] != oldraw[6]:
                groups = [('тип и все параметры строения',[(0,size)])]
            for group,ranges in groups:
                patches = [(key,i*size+a,raw[a:b]) for a,b in ranges if raw[a:b] != oldraw[a:b]]
                if patches:
                    changed = bytearray(oldraw)
                    for a,b in ranges:
                        changed[a:b] = raw[a:b]
                    details = '; '.join(qs.describe_binary_changes(oldraw,bytes(changed)))
                    changes.append(Change(f'{key}:{i}:{group}',f'{labels[key]} ID {i+1}: {group}',details,patches=patches))
            if key in new.texts:
                a,b = old.texts[key][i*3:i*3+3],new.texts[key][i*3:i*3+3]
                if a != b:
                    changes.append(Change(f'{key}:{i}:text',f'{labels[key]} ID {i+1}: текст',
                        '; '.join(qs.describe_event_text_changes(tuple(a),tuple(b))),texts={(key,i):b}))
        if len(after) > len(before):
            # Appended events must remain contiguous; do not leave ID holes.
            changes.append(Change(f'{key}:append',f'Новые события ID {len(before)+1}–{len(after)}',
                'Новые записи и их тексты (единая группа).',
                patches=[(key,len(before)*size,b''.join(after[len(before):]))],
                texts={(key,len(before)):new.texts[key][len(before)*3:]}))
    for key,label in (('decorations','Декорации'),):
        if old.sections[key] != new.sections[key]:
            changes.append(Change(key,label,'Изменения секции карты.',sections={key:new.sections[key]}))
    width,height = struct.unpack_from('<II',old.header,12)
    def tiles(doc):
        blob=doc.sections['surface']
        return qs.decode_surface(blob,{'offset':0,'end':len(blob)},width*height)
    for i,(a,b) in enumerate(zip(tiles(old),tiles(new))):
        if a != b:
            changes.append(Change(f'terrain:{i}',f'Ландшафт ({i%width}, {i//width})',
                                  f'Тип клетки: {a} → {b}',terrain={i:b}))
    if not structural:
        for off in (*br.HERO_START_BUILDING_OFFSETS,*es.SETTING_REFS):
            size = 2 if off in es.SETTING_REFS else 1
            if old.header[off:off+size] != new.header[off:off+size]:
                changes.append(Change(f'header:{off}',f'Настройка карты +{off}','Изменение ссылки в настройках.',
                    header=[(off,bytes(new.header[off:off+size]))]))
    # +0x124..0x125 is the editor save counter, not a gameplay setting.
    known=set(range(24,52)) | {0x124,0x125} | set(br.HERO_START_BUILDING_OFFSETS) | {i for o in es.SETTING_REFS for i in (o,o+1)}
    unknown=[i for i,(a,b) in enumerate(zip(old.header,new.header)) if a!=b and i not in known]
    if unknown:
        changes.append(Change('unsupported:header','Прочие настройки карты',
            'Поля: '+', '.join(f'+0x{i:X}: {old.header[i]:02X} → {new.header[i]:02X}' for i in unknown),
            unsupported='Перенос этих полей заголовка DTm в сохранение ещё не поддерживается.'))
    for name in ('prefix','suffix'):
        if old.texts[name] != new.texts[name]:
            changes.append(Change(f'unsupported:text:{name}','Общие тексты / именные персонажи',
                f'Изменена группа текстов {name}.',unsupported='Перенос общих текстов и именных персонажей не поддерживается.'))
    if old.gap!=new.gap or old.suffix!=new.suffix:
        changes.append(Change('unsupported:extra','Дополнительные данные DTm',
            'Изменены данные вне основных секций и текстов.',
            unsupported='Перенос дополнительных данных и пользовательских изображений не поддерживается.'))
    return changes


def prepare_paths(source, original, modified, objects_ugs=None, *, delete_event_ids=()):
    source, original, modified = map(Path,(source,original,modified))
    raw = st.read_bounded(source)
    old_bytes, requested_bytes = st.read_dtm(original),st.read_dtm(modified)
    old, requested = es.MapData.read(old_bytes),es.MapData.read(requested_bytes)
    if old.header[12:20] != requested.header[12:20]:
        raise qs.QuestSyncError('Размер карты изменён. Частичный перенос между разными размерами не поддерживается.')
    normalized, removed, target_to_stable = es.normalize_events(old,requested)
    for event_id in delete_event_ids:
        if event_id not in target_to_stable:
            raise qs.QuestSyncError(f'В изменённой карте нет события ID {event_id}.')
        removed.append(target_to_stable[event_id]-1)
    changes = collect_changes(old,normalized)
    for i in sorted(set(removed)):
        title = normalized.texts['events'][i*3].decode('cp1251','replace')
        changes.append(Change(f'delete:{i}',f'Удаление события ID {i+1}: {title}',
                              'Удалить запись и три текста события; обновить ссылки и номера.',deletion=i))
    expected_errors = (qs.QuestSyncError,st.SavError,ValueError,struct.error)

    def build(selected):
        doc = old.clone()
        deletions = []
        for change in selected:
            if change.unsupported:
                raise qs.QuestSyncError(change.unsupported)
            change.apply(doc)
            if change.deletion is not None:
                deletions.append(change.deletion)
        terrain={i:tile for change in selected for i,tile in change.terrain.items()}
        if terrain:
            blob=doc.sections['surface']
            width,height=struct.unpack_from('<II',doc.header,12)
            tiles=bytearray(qs.decode_surface(blob,{'offset':0,'end':len(blob)},width*height))
            for i,tile in terrain.items():
                tiles[i]=tile
            encoded=bytearray()
            pos=0
            while pos<len(tiles):
                end=pos+1
                while end<len(tiles) and tiles[end]==tiles[pos] and end-pos<256:
                    end+=1
                encoded.extend((tiles[pos],end-pos-1)); pos=end
            doc.sections['surface']=bytes(encoded)
        plan = qs.analyze_data(source,original,modified,raw,old_bytes,doc.encode(),objects_ugs)
        # Progressed events are an explicit blocked edit, never a silent no-op.
        conflicts=[]
        if plan.modified_progressed:
            am,ad=ar.align_armies(old.records('armies'),doc.records('armies'))
            army_ids={i+1:j+1 for j,i in enumerate(am) if i is not None}
            building_ids={i+1:j+1 for j,i in enumerate(plan.building_mapping) if i is not None}
            for item in plan.modified_progressed:
                old_record=old.records('events')[item.index]
                rebased=ar.remap_event_army_refs(old_record,{i+1 for i in ad},army_ids)
                rebased=br.remap_event_building_refs(rebased,{i+1 for i in plan.building_deleted_indices},building_ids)
                if item.text_changed_in_modified_map or rebased!=doc.records('events')[item.index]:
                    conflicts.append(item)
        if conflicts:
            ids = ', '.join(str(v.event_id) for v in conflicts)
            raise qs.QuestSyncError(f'События ID {ids} уже затронуты игрой: изменение записи или текста сбросит прогресс.')
        old_armies=old.records('armies'); new_armies=doc.records('armies')
        army_mapping,_=ar.align_armies(old_armies,new_armies)
        for target,source_index in enumerate(army_mapping):
            if source_index is None or old_armies[source_index]==new_armies[target]:
                continue
            off=plan.text_end+(source_index+1)*ar.RUNTIME_ARMY_SIZE
            block=bytearray(plan.payload[off:off+ar.RUNTIME_ARMY_SIZE])
            skipped=[]
            ar.patch_direct_fields(block,ar.parse_army_record(old_armies[source_index]),
                ar.parse_army_record(new_armies[target]),catalog=ar.load_catalog(),skipped=skipped)
            if skipped:
                raise qs.QuestSyncError(f'Армия ID {source_index+1}: параметры уже изменены в игре; '+ '; '.join(skipped))
        payload, report = qs.build_synced_payload(plan)
        payload, final_map = es.remove_from_payload(plan,payload,deletions)
        final_relation=es.validate_payload_references(plan,payload,final_map)
        report.update({'deleted_event_ids': [i+1 for i in deletions],
                       'output_event_count':plan.modified_event_count-len(deletions),
                       'event_size_delta':171*(plan.modified_event_count-len(deletions)-plan.event_count),
                       'output_payload_size':len(payload),'output_payload_sha256':st.sha256(payload),
                       'effective_map_sha256':st.sha256(final_map),
                       'event_reference_verifier':{'ok':True},
                       'text_size_delta':final_relation['texts']['end']-final_relation['texts']['offset']-(plan.text_end-plan.text_start)})
        return plan,payload,report,final_map

    # Fatal input/format failures must not be interpreted as skippable edits.
    baseline = build([])
    accepted, rejected = [], []
    result = baseline
    def attempt(batch):
        nonlocal result
        if not batch:
            return
        try:
            candidate = build(accepted+batch)
        except expected_errors as exc:
            if len(batch) == 1:
                rejected.append((batch[0],str(exc)))
            else:
                mid = len(batch)//2
                attempt(batch[:mid])
                attempt(batch[mid:])
        else:
            accepted.extend(batch)
            result = candidate
    attempt(changes)
    # Retry after dependencies have become available. Full-batch trial also
    # permits mutually dependent edits when they form a valid transaction.
    while rejected:
        before = len(accepted)
        batch = [change for change,_ in rejected]
        rejected = []
        attempt(batch)
        if len(accepted) == before:
            break
    blocked = [c.blocked(reason) for c,reason in rejected]
    plan,payload,report,final_map = result
    report.update({'partial_conversion':bool(blocked),'blocked_changes':blocked,
                   'applied_changes':[{'key':c.key,'data':c.label} for c in accepted],
                   'requested_modified_map_sha256':st.sha256(requested_bytes),
                   'requested_deleted_event_ids':sorted({i+1 for i in removed}),
                   'editor_event_ids':{stable:target for target,stable in target_to_stable.items()}})
    return PreparedConversion(plan,payload,report,final_map,blocked,accepted)


def save_prepared(prepared, output, *, accept_partial=False, write_report=True, allow_overwrite=False):
    if prepared.blocked and not accept_partial:
        raise qs.QuestSyncError('Есть заблокированные изменения. Подтвердите сохранение без них.')
    return qs.convert_plan(prepared.plan,output,write_report=write_report,allow_overwrite=allow_overwrite,
                           prepared_result=(prepared.payload,prepared.report),
                           save_name=Path(output).stem)
