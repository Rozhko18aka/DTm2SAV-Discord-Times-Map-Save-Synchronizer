from __future__ import annotations

import copy
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import army_runtime

REQUIRED_RELATIVE_PATHS = {
    "units": Path("Rus_Units.ini"),
    "artifacts": Path("Rus_Artefacts.ini"),
    "spells": Path("Rus_Spells.ini"),
    "global": Path("_Global.ini"),
    "objects": Path("Graphics") / "Objects" / "Objects.ugs",
}


class GameDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class GameResources:
    app_dir: Path
    units_path: Path
    artifacts_path: Path
    spells_path: Path
    global_path: Path
    objects_ugs: Path
    catalog: dict
    warnings: tuple[str, ...]
    changed_units: tuple[int, ...]
    changed_artifacts: tuple[int, ...]
    changed_spells: tuple[int, ...]
    global_changed: bool
    hashes: dict[str, str]

    @property
    def has_untested_data(self) -> bool:
        return bool(self.changed_units or self.changed_artifacts or self.changed_spells or self.global_changed)

    def warning_text(self, *, max_ids: int = 24) -> str:
        if not self.has_untested_data:
            return ""

        def fmt(values: tuple[int, ...]) -> str:
            shown = ", ".join(map(str, values[:max_ids]))
            if len(values) > max_ids:
                shown += f", … (+{len(values) - max_ids})"
            return shown or "—"

        lines = [
            "Обнаружены игровые параметры, отличающиеся от встроенного профиля, на котором проводились native-тесты.",
            "Приложение будет использовать значения из текущих файлов игры и разрешит сохранение, но для этих изменений тестирование не проводилось.",
        ]
        if self.changed_units:
            lines.append(f"Юниты: {fmt(self.changed_units)}")
        if self.changed_artifacts:
            lines.append(f"Артефакты: {fmt(self.changed_artifacts)}")
        if self.changed_spells:
            lines.append(f"Заклинания: {fmt(self.changed_spells)}")
        if self.global_changed:
            lines.append("_Global.ini: CostRecruitDiv отличается от проверенного значения 2.")
        return "\n".join(lines)


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _decode_legacy_text(path: Path) -> str:
    data = path.read_bytes()
    errors: list[str] = []
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
    raise GameDataError(f"Не удалось декодировать {path.name}: {'; '.join(errors)}")


