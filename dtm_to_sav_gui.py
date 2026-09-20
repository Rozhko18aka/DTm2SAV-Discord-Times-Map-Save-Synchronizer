#!/usr/bin/env python3
"""Tkinter interface for conservative Discord Times DTm -> SAV updates."""

from __future__ import annotations

import json
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import dtm_sav_quest_sync as sync
import game_resources


class QuestSyncApp(tk.Tk):
    def __init__(self, resources: game_resources.GameResources) -> None:
        super().__init__()
        self.resources = resources
        self.title("Discord Times — обновление DTm → SAV")
        self.geometry("1180x760")
        self.minsize(900, 620)
        self.plan: sync.SyncPlan | None = None

        self.source_var = tk.StringVar()
        self.original_var = tk.StringVar()
        self.modified_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.summary_var = tk.StringVar(value="Выберите четыре файла и нажмите «Анализировать».")
        self.write_report_var = tk.BooleanVar(value=True)

        self._configure_style()
        self._build_ui()
        self.after(100, self._announce_game_data)

    def _announce_game_data(self) -> None:
        self._log(f"Игровые данные загружены из: {self.resources.app_dir}")
        self._log(
            f"Rus_Units.ini: {len(self.resources.catalog.get('units', {}))} юнитов; "
            f"Rus_Artefacts.ini: {len(self.resources.catalog.get('artifacts', {}))} артефактов; "
            f"Rus_Spells.ini: {len(self.resources.catalog.get('spells', []))} заклинаний."
        )
        if self.resources.has_untested_data:
            warning = self.resources.warning_text()
            self._log("ПРЕДУПРЕЖДЕНИЕ: " + warning.replace("\n", " "))
            messagebox.showwarning("Изменённые игровые данные", warning, parent=self)
        else:
            self._log("Игровые параметры совпадают с проверенным встроенным профилем.")

    def _profile_warning_suffix(self) -> str:
        if not self.resources.has_untested_data:
            return ""
        return (
            "\n\nВНИМАНИЕ: параметры игры отличаются от профиля, на котором проводились native-тесты. "
            "Будут использованы текущие файлы игры; сохранение разрешено, но изменённые параметры не тестировались."
        )

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Hint.TLabel", foreground="#555555")
        style.configure("Summary.TLabel", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)

        ttk.Label(root, text="Обновление карты в существующем сохранении", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            root,
            text=(
                "Пройденные или уже затронутые события сохраняются. Из изменённой DTm переносятся "
                "изменённые и новые будущие события, изменения армий, ландшафт, строения, Фонарь/Events и декорации. "
                "Игровые Rus_Units.ini, Rus_Artefacts.ini, Rus_Spells.ini, _Global.ini и "
                "Graphics\\Objects\\Objects.ugs загружаются при запуске рядом с приложением. "
                "Если параметры отличаются от проверенного профиля, приложение предупреждает об отсутствии тестирования, "
                "но использует текущие игровые данные и разрешает сохранение."
            ),
            style="Hint.TLabel",
            wraplength=1120,
            justify="left",
        ).pack(anchor="w", pady=(4, 12))

        files = ttk.LabelFrame(root, text="Файлы", padding=10)
        files.pack(fill="x")
        self._file_row(
            files,
            0,
            "Исходное сохранение SAV",
            self.source_var,
            self._browse_source,
            "Сохранение с прогрессом, которое нельзя перезаписывать",
        )
        self._file_row(
            files,
            1,
            "Оригинальная карта DTm",
            self.original_var,
            lambda: self._browse_map(self.original_var, "Выберите оригинальную DTm"),
            "Неизменённая карта, на которой было создано сохранение",
        )
        self._file_row(
            files,
            2,
            "Изменённая карта DTm",
            self.modified_var,
            lambda: self._browse_map(self.modified_var, "Выберите изменённую DTm"),
            "Карта с будущими квестами и изменёнными объектами",
        )
        self._file_row(
            files,
            3,
            "Новое сохранение SAV",
            self.output_var,
            self._browse_output,
            "Всегда новый файл; исходное сохранение приложение не перезаписывает",
        )
        files.columnconfigure(1, weight=1)

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=10)
        ttk.Button(actions, text="1. Анализировать", command=self.analyze).pack(side="left")
        self.convert_button = ttk.Button(
            actions, text="2. Создать новый SAV", command=self.convert, state="disabled"
        )
        self.convert_button.pack(side="left", padx=(8, 0))
        ttk.Checkbutton(
            actions,
            text="Сохранить JSON-отчёт рядом с новым SAV",
            variable=self.write_report_var,
        ).pack(side="left", padx=16)
        ttk.Button(actions, text="Очистить", command=self.clear).pack(side="right")

        ttk.Label(root, textvariable=self.summary_var, style="Summary.TLabel", wraplength=1120).pack(
            fill="x", pady=(0, 8)
        )

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)

        events_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(events_tab, text="События")
        columns = ("id", "state", "modified", "action", "changes", "old_title", "new_title")
        self.tree = self._create_report_tree(
            events_tab,
            columns,
            {
                "id": "ID",
                "state": "Состояние в SAV",
                "modified": "Изменено в DTm",
                "action": "Действие",
                "changes": "Что изменено",
                "old_title": "Старое название",
                "new_title": "Новое название",
            },
            {
                "id": 55,
                "state": 150,
                "modified": 115,
                "action": 150,
                "changes": 300,
                "old_title": 250,
                "new_title": 250,
            },
            stretch={"changes", "old_title", "new_title"},
        )
        self.tree.tag_configure("selected", background="#e2f4e5")
        self.tree.tag_configure("progressed", background="#f7e5e5")
        self.tree.tag_configure("unchanged", foreground="#777777")
        self.tree.bind("<<TreeviewSelect>>", self._show_selected_event_details)

        details_frame = ttk.LabelFrame(events_tab, text="Подробности выбранного события", padding=6)
        details_frame.pack(fill="x", pady=(8, 0))
        self.event_details = tk.Text(
            details_frame,
            height=6,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
        )
        details_scroll = ttk.Scrollbar(details_frame, orient="vertical", command=self.event_details.yview)
        self.event_details.configure(yscrollcommand=details_scroll.set)
        self.event_details.grid(row=0, column=0, sticky="nsew")
        details_scroll.grid(row=0, column=1, sticky="ns")
        details_frame.columnconfigure(0, weight=1)

        buildings_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(buildings_tab, text="Строения")
        self.building_tree = self._create_report_tree(
            buildings_tab,
            ("id", "action", "type", "coords", "picture", "runtime"),
            {
                "id": "ID",
                "action": "Действие",
                "type": "Тип",
                "coords": "Координаты",
                "picture": "Изображение",
                "runtime": "Runtime-блок",
            },
            {"id": 70, "action": 140, "type": 100, "coords": 160, "picture": 180, "runtime": 260},
            stretch={"runtime"},
        )

        armies_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(armies_tab, text="Армии")
        self.army_tree = self._create_report_tree(
            armies_tab,
            ("id", "action", "old_coords", "new_coords", "changes", "old_title", "new_title"),
            {
                "id": "ID",
                "action": "Действие",
                "old_coords": "Было",
                "new_coords": "Стало",
                "changes": "Что изменено",
                "old_title": "Старое название",
                "new_title": "Новое название",
            },
            {
                "id": 60,
                "action": 180,
                "old_coords": 120,
                "new_coords": 120,
                "changes": 420,
                "old_title": 220,
                "new_title": 220,
            },
            stretch={"changes", "old_title", "new_title"},
        )

        lanterns_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(lanterns_tab, text="Фонарь/Events")
        self.lantern_tree = self._create_report_tree(
            lanterns_tab,
            ("id", "action", "old_coords", "new_coords", "changes"),
            {
                "id": "ID",
                "action": "Действие",
                "old_coords": "Было",
                "new_coords": "Стало",
                "changes": "Что изменено",
            },
            {"id": 70, "action": 140, "old_coords": 160, "new_coords": 160, "changes": 560},
            stretch={"changes"},
        )

        terrain_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(terrain_tab, text="Ландшафт")
        self.terrain_tree = self._create_report_tree(
            terrain_tab,
            ("x", "y", "old", "new"),
            {"x": "X", "y": "Y", "old": "Было", "new": "Стало"},
            {"x": 100, "y": 100, "old": 240, "new": 240},
        )

        decor_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(decor_tab, text="Декор")
        self.decor_tree = self._create_report_tree(
            decor_tab,
            ("object", "action", "old_coords", "new_coords", "kind", "passable", "footprint"),
            {
                "object": "Object ID",
                "action": "Действие",
                "old_coords": "Было",
                "new_coords": "Стало",
                "kind": "UGS тип",
                "passable": "Проходимый",
                "footprint": "Размер",
            },
            {
                "object": 100,
                "action": 140,
                "old_coords": 150,
                "new_coords": 150,
                "kind": 100,
                "passable": 120,
                "footprint": 120,
            },
        )

        self.status = tk.Text(root, height=5, wrap="word", state="disabled", font=("Consolas", 9))
        self.status.pack(fill="x", pady=(10, 0))
        self._log("Готово к выбору файлов.")

    def _create_report_tree(
        self,
        parent: ttk.Frame,
        columns: tuple[str, ...],
        headings: dict[str, str],
        widths: dict[str, int],
        *,
        stretch: set[str] | None = None,
    ) -> ttk.Treeview:
        """Create a scrollable report table used by notebook tabs."""
        stretch = stretch or set()
        table = ttk.Frame(parent)
        table.pack(fill="both", expand=True)
        tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(
                column,
                width=widths[column],
                minwidth=45,
                stretch=column in stretch,
            )
        yscroll = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(table, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        return tree

    def _file_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command,
        hint: str,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row * 2, column=0, sticky="w", padx=(0, 8), pady=(3, 0))
        ttk.Entry(parent, textvariable=variable).grid(row=row * 2, column=1, sticky="ew", pady=(3, 0))
        ttk.Button(parent, text="Обзор…", command=command).grid(row=row * 2, column=2, padx=(8, 0), pady=(3, 0))
        ttk.Label(parent, text=hint, style="Hint.TLabel").grid(row=row * 2 + 1, column=1, sticky="w", pady=(0, 3))

    @staticmethod
    def _app_dir() -> Path:
        """Return the directory containing the running script or packaged EXE."""
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent

    def _preferred_saves_dir(self) -> Path | None:
        candidate = self._app_dir() / "Saves"
        return candidate if candidate.is_dir() else None

    def _preferred_maps_dir(self) -> Path | None:
        # Primary layout requested for the game installation.  Maps_Rus is
        # also supported because some Discord Times installations use it.
        candidates = (
            self._app_dir() / "Maps" / "_Rus",
            self._app_dir() / "Maps_Rus",
        )
        return next((path for path in candidates if path.is_dir()), None)

    @staticmethod
    def _existing_parent(value: str) -> Path | None:
        if not value.strip():
            return None
        path = Path(value.strip()).expanduser()
        parent = path if path.is_dir() else path.parent
        return parent if parent.is_dir() else None

    def _browse_open(
        self,
        variable: tk.StringVar,
        title: str,
        filetypes,
        preferred_dir: Path | None = None,
    ) -> None:
        initial_dir = preferred_dir or self._existing_parent(variable.get())
        value = filedialog.askopenfilename(
            title=title,
            filetypes=filetypes,
            initialdir=str(initial_dir) if initial_dir else None,
        )
        if value:
            variable.set(value)
            self._invalidate_plan()

    def _browse_source(self) -> None:
        self._browse_open(
            self.source_var,
            "Выберите исходное сохранение",
            [("Discord Times save", "*.sav"), ("Все файлы", "*.*")],
            preferred_dir=self._preferred_saves_dir(),
        )
        if self.source_var.get() and not self.output_var.get():
            source = Path(self.source_var.get())
            self.output_var.set(str(source.with_name(source.stem + "_updated.sav")))

    def _browse_map(self, variable: tk.StringVar, title: str) -> None:
        self._browse_open(
            variable,
            title,
            [("Discord Times map", "*.DTm"), ("Все файлы", "*.*")],
            preferred_dir=self._preferred_maps_dir(),
        )

    def _browse_output(self) -> None:
        initial = self.output_var.get()
        initial_dir = self._preferred_saves_dir() or self._existing_parent(initial)
        value = filedialog.asksaveasfilename(
            title="Куда сохранить новое сохранение",
            defaultextension=".sav",
            initialfile=Path(initial).name if initial else "updated.sav",
            initialdir=str(initial_dir) if initial_dir else None,
            filetypes=[("Discord Times save", "*.sav"), ("Все файлы", "*.*")],
        )
        if value:
            self.output_var.set(value)

    def _paths(self) -> tuple[Path, Path, Path, Path]:
        values = [
            self.source_var.get().strip(),
            self.original_var.get().strip(),
            self.modified_var.get().strip(),
            self.output_var.get().strip(),
        ]
        if not all(values):
            raise sync.QuestSyncError("Заполните все четыре пути.")
        source, original, modified, output = map(Path, values)
        for path in (source, original, modified):
            if not path.is_file():
                raise sync.QuestSyncError(f"Файл не найден: {path}")
        return source, original, modified, output

    def _invalidate_plan(self) -> None:
        self.plan = None
        self.convert_button.configure(state="disabled")

    def _busy(self, active: bool) -> None:
        self.configure(cursor="watch" if active else "")
        self.update_idletasks()

    def _log(self, text: str) -> None:
        self.status.configure(state="normal")
        self.status.insert("end", text.rstrip() + "\n")
        self.status.see("end")
        self.status.configure(state="disabled")

    def _set_event_details(self, text: str) -> None:
        self.event_details.configure(state="normal")
        self.event_details.delete("1.0", "end")
        self.event_details.insert("1.0", text)
        self.event_details.configure(state="disabled")

    def _show_selected_event_details(self, _event=None) -> None:
        if self.plan is None:
            return
        selection = self.tree.selection()
        if not selection:
            self._set_event_details("")
            return
        try:
            event_id = int(selection[0].removeprefix("event-"))
        except ValueError:
            return
        item = next((value for value in self.plan.decisions if value.event_id == event_id), None)
        if item is None:
            return

        lines = [f"Событие ID {item.event_id}: {item.change_summary}"]
        if item.binary_change_ranges:
            lines.append("\nИзменения новой DTm относительно оригинальной DTm:")
            lines.extend(f"  • {value}" for value in item.binary_change_ranges)
        elif not item.text_changes:
            lines.append("\nНовая DTm: бинарная запись не изменена.")

        if item.text_changes:
            lines.append("\nИзменения текстов:")
            lines.extend(f"  • {value}" for value in item.text_changes)

        if item.save_change_ranges:
            lines.append("\nИзменения текущего SAV относительно оригинальной DTm (игровой прогресс):")
            lines.extend(f"  • {value}" for value in item.save_change_ranges)

        self._set_event_details("\n".join(lines))

    def _fill_table(self, plan: sync.SyncPlan) -> None:
        self.tree.delete(*self.tree.get_children())
        self._set_event_details("")
        for item in plan.decisions:
            changed = item.binary_changed_in_modified_map or item.text_changed_in_modified_map
            if item.progressed:
                state = "затронуто/пройдено"
                action = "сохранить прогресс"
                tag = "progressed"
            elif item.selected_for_import:
                state = "будущее"
                action = "импортировать"
                tag = "selected"
            else:
                state = "будущее"
                action = "без изменений"
                tag = "unchanged"
            self.tree.insert(
                "",
                "end",
                iid=f"event-{item.event_id}",
                values=(
                    item.event_id,
                    state,
                    "да" if changed else "нет",
                    action,
                    item.change_summary,
                    item.original_title,
                    item.modified_title,
                ),
                tags=(tag,),
            )

    @staticmethod
    def _coords_text(x: int | None, y: int | None) -> str:
        if x is None or y is None:
            return "—"
        return f"({x}, {y})"

    @staticmethod
    def _old_lantern_records(plan: sync.SyncPlan) -> dict[int, tuple[int, int, bytes]]:
        section = plan.original_sections["lanterns"]
        result: dict[int, tuple[int, int, bytes]] = {}
        for index in range(section["count"]):
            start = section["offset"] + index * sync.LANTERN_SIZE
            record = plan.original_inner[start:start + sync.LANTERN_SIZE]
            x = int.from_bytes(record[0:2], "little")
            y = int.from_bytes(record[2:4], "little")
            result[record[4]] = (x, y, record)
        return result

    def _fill_building_table(self, plan: sync.SyncPlan) -> None:
        self.building_tree.delete(*self.building_tree.get_children())
        for item in plan.building_additions:
            self.building_tree.insert(
                "",
                "end",
                values=(
                    item.index + 1,
                    "добавить",
                    item.building_type,
                    self._coords_text(item.save_x, item.save_y),
                    f"{item.picture_number}/{item.picture_variant}",
                    "добавляется" if item.runtime_state_added else "не требуется",
                ),
            )

    def _fill_army_table(self, plan: sync.SyncPlan) -> None:
        self.army_tree.delete(*self.army_tree.get_children())
        for item in plan.army_changes:
            if item.action == "add":
                action = "добавить"
            elif item.action == "delete":
                action = "удалить"
            elif item.binary_changed and item.text_changed:
                action = "изменить + текст"
            elif item.binary_changed:
                action = "изменить"
            elif item.text_changed:
                action = "импортировать текст"
            else:
                action = "без изменений"
            details = item.change_summary
            if item.binary_change_ranges:
                details += "; " + "; ".join(item.binary_change_ranges)
            if item.text_changes:
                details += "; " + "; ".join(item.text_changes)
            self.army_tree.insert(
                "",
                "end",
                values=(
                    item.army_id,
                    action,
                    self._coords_text(item.old_x, item.old_y),
                    self._coords_text(item.new_x, item.new_y),
                    details,
                    item.original_title,
                    item.modified_title,
                ),
            )

    def _fill_lantern_table(self, plan: sync.SyncPlan) -> None:
        self.lantern_tree.delete(*self.lantern_tree.get_children())
        old_by_id = self._old_lantern_records(plan)
        new_by_id = {item.lantern_id: item for item in plan.lantern_records}
        added_ids = {item.lantern_id for item in plan.lantern_additions}
        changed_ids = set(plan.lantern_changed_ids)
        removed_ids = set(plan.lantern_removed_ids)

        for lantern_id in sorted(added_ids | changed_ids | removed_ids):
            old = old_by_id.get(lantern_id)
            new = new_by_id.get(lantern_id)
            if lantern_id in added_ids:
                action = "добавить"
                details = "новая запись Фонарь/Events"
            elif lantern_id in removed_ids:
                action = "удалить"
                details = "запись удалена из новой DTm"
            else:
                action = "изменить"
                ranges = sync.describe_binary_changes(old[2], new.record) if old and new else ()
                details = "; ".join(ranges) if ranges else "изменена запись"

            self.lantern_tree.insert(
                "",
                "end",
                values=(
                    lantern_id,
                    action,
                    self._coords_text(old[0], old[1]) if old else "—",
                    self._coords_text(new.x, new.y) if new else "—",
                    details,
                ),
            )

    def _fill_terrain_table(self, plan: sync.SyncPlan) -> None:
        self.terrain_tree.delete(*self.terrain_tree.get_children())
        for item in plan.terrain_changes:
            self.terrain_tree.insert(
                "",
                "end",
                values=(item.x, item.y, item.old_tile, item.new_tile),
            )

    def _fill_decor_table(self, plan: sync.SyncPlan) -> None:
        self.decor_tree.delete(*self.decor_tree.get_children())
        removals_by_id: dict[int, list[sync.DecorationAddition]] = {}
        additions_by_id: dict[int, list[sync.DecorationAddition]] = {}
        for item in plan.decoration_removals:
            removals_by_id.setdefault(item.object_id, []).append(item)
        for item in plan.decoration_additions:
            additions_by_id.setdefault(item.object_id, []).append(item)

        rows: list[tuple[int, str, sync.DecorationAddition | None, sync.DecorationAddition | None]] = []
        for object_id in sorted(set(removals_by_id) | set(additions_by_id)):
            old_items = removals_by_id.get(object_id, [])
            new_items = additions_by_id.get(object_id, [])
            paired = min(len(old_items), len(new_items))
            for index in range(paired):
                rows.append((object_id, "переместить", old_items[index], new_items[index]))
            for item in old_items[paired:]:
                rows.append((object_id, "удалить", item, None))
            for item in new_items[paired:]:
                rows.append((object_id, "добавить", None, item))

        for object_id, action, old, new in rows:
            sample = new or old
            assert sample is not None
            footprint = sample.atlas.footprint
            self.decor_tree.insert(
                "",
                "end",
                values=(
                    object_id,
                    action,
                    self._coords_text(old.x, old.y) if old else "—",
                    self._coords_text(new.x, new.y) if new else "—",
                    sample.atlas.kind,
                    "да" if sample.atlas.passable else "нет",
                    f"{footprint[0]}×{footprint[1]}",
                ),
            )

    def _fill_reports(self, plan: sync.SyncPlan) -> None:
        self._fill_table(plan)
        self._fill_building_table(plan)
        self._fill_army_table(plan)
        self._fill_lantern_table(plan)
        self._fill_terrain_table(plan)
        self._fill_decor_table(plan)

        self.notebook.tab(0, text=f"События ({len(plan.decisions)})")
        self.notebook.tab(1, text=f"Строения ({len(self.building_tree.get_children())})")
        self.notebook.tab(2, text=f"Армии ({len(self.army_tree.get_children())})")
        self.notebook.tab(3, text=f"Фонарь/Events ({len(self.lantern_tree.get_children())})")
        self.notebook.tab(4, text=f"Ландшафт ({len(self.terrain_tree.get_children())})")
        self.notebook.tab(5, text=f"Декор ({len(self.decor_tree.get_children())})")

    def analyze(self) -> None:
        try:
            source, original, modified, _ = self._paths()
            self._busy(True)
            self._log("Анализ файлов…")
            plan = sync.analyze_paths(source, original, modified, self.resources.objects_ugs)
            self.plan = plan
            self._fill_reports(plan)
            changed_total = sum(
                item.binary_changed_in_modified_map or item.text_changed_in_modified_map
                for item in plan.decisions
            )
            self.summary_var.set(
                f"Событий: {plan.event_count} → {plan.modified_event_count}. "
                f"Уже затронуто сохранением: {len(plan.progressed)}. "
                f"Изменено в новой DTm: {changed_total}. Будет импортировано будущих: {len(plan.selected)}. "
                f"Клеток ландшафта: {len(plan.terrain_changes)}. "
                f"Изменено армий: {len(plan.army_changes)}. "
                f"Новых строений: {len(plan.building_additions)}. "
                f"Неизвестных native-footprint профилей: {len(plan.building_navigation_unseen_profile_ids)}. "
                f"Новых строений без runtime-core шаблона: {len(plan.building_runtime_template_missing_ids)}. "
                f"Новых Фонарь/Events: {len(plan.lantern_additions)}. "
                f"Изменено Фонарь/Events: {len(plan.lantern_changed_ids)}. "
                f"Удалено Фонарь/Events: {len(plan.lantern_removed_ids)}. "
                f"Добавлено/перемещено декораций: {len(plan.decoration_additions)}. "
                f"Удалено/перемещено: {len(plan.decoration_removals)}. "
                f"Изменений пройденных квестов пропущено: {len(plan.modified_progressed)}."
            )
            self.convert_button.configure(state="normal")
            hero = (
                plan.hero_replacement.decode("cp1251", "replace")
                if plan.hero_replacement is not None else "не обнаружено"
            )
            self._log(f"Анализ завершён. Подстановка #HERONAME: {hero}.")
            if plan.army_changes:
                text_count = sum(item.text_changed for item in plan.army_changes)
                binary_ids = [item.army_id for item in plan.army_changes if item.binary_changed]
                if text_count:
                    self._log(f"Изменены тексты армий: {text_count}.")
                if binary_ids:
                    self._log(
                        "Будут синхронизированы runtime-изменения армий. "
                        f"ID: {', '.join(map(str, binary_ids))}."
                    )
            if plan.building_additions:
                ids = ", ".join(str(item.index + 1) for item in plan.building_additions)
                self._log(f"Будут добавлены строения: {ids}.")
            if plan.building_navigation_unseen_profile_ids:
                ids = ", ".join(map(str, plan.building_navigation_unseen_profile_ids))
                self._log(
                    "ВНИМАНИЕ: для строений ID " + ids
                    + " exact navigation-footprint не наблюдался в исходном SAV; "
                      "будет использован прямоугольный fallback size_x×size_y."
                )
            if plan.building_runtime_template_missing_ids:
                ids = ", ".join(map(str, plan.building_runtime_template_missing_ids))
                self._log(
                    "ВНИМАНИЕ: для новых строений ID " + ids
                    + " нет runtime-core шаблона того же типа в исходном SAV; "
                      "opaque bytes не считаются byte-certified."
                )
            if plan.lantern_additions:
                ids = ", ".join(str(item.lantern_id) for item in plan.lantern_additions)
                self._log(f"Будут добавлены Фонарь/Events: {ids}.")
            if plan.lantern_changed_ids:
                self._log(f"Будут изменены Фонарь/Events ID: {', '.join(map(str, plan.lantern_changed_ids))}.")
            if plan.lantern_removed_ids:
                self._log(f"Будут удалены Фонарь/Events ID: {', '.join(map(str, plan.lantern_removed_ids))}.")
            if plan.decoration_additions:
                ids = ", ".join(str(item.object_id) for item in plan.decoration_additions)
                self._log(f"Будут добавлены или перемещены декорации Objects.ugs: {ids}.")
                self._log("Визуальный цвет декораций пока рассчитывается экспериментально.")
            if plan.decoration_removals:
                ids = ", ".join(str(item.object_id) for item in plan.decoration_removals)
                self._log(f"Будут удалены или перемещены старые декорации: {ids}.")
            if plan.terrain_changes:
                self._log(f"Будет изменено клеток ландшафта: {len(plan.terrain_changes)}.")
            self._log("Зелёные строки будут перенесены; красные останутся из исходного SAV.")
        except Exception as exc:
            self._invalidate_plan()
            self.summary_var.set("Анализ не выполнен.")
            self._log(f"ОШИБКА: {exc}")
            messagebox.showerror("Ошибка анализа", str(exc), parent=self)
        finally:
            self._busy(False)

    def convert(self) -> None:
        try:
            source, original, modified, output = self._paths()
            self._busy(True)
            plan = sync.analyze_paths(source, original, modified, self.resources.objects_ugs)
            if not messagebox.askyesno(
                "Создать сохранение?",
                (
                    f"Будет импортировано будущих событий: {len(plan.selected)}.\n"
                    f"Будет изменено клеток ландшафта: {len(plan.terrain_changes)}.\n"
                    f"Будет синхронизировано армий: {len(plan.army_changes)}.\n"
                    f"Будет добавлено строений: {len(plan.building_additions)}.\n"
                    f"Footprint fallback: {len(plan.building_navigation_unseen_profile_ids)} строений.\n"
                    f"Без runtime-core шаблона: {len(plan.building_runtime_template_missing_ids)} строений.\n"
                    f"Будет добавлено Фонарь/Events: {len(plan.lantern_additions)}.\n"
                    f"Будет изменено Фонарь/Events: {len(plan.lantern_changed_ids)}.\n"
                    f"Будет удалено Фонарь/Events: {len(plan.lantern_removed_ids)}.\n"
                    f"Будет добавлено/перемещено декораций: {len(plan.decoration_additions)}.\n"
                    f"Будет удалено/перемещено старых декораций: {len(plan.decoration_removals)}.\n"
                    f"Событий с сохранённым прогрессом: {len(plan.progressed)}.\n\n"
                    "Игровой прогресс существующих армий сохраняется консервативно; изменяются только поля, "
                    "которые всё ещё соответствуют исходной DTm, и необходимые структурные ссылки.\n"
                    "Остальной динамический прогресс SAV не изменяется.\n"
                    f"Новый файл:\n{output}"
                    + self._profile_warning_suffix()
                ),
                parent=self,
            ):
                return
            overwrite = False
            if output.exists():
                overwrite = messagebox.askyesno(
                    "Файл существует",
                    f"Перезаписать выходной файл?\n{output}",
                    parent=self,
                )
                if not overwrite:
                    return
            self._log("Создание нового SAV…")
            report = sync.convert_plan(
                plan,
                output,
                write_report=self.write_report_var.get(),
                allow_overwrite=overwrite,
            )
            self.plan = plan
            self._log(f"Готово: {output}")
            self._log(f"SHA-256: {report['output_sha256']}")
            if report.get("report_file"):
                self._log(f"Отчёт: {report['report_file']}")
            messagebox.showinfo(
                "Готово",
                (
                    "Новое сохранение создано и проверено.\n\n"
                    f"Перенесено будущих событий: {len(plan.selected)}\n"
                    f"Изменено клеток ландшафта: {len(plan.terrain_changes)}\n"
                    f"Синхронизировано армий: {len(plan.army_changes)}\n"
                    f"Добавлено строений: {len(plan.building_additions)}\n"
                    f"Добавлено Фонарь/Events: {len(plan.lantern_additions)}\n"
                    f"Изменено Фонарь/Events: {len(plan.lantern_changed_ids)}\n"
                    f"Удалено Фонарь/Events: {len(plan.lantern_removed_ids)}\n"
                    f"Добавлено/перемещено декораций: {len(plan.decoration_additions)}\n"
                    f"Удалено/перемещено старых декораций: {len(plan.decoration_removals)}\n"
                    f"Сохранено затронутых событий: {len(plan.progressed)}\n"
                    f"Файл: {output}"
                ),
                parent=self,
            )
        except Exception as exc:
            self._log(f"ОШИБКА: {exc}")
            messagebox.showerror("Ошибка создания SAV", str(exc), parent=self)
        finally:
            self._busy(False)

    def clear(self) -> None:
        for variable in (self.source_var, self.original_var, self.modified_var, self.output_var):
            variable.set("")
        self._invalidate_plan()
        for tree in (self.tree, self.building_tree, self.army_tree, self.lantern_tree, self.terrain_tree, self.decor_tree):
            tree.delete(*tree.get_children())
        self._set_event_details("")
        self.notebook.tab(0, text="События")
        self.notebook.tab(1, text="Строения")
        self.notebook.tab(2, text="Армии")
        self.notebook.tab(3, text="Фонарь/Events")
        self.notebook.tab(4, text="Ландшафт")
        self.notebook.tab(5, text="Декор")
        self.summary_var.set("Выберите четыре файла и нажмите «Анализировать».")
        self._log("Поля очищены.")


def _show_startup_error(message: str) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Не удалось загрузить данные игры", message, parent=root)
        root.destroy()
    except tk.TclError:
        print(message, file=sys.stderr)


def main() -> int:
    try:
        resources = game_resources.load_game_resources()
        game_resources.install_game_resources(resources)
    except (OSError, game_resources.GameDataError) as exc:
        message = str(exc)
        if "корневую папку Discord Times" not in message:
            message += "\n\nПриложение нужно запускать из корневой папки Discord Times."
        _show_startup_error(message)
        return 2
    try:
        app = QuestSyncApp(resources)
        app.mainloop()
    except tk.TclError as exc:
        print(f"Не удалось запустить интерфейс: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
