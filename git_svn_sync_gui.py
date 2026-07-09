#!/usr/bin/env python3
"""Tkinter GUI for git-svn-sync."""

import dataclasses
import queue
import threading
from dataclasses import dataclass
from typing import Iterable, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import git_svn_sync as sync


MANUAL_PRESET_LABEL = "Manual paths"
SVN_UPDATE_HINT = "Please run 'svn update'"


@dataclass
class GuiPlanRow:
    item: sync.PlanItem
    selected: bool = True
    use_alternate: bool = False

    @property
    def operation(self) -> Optional[sync.SyncOperation]:
        if self.use_alternate and self.item.alternate_operation:
            return self.item.alternate_operation
        return self.item.suggested_operation


def operation_label(operation: Optional[sync.SyncOperation]) -> str:
    if operation is None:
        return "skip"
    if operation.action == "copy":
        return f"copy to {operation.destination.upper()}"
    return f"delete from {operation.destination.upper()}"


def operation_author(operation: Optional[sync.SyncOperation]) -> str:
    if operation is None:
        return ""
    marker = "\nOriginal author: "
    if marker in operation.message:
        return operation.message.rsplit(marker, 1)[1].strip()
    return ""


def operation_summary(operation: Optional[sync.SyncOperation]) -> str:
    if operation is None or not operation.message:
        return ""
    return operation.message.splitlines()[0]


def row_values(row: GuiPlanRow) -> tuple:
    operation = row.operation
    status = {
        "diff": "Diff",
        "only_git": "Only in Git",
        "only_svn": "Only in SVN",
    }.get(row.item.kind, row.item.kind)
    return (
        "yes" if row.selected else "no",
        status,
        operation_label(operation),
        row.item.relpath,
        operation_author(operation),
        operation_summary(operation),
    )


def selected_operations(rows: Iterable[GuiPlanRow]) -> List[sync.SyncOperation]:
    operations: List[sync.SyncOperation] = []
    for row in rows:
        if row.selected and row.operation:
            operations.append(row.operation)
    return operations


def is_svn_update_required_error(message: str) -> bool:
    return SVN_UPDATE_HINT in message


class TkQueueReporter(sync.CommandReporter):
    def __init__(self, events: "queue.Queue[tuple]"):
        self.events = events

    def message(self, text: str, stream: str = "stdout") -> None:
        self.events.put(("message", stream, text))

    def command(self, event: sync.CommandEvent) -> None:
        self.events.put(("command", event))

    def file_operation(self, event: sync.FileOperationEvent) -> None:
        self.events.put(("file_operation", event))


class GitSvnSyncApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("git-svn-sync")
        self.root.geometry("1180x760")

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.reporter = TkQueueReporter(self.events)
        self.current_plan: Optional[sync.SyncPlan] = None
        self.rows: List[GuiPlanRow] = []
        self.busy = False

        self.preset_var = tk.StringVar(value=MANUAL_PRESET_LABEL)
        self.git_var = tk.StringVar()
        self.svn_var = tk.StringVar()
        self.dry_run_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Ready")

        self._build_ui()
        self._poll_events()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.root.rowconfigure(2, weight=1)

        controls = ttk.Frame(self.root, padding=8)
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Preset").grid(row=0, column=0, sticky="w")
        preset_values = [MANUAL_PRESET_LABEL] + list(sync.PRESETS.keys())
        preset = ttk.Combobox(
            controls,
            textvariable=self.preset_var,
            values=preset_values,
            state="readonly",
            width=18,
        )
        preset.grid(row=0, column=1, sticky="ew", padx=(6, 12))
        preset.bind("<<ComboboxSelected>>", self._on_preset_changed)

        ttk.Checkbutton(
            controls,
            text="Dry run",
            variable=self.dry_run_var,
        ).grid(row=0, column=2, sticky="w", padx=(0, 12))

        ttk.Button(controls, text="Scan", command=self.scan).grid(row=0, column=3, padx=2)
        ttk.Button(controls, text="Run selected", command=self.run_selected).grid(row=0, column=4, padx=2)
        ttk.Button(controls, text="Rebaseline", command=self.rebaseline).grid(row=0, column=5, padx=2)
        ttk.Button(controls, text="Update SVN", command=self.update_svn).grid(row=0, column=6, padx=2)

        ttk.Label(controls, text="Git").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.git_entry = ttk.Entry(controls, textvariable=self.git_var)
        self.git_entry.grid(row=1, column=1, columnspan=5, sticky="ew", padx=(6, 6), pady=(8, 0))
        ttk.Button(controls, text="Browse", command=lambda: self._browse(self.git_var)).grid(row=1, column=6, sticky="w", pady=(8, 0))

        ttk.Label(controls, text="SVN").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.svn_entry = ttk.Entry(controls, textvariable=self.svn_var)
        self.svn_entry.grid(row=2, column=1, columnspan=5, sticky="ew", padx=(6, 6), pady=(6, 0))
        ttk.Button(controls, text="Browse", command=lambda: self._browse(self.svn_var)).grid(row=2, column=6, sticky="w", pady=(6, 0))

        table_frame = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        columns = ("selected", "status", "action", "path", "author", "message")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        widths = {
            "selected": 74,
            "status": 110,
            "action": 120,
            "path": 360,
            "author": 140,
            "message": 360,
        }
        for column in columns:
            self.tree.heading(column, text=column.title())
            self.tree.column(column, width=widths[column], anchor="w", stretch=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.bind("<Double-1>", self._toggle_selected)
        self.tree.bind("<space>", self._toggle_selected)

        table_buttons = ttk.Frame(table_frame)
        table_buttons.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(table_buttons, text="Toggle selected", command=self._toggle_selected).pack(side="left")
        ttk.Button(table_buttons, text="Toggle add/remove", command=self._toggle_action).pack(side="left", padx=(6, 0))
        ttk.Label(table_buttons, textvariable=self.status_var).pack(side="right")

        log_frame = ttk.Frame(self.root, padding=(8, 4, 8, 8))
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log = tk.Text(log_frame, height=14, wrap="none", font=("TkFixedFont", 10))
        self.log.grid(row=0, column=0, sticky="nsew")
        log_y = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_y.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_y.set)

        log_buttons = ttk.Frame(log_frame)
        log_buttons.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(log_buttons, text="Clear log", command=self._clear_log).pack(side="left")
        ttk.Button(log_buttons, text="Copy log", command=self._copy_log).pack(side="left", padx=(6, 0))

    def _config(self, rebaseline: bool = False) -> sync.SyncConfig:
        preset = self.preset_var.get()
        preset_name = None if preset == MANUAL_PRESET_LABEL else preset
        return sync.SyncConfig(
            git_root=None if preset_name else self.git_var.get().strip(),
            svn_root=None if preset_name else self.svn_var.get().strip(),
            preset_name=preset_name,
            dry_run=self.dry_run_var.get(),
            rebaseline=rebaseline,
            auto_yes=True,
        )

    def _browse(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory()
        if path:
            var.set(path)

    def _on_preset_changed(self, _event=None) -> None:
        preset_name = self.preset_var.get()
        if preset_name == MANUAL_PRESET_LABEL:
            self.git_entry.configure(state="normal")
            self.svn_entry.configure(state="normal")
            return
        preset = sync.PRESETS[preset_name]
        self.git_var.set(preset.git_root)
        self.svn_var.set(preset.svn_root)
        self.git_entry.configure(state="disabled")
        self.svn_entry.configure(state="disabled")

    def scan(self) -> None:
        if self._start_worker("Scanning..."):
            config = self._config(rebaseline=False)
            self._run_worker(lambda: self.events.put(("plan", sync.prepare_sync_plan(config, self.reporter))))

    def rebaseline(self) -> None:
        if self._start_worker("Rebaselining..."):
            config = self._config(rebaseline=True)

            def work() -> None:
                plan = sync.prepare_sync_plan(config, self.reporter)
                added = sync.apply_rebaseline_plan(plan, self.reporter)
                self.events.put(("rebaseline_done", plan, added))

            self._run_worker(work)

    def update_svn(self) -> None:
        if self._start_worker("Updating SVN..."):
            config = self._config(rebaseline=False)

            def work() -> None:
                sync.safe_svn_update_from_config(config, self.reporter)
                self.events.put(("svn_update_done",))

            self._run_worker(work)

    def run_selected(self) -> None:
        if not self.current_plan:
            messagebox.showinfo("git-svn-sync", "Scan before running selected operations.")
            return
        operations = selected_operations(self.rows)
        if not operations:
            messagebox.showinfo("git-svn-sync", "No selected operations to run.")
            return
        if self._start_worker("Running selected operations..."):
            plan = dataclasses.replace(
                self.current_plan,
                config=dataclasses.replace(
                    self.current_plan.config,
                    dry_run=self.dry_run_var.get(),
                ),
            )

            def work() -> None:
                sync.execute_plan_operations(plan, operations, self.reporter)
                self.events.put(("run_done",))

            self._run_worker(work)

    def _start_worker(self, status: str) -> bool:
        if self.busy:
            return False
        self.busy = True
        self.status_var.set(status)
        return True

    def _run_worker(self, target) -> None:
        def wrapped() -> None:
            try:
                target()
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=wrapped, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "message":
            _kind, stream, text = event
            self._append_log(text + "\n")
        elif kind == "command":
            self._append_command(event[1])
        elif kind == "file_operation":
            self._append_file_operation(event[1])
        elif kind == "plan":
            self.current_plan = event[1]
            self._load_plan(self.current_plan)
            self.busy = False
            self.status_var.set(
                f"Scan complete: {len(self.rows)} item(s), dry run {'on' if self.current_plan.config.dry_run else 'off'}"
            )
        elif kind == "rebaseline_done":
            _kind, plan, added = event
            self.current_plan = plan
            self.rows = []
            self._reload_tree()
            self.busy = False
            self.status_var.set(f"Rebaseline complete: {len(added)} path(s)")
        elif kind == "run_done":
            self.busy = False
            self.status_var.set("Run complete")
        elif kind == "svn_update_done":
            self.busy = False
            self.status_var.set("SVN update complete; scan again when ready")
        elif kind == "error":
            self.busy = False
            self.status_var.set("Error")
            message = event[1]
            self._append_log(f"ERROR: {message}\n")
            if is_svn_update_required_error(message):
                self._show_svn_update_error(message)
            else:
                messagebox.showerror("git-svn-sync", message)

    def _show_svn_update_error(self, message: str) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("SVN Update Required")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.columnconfigure(0, weight=1)

        body = ttk.Frame(dialog, padding=12)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)

        ttk.Label(
            body,
            text=message,
            wraplength=560,
            justify="left",
        ).grid(row=0, column=0, sticky="ew")
        ttk.Label(
            body,
            text=(
                "Update SVN will first run svn status. If local changes are "
                "found, it will refuse to run svn update."
            ),
            wraplength=560,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(10, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=2, column=0, sticky="e", pady=(14, 0))

        def run_update() -> None:
            dialog.destroy()
            self.update_svn()

        ttk.Button(buttons, text="Update SVN", command=run_update).pack(side="right")
        ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side="right", padx=(0, 8))

        dialog.grab_set()
        dialog.wait_visibility()
        dialog.focus_set()

    def _load_plan(self, plan: sync.SyncPlan) -> None:
        self.rows = [
            GuiPlanRow(item, selected=item.suggested_operation is not None)
            for item in plan.items
        ]
        self._reload_tree()

    def _reload_tree(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        for index, row in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(index), values=row_values(row))

    def _toggle_selected(self, _event=None) -> None:
        for item_id in self.tree.selection():
            row = self.rows[int(item_id)]
            row.selected = not row.selected
            self.tree.item(item_id, values=row_values(row))

    def _toggle_action(self) -> None:
        for item_id in self.tree.selection():
            row = self.rows[int(item_id)]
            if row.item.alternate_operation:
                row.use_alternate = not row.use_alternate
                self.tree.item(item_id, values=row_values(row))

    def _append_log(self, text: str) -> None:
        self.log.insert("end", text)
        self.log.see("end")

    def _append_command(self, event: sync.CommandEvent) -> None:
        prefix = "[dry-run] " if event.dry_run else ""
        if event.planned:
            prefix += "planned "
        cwd = f" (cwd: {event.cwd})" if event.cwd else ""
        self._append_log(f"{prefix}$ {sync.format_command(event.cmd)}{cwd}\n")
        if event.stdout:
            self._append_log(event.stdout.rstrip() + "\n")
        if event.stderr:
            self._append_log(event.stderr.rstrip() + "\n")
        if event.returncode is not None:
            self._append_log(f"exit code: {event.returncode}\n")

    def _append_file_operation(self, event: sync.FileOperationEvent) -> None:
        prefix = "[dry-run] " if event.dry_run else ""
        if event.planned:
            prefix += "planned "
        if event.source and event.destination:
            self._append_log(f"{prefix}{event.action} {event.source} -> {event.destination}\n")
        else:
            self._append_log(f"{prefix}{event.action} {event.relpath}\n")

    def _clear_log(self) -> None:
        self.log.delete("1.0", "end")

    def _copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end-1c"))


def main() -> None:
    root = tk.Tk()
    GitSvnSyncApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