def parse_legacy_ini(path: Path) -> list[tuple[str, dict[str, str]]]:
    """Parse Discord Times INI while preserving duplicate section names/order."""
    text = _decode_legacy_text(path)
    sections: list[tuple[str, dict[str, str]]] = []
    current_name: str | None = None
    current: dict[str, str] | None = None
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith((";", "#", "//")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_name = line[1:-1].strip()
            current = {}
            sections.append((current_name, current))
            continue
        if current is None:
            # Legacy files may contain harmless preamble text before section 1.
            continue
        if "=" not in raw:
            # Original Discord Times INI files may contain visual separators
            # inside a section (for example a line made only of dashes in
            # Rus_Spells.ini).  They carry no data and must not make the
            # resource loader reject an otherwise valid game installation.
            if line and all(ch in "-_=*~" for ch in line):
                continue
            raise GameDataError(f"{path.name}:{line_no}: ожидалось Имя=Значение")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise GameDataError(f"{path.name}:{line_no}: пустое имя параметра")
        current[key] = value.strip()
    if not sections:
        raise GameDataError(f"{path.name}: не найдено ни одной INI-секции")
    return sections


def _leading_int(text: str) -> int | None:
    token = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    try:
        value = int(token)
    except ValueError:
        return None
    return value if value > 0 else None


def _index_sections(path: Path, *, kind: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for section, values in parse_legacy_ini(path):
        raw_id = values.get("GlobalIndex", "").strip()
        try:
            item_id = int(raw_id) if raw_id else _leading_int(section)
        except ValueError as exc:
            raise GameDataError(f"{path.name}: неверный GlobalIndex в секции [{section}]") from exc
        if item_id is None or item_id <= 0:
            # Ignore explicit metadata sections, but not data-looking sections.
            if any(k in values for k in ("Name", "Cost", "Hits", "Type")):
                raise GameDataError(f"{path.name}: не удалось определить ID секции [{section}]")
            continue
        key = str(item_id)
        if key in out:
            raise GameDataError(f"{path.name}: повторяется ID {item_id}")
        entry = {"_section": section}
        entry.update(values)
        entry.setdefault("GlobalIndex", key)
        out[key] = entry
    if not out:
        raise GameDataError(f"{path.name}: не найдено данных {kind}")
    return out


def _spell_sections(path: Path) -> list[dict[str, str]]:
    spells: list[dict[str, str]] = []
    for section, values in parse_legacy_ini(path):
        # Spell IDs are positional and the legacy file also contains trailing
        # service sections (for example MapEditorSpecialOptions).  Preserve
        # every section exactly in source order so no positional ID can shift.
        entry = {"_section": section}
        entry.update(values)
        spells.append(entry)
    if not spells:
        raise GameDataError(f"{path.name}: не найдено секций")
    return spells


def _global_values(path: Path) -> dict[str, str]:
    # _Global.ini exists in several legacy builds both with and without named
    # sections, so collect key=value pairs from the whole file.
    text = _decode_legacy_text(path)
    flat: dict[str, str] = {}
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith((";", "#", "//")) or (line.startswith("[") and line.endswith("]")):
            continue
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        if key:
            flat.setdefault(key, value.strip())
    if not flat:
        raise GameDataError(f"{path.name}: не удалось прочитать параметры")
    raw = flat.get("CostRecruitDiv", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 2.0
    if not math.isfinite(value) or value <= 0:
        value = 2.0
    # Preserve integer spelling when possible; army_runtime accepts str/float.
    flat["CostRecruitDiv"] = str(int(value)) if value.is_integer() else str(value)
    return flat


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_UNIT_COSMETIC = {"_section", "Name", "Descript", "IconIndex"}
_ARTIFACT_COSMETIC = {"_section", "Name", "Descript", "Icon"}
_SPELL_COSMETIC = {
    "_section", "Name", "Icon1", "Icon2", "Icon3",
    "ColorC1", "ColorC2", "ColorC3", "Effect1", "Effect2", "Effect3",
}


def _semantic(entry: dict[str, str] | None, ignored: set[str]) -> dict[str, str] | None:
    if entry is None:
        return None
    return {k: str(v).strip() for k, v in entry.items() if k not in ignored}


def _changed_dict_ids(current: dict[str, dict[str, str]], baseline: dict[str, dict[str, str]], ignored: set[str]) -> tuple[int, ...]:
    result: list[int] = []
    for key in sorted(set(current) | set(baseline), key=lambda x: int(x) if str(x).isdigit() else 10**9):
        if _semantic(current.get(key), ignored) != _semantic(baseline.get(key), ignored):
            try:
                result.append(int(key))
            except ValueError:
                continue
    return tuple(result)


def _changed_spell_ids(current: list[dict[str, str]], baseline: list[dict[str, str]]) -> tuple[int, ...]:
    result: list[int] = []
    count = max(len(current), len(baseline))
    for index in range(count):
        left = current[index] if index < len(current) else None
        right = baseline[index] if index < len(baseline) else None
        if _semantic(left, _SPELL_COSMETIC) != _semantic(right, _SPELL_COSMETIC):
            result.append(index + 1)
    return tuple(result)


def load_game_resources(
    app_dir: Path | None = None,
    *,
    objects_parser: Callable[[Path], object] | None = None,
) -> GameResources:
    base = Path(app_dir) if app_dir is not None else application_dir()
    base = base.resolve()
    paths = {name: base / rel for name, rel in REQUIRED_RELATIVE_PATHS.items()}
    missing = [str(REQUIRED_RELATIVE_PATHS[name]) for name, path in paths.items() if not path.is_file()]
    if missing:
        raise GameDataError(
            "Не удаётся загрузить данные игры. Не найдены обязательные файлы:\n"
            + "\n".join(f"• {value}" for value in missing)
            + "\n\nПоместите приложение в корневую папку Discord Times и запускайте его оттуда."
        )

    try:
        units = _index_sections(paths["units"], kind="юнитов")
        artifacts = _index_sections(paths["artifacts"], kind="артефактов")
        spells = _spell_sections(paths["spells"])
        global_values = _global_values(paths["global"])
        if objects_parser is None:
            import dtm_sav_quest_sync as sync
            objects_parser = sync.parse_objects_ugs
        objects_parser(paths["objects"])
    except GameDataError:
        raise
    except Exception as exc:
        raise GameDataError(
            "Не удаётся загрузить данные игры. Один из обязательных файлов повреждён или имеет неподдерживаемый формат.\n\n"
            "Приложение нужно запускать из корневой папки Discord Times.\n"
            f"Причина: {exc}"
        ) from exc

    baseline_path = Path(__file__).resolve().with_name("army_catalog.json")
    baseline = army_runtime.load_catalog(baseline_path)
    changed_units = _changed_dict_ids(units, baseline.get("units", {}), _UNIT_COSMETIC)
    changed_artifacts = _changed_dict_ids(artifacts, baseline.get("artifacts", {}), _ARTIFACT_COSMETIC)
    changed_spells = _changed_spell_ids(spells, baseline.get("spells", []))

    try:
        recruit_div = float(global_values.get("CostRecruitDiv", 2) or 2)
    except (TypeError, ValueError):
        recruit_div = 2.0
    global_changed = not math.isclose(recruit_div, 2.0, rel_tol=0.0, abs_tol=1e-12)

    catalog = {
        "units": units,
        "artifacts": artifacts,
        "spells": spells,
        "_global": global_values,
        "_native_profiles": copy.deepcopy(baseline.get("_native_profiles", {})),
        "_compat": {
            "changed_units": list(changed_units),
            "changed_artifacts": list(changed_artifacts),
            "changed_spells": list(changed_spells),
            "global_changed": bool(global_changed),
        },
    }
    hashes = {name: _sha256(path) for name, path in paths.items()}
    catalog["_game_data"] = {
        "app_dir": str(base),
        "files": {name: str(path) for name, path in paths.items()},
        "sha256": hashes,
        "untested": bool(changed_units or changed_artifacts or changed_spells or global_changed),
    }

    warnings: list[str] = []
    if changed_units:
        warnings.append(f"изменены параметры {len(changed_units)} юнитов")
    if changed_artifacts:
        warnings.append(f"изменены параметры {len(changed_artifacts)} артефактов")
    if changed_spells:
        warnings.append(f"изменены параметры {len(changed_spells)} заклинаний")
    if global_changed:
        warnings.append("CostRecruitDiv отличается от проверенного профиля")

    return GameResources(
        app_dir=base,
        units_path=paths["units"],
        artifacts_path=paths["artifacts"],
        spells_path=paths["spells"],
        global_path=paths["global"],
        objects_ugs=paths["objects"],
        catalog=catalog,
        warnings=tuple(warnings),
        changed_units=changed_units,
        changed_artifacts=changed_artifacts,
        changed_spells=changed_spells,
        global_changed=global_changed,
        hashes=hashes,
    )


def install_game_resources(resources: GameResources) -> None:
    army_runtime.set_active_catalog(resources.catalog)
