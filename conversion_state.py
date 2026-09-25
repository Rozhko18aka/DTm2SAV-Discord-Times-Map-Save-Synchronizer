"""Local repeat-conversion state, keyed by the exact SAV content hash."""
import bz2
import os
from pathlib import Path
import sav_tool as st

MAGIC=b'DTm2SAV-state-v1\0'

def state_path(save_hash):
    if len(save_hash)!=64 or any(c not in '0123456789abcdef' for c in save_hash):
        raise ValueError('Invalid save hash')
    folder=os.environ.get('DTM2SAV_STATE_DIR')
    if not folder:
        folder=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'DTm2SAV/baselines'
    return Path(folder)/(save_hash+'.state')

def read(save_hash):
    path=state_path(save_hash)
    if not path.is_file():return None
    data=st.read_bounded(path,st.MAX_DTM_BYTES)
    start=len(MAGIC)
    if not data.startswith(MAGIC) or len(data)<start+32:
        raise st.SavError('Повреждён кэш повторного переноса.')
    inner=st.decompress_bounded(data[start+32:],st.MAX_DTM_BYTES,'conversion state')
    if st.sha256(inner)!=data[start:start+32].hex():
        raise st.SavError('Контрольная сумма кэша повторного переноса не совпадает.')
    st.validate_map_geometry(inner)
    return inner

def write(save_hash, inner):
    from dtm_sav_quest_sync import atomic_write
    path=state_path(save_hash);path.parent.mkdir(parents=True,exist_ok=True)
    data=MAGIC+bytes.fromhex(st.sha256(inner))+bz2.compress(inner)
    atomic_write(path,data,overwrite=True)
