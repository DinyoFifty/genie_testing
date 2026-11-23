"""Review local batches, test conditions, and measurement evidence."""

import argparse
from datetime import datetime
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from support.master_results import (
    CONTEXT_FIELDS, NUMBER_FIELDS, PROFILES, ROOT, attach_evidence, collect_results,
    details, load_json, overview_row, radio_profile, result_groups, save_context,
    save_details, select_profile, test_context, write_workbook,
)
from support.results_layout import FIELDS, TERMS


def recorded_local(value):
    if not value:
        return "Unknown"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%m/%d/%y %I:%M %p")
    except (ValueError, AttributeError):
        return "Unknown"


def visible_rows(rows, category, query=""):
    query = query.strip().casefold()
    return [row for row in rows
            if (category == "All trials" or category == row["group"]
                or category == "All real trials" and row["group"] != "Simulated")
            and (not query or query in " ".join(str(row.get(key) or "") for key in
                 ("name", "batch", "group", "environment", "obstruction", "notes")).casefold())]


class ResultsWindow:
    def __init__(self, window, data=ROOT / "data"):
        self.window, self.data = window, Path(data)
        self.rows, self.warnings, self.items = [], [], {}
        self.search_job = None
        window.title("Genie results")
        width, height = min(1180, window.winfo_screenwidth() - 80), min(740, window.winfo_screenheight() - 100)
        window.geometry(f"{width}x{height}")
        window.minsize(min(850, width), min(560, height))
        style = ttk.Style(window)
        style.configure("Results.Treeview", font=("Segoe UI", 10), rowheight=29)
        style.configure("Results.Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("Results.Title.TLabel", font=("Segoe UI", 17, "bold"))
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Results", style="Results.Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Expand a batch to review its trials. Captures and notes stay on this laptop.").pack(anchor="w", pady=(3, 12))
        settings = ttk.LabelFrame(frame, text="Before your next capture", padding=10)
        settings.pack(fill="x", pady=(0, 12))
        ttk.Label(settings, text="Analyzer profile").grid(row=0, column=0, sticky="w")
        self.profile_keys = list(PROFILES)
        self.profile_names = [PROFILES[key]["label"] for key in self.profile_keys]
        self.profile = tk.StringVar(value=PROFILES[radio_profile(self.data)]["label"])
        profile = ttk.Combobox(settings, state="readonly", textvariable=self.profile, values=self.profile_names, width=27)
        profile.grid(row=0, column=1, sticky="w", padx=(8, 16))
        profile.bind("<<ComboboxSelected>>", self.change_profile)
        ttk.Button(settings, text="Next test setup", command=lambda: self.edit(True)).grid(row=0, column=2, sticky="e")
        settings.columnconfigure(2, weight=1)
        ttk.Label(settings, text="Applies on the next connection. All profiles use 72 channels; normal trials keep your offsets fixed.",
                  wraplength=760).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        filters = ttk.Frame(frame)
        filters.pack(fill="x", pady=(0, 8))
        ttk.Label(filters, text="Show").pack(side="left")
        self.category = tk.StringVar(value="All real trials")
        category = ttk.Combobox(filters, textvariable=self.category, state="readonly", width=19,
                               values=("All real trials", "Normal trials", "Calibration", "Simulated", "All trials"))
        category.pack(side="left", padx=(6, 18))
        category.bind("<<ComboboxSelected>>", lambda _event: self.render())
        ttk.Label(filters, text="Find batch or trial").pack(side="left")
        self.query = tk.StringVar()
        ttk.Entry(filters, textvariable=self.query, width=24).pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.query.trace_add("write", self.search_changed)
        area = ttk.Frame(frame)
        area.pack(fill="both", expand=True)
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(area, columns=("type", "recorded", "distance", "n", "error", "state"),
                                 show="tree headings", selectmode="browse", height=5, style="Results.Treeview")
        for key, title, column_width in (("#0", "Batch / trial", 360), ("type", "Type", 110),
                                  ("recorded", "Recorded (local)", 165), ("distance", "Actual (m)", 85),
                                  ("n", "Readings", 80), ("error", "Avg. error (m)", 110), ("state", "Recording", 145)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=column_width, minwidth=column_width, stretch=key == "#0",
                             anchor="e" if key in ("distance", "n", "error") else "w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(area, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(area, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.tag_configure("batch", background="#EAF1F7", font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("simulated", foreground="#6B5876")
        self.tree.bind("<<TreeviewSelect>>", self.selection_changed)
        selected = ttk.LabelFrame(frame, text="Selected capture", padding=(10, 7))
        selected.pack(fill="x", pady=(10, 0))
        self.selection_note = tk.StringVar(value="Select a trial inside a batch to add notes or import evidence.")
        self.selection_label = ttk.Label(selected, textvariable=self.selection_note, wraplength=950)
        self.selection_label.pack(anchor="w", fill="x")
        selected.bind("<Configure>", lambda event: self.selection_label.configure(wraplength=max(300, event.width - 30)))
        self.status = tk.StringVar()
        self.status_label = ttk.Label(frame, textvariable=self.status, wraplength=1000)
        self.status_label.pack(anchor="w", fill="x", pady=(8, 6))
        frame.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(300, event.width - 32)))
        primary = ttk.Frame(frame)
        primary.pack(fill="x")
        for label, action in (("Refresh", self.refresh), ("Open workbook", self.open_workbook), ("Label guide", self.label_guide)):
            ttk.Button(primary, text=label, command=action).pack(side="left", padx=(0, 8))
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(8, 0))
        self.trial_buttons = []
        for label, action in (("Trial details", self.edit), ("Import attempts", lambda: self.evidence("attempts")),
                              ("Import reflector power", lambda: self.evidence("power"))):
            button = ttk.Button(actions, text=label, command=action, state="disabled")
            button.pack(side="left", padx=(0, 8))
            self.trial_buttons.append(button)
        self.refresh()

    def change_profile(self, _event=None):
        try:
            key = self.profile_keys[self.profile_names.index(self.profile.get())]
            select_profile(key, self.data)
            self.status.set(f"Next connection: {self.profile.get()}. Current recordings keep their settings.")
        except (OSError, ValueError) as error:
            messagebox.showerror("Analyzer profile", str(error), parent=self.window)

    def search_changed(self, *_args):
        if self.search_job:
            self.window.after_cancel(self.search_job)
        self.search_job = self.window.after(150, self.render)

    def render(self):
        self.search_job = None
        opened = {key for key in self.tree.get_children() if self.tree.item(key, "open")}
        chosen = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        rows = visible_rows(self.rows, self.category.get(), self.query.get())
        # Filters reuse cached rows; they never reread captures or rebuild Excel.
        visible = {row["trial"] for row in rows}
        for group in result_groups(self.rows):
            matches = [row for row in group["rows"] if row["trial"] in visible]
            if not matches:
                continue
            summary = overview_row({**group, "rows": matches})
            parent = "batch_" + group["id"]
            last = max((row.get("recorded") or "" for row in matches), default="")
            distance_count = len({row["actual_m"] for row in matches if row["actual_m"] is not None})
            coverage = summary["condition_count"]
            state = f"{coverage}/40 conditions" if coverage is not None else f"{summary['complete_count']}/{len(matches)} complete"
            error = summary["filtered_mae_m"]
            self.tree.insert("", "end", iid=parent, text=f"{group['name']} - Setup {group['set']}",
                open=parent in opened, tags=("batch",), values=(group["group"], recorded_local(last),
                f"{distance_count} distance{'s' if distance_count != 1 else ''}", summary["filtered_n"], f"{error:.3f}" if error is not None else "", state))
            self.items[parent] = {**group, "rows": matches}
            for row in matches:
                key = "trial_" + row["trial"]
                self.items[key] = row
                error = row["filtered_mae_m"]
                self.tree.insert(parent, "end", iid=key, text=row["name"], tags=("simulated",) if row["group"] == "Simulated" else (),
                    values=(row["group"], recorded_local(row["recorded"]),
                    f"{row['actual_m']:g}" if row["actual_m"] is not None else "Unknown", row["filtered_n"],
                    f"{error:.3f}" if error is not None else "", row["recording_status"]))
        if chosen and self.tree.exists(chosen[0]):
            self.tree.selection_set(chosen[0])
        self.selection_changed()
        simulated = sum(row["group"] == "Simulated" for row in self.rows)
        note = f"Showing {len(rows)} of {len(self.rows)} captures."
        if self.category.get() == "All real trials" and simulated:
            note += f" {simulated} simulated captures hidden; choose Simulated or All trials to view them."
        if not rows:
            note += " No matching captures."
        if self.query.get().strip():
            note += " Group totals reflect the matching trials."
        self.status.set(note)

    def refresh(self, dialog_errors=False):
        try:
            self.window.configure(cursor="watch")
            self.window.update_idletasks()
            self.rows, self.warnings = collect_results(self.data)
            self.render()
            output = write_workbook(self.data, self.rows, self.warnings)
            suffix = f" {len(self.warnings)} skipped capture(s); see Definitions." if self.warnings else " Workbook updated."
            self.status.set(self.status.get() + suffix)
            return output
        except (OSError, ValueError) as error:
            self.status.set(f"List available; workbook could not update. Close Excel and refresh. {error}")
            if dialog_errors:
                messagebox.showerror("Workbook update", self.status.get(), parent=self.window)
            return None
        finally:
            self.window.configure(cursor="")

    def selected(self):
        chosen = self.tree.selection()
        row = self.items.get(chosen[0]) if chosen else None
        return row if row and "trial" in row else None

    def selection_changed(self, _event=None):
        row = self.selected()
        for button in self.trial_buttons:
            button.configure(state="normal" if row else "disabled")
        if row:
            self.selection_note.set(
                f"{row['name']} | Setup {row['set']} | {row['stop_label']} | Unusable result messages: {row['unusable_results'] if row['unusable_results'] is not None else 'unknown'}\n"
                f"Attempt log: {row['attempt_status'].lower()}   Timing: {row['latency_status'].lower()}   "
                f"Power: {row['power_status'].lower()}   TX readback: {row['tx_status'].lower()}")
        else:
            chosen = self.tree.selection()
            group = self.items.get(chosen[0]) if chosen else None
            if group:
                summary = overview_row(group)
                self.selection_note.set(f"{summary['complete_count']} complete captures. Missing attempt logs: {summary['missing_attempts']}; "
                    f"missing timing: {summary['missing_latency']}; missing current: {summary['missing_power']}.\n"
                    "Expand this group and select a trial to add notes or import evidence.")
            else:
                self.selection_note.set("Select a trial inside a batch to add notes or import evidence. Blank metrics mean unknown.")

    def edit(self, defaults=False):
        row = None if defaults else self.selected()
        if not defaults and row is None:
            return
        folder = self.data / "live" / row["trial"] if row else None
        try:
            saved = test_context(self.data) if defaults else {
                **(load_json(folder / "capture/summary.json").get("conditions") or {}), **details(folder)}
        except (OSError, ValueError) as error:
            messagebox.showerror("Trial details", str(error), parent=self.window)
            return
        dialog = tk.Toplevel(self.window)
        dialog.transient(self.window)
        dialog.title("Next test setup" if defaults else f"Trial details - {row['name']}")
        dialog.geometry(f"{min(900, self.window.winfo_screenwidth() - 100)}x590")
        dialog.minsize(640, 520)
        container = ttk.Frame(dialog, padding=14)
        container.pack(fill="both", expand=True)
        note = "Defaults for future captures. Resumed guided batches keep their original setup." if defaults else "Changes stay with this capture. Measured evidence fields do not configure the boards."
        ttk.Label(container, text=note, wraplength=760).pack(anchor="w", pady=(0, 10))
        tabs = ttk.Notebook(container)
        tabs.pack(fill="both", expand=True)
        fields = {}
        suggestions = {"environment": ("Open LOS", "Garage LOS", "Outdoors LOS", "Obstructed"),
                       "obstruction": ("None", "Person", "Glass", "Garage door"),
                       "motion": ("Stationary", "Approaching", "Moving away"),
                       "power_source": ("USB", "Coin cell", "Bench supply")}
        sections = [("Conditions", CONTEXT_FIELDS if defaults else (*CONTEXT_FIELDS, "notes"))]
        if not defaults:
            sections.append(("Measured evidence", (*NUMBER_FIELDS, "tx_power_evidence", "latency_definition", "latency_evidence", "power_evidence")))
        for title, keys in sections:
            page = ttk.Frame(tabs, padding=12)
            tabs.add(page, text=title)
            page.columnconfigure(1, weight=1)
            for index, key in enumerate(keys):
                label, meaning = FIELDS[key]
                ttk.Label(page, text=label).grid(row=index * 2, column=0, sticky="w", padx=(0, 12), pady=(5, 0))
                entry = ttk.Combobox(page, values=suggestions[key]) if key in suggestions else ttk.Entry(page)
                entry.insert(0, str(saved.get(key) if saved.get(key) is not None else ""))
                entry.grid(row=index * 2, column=1, sticky="ew", pady=(5, 0))
                ttk.Label(page, text=meaning, foreground="#536576", wraplength=680).grid(
                    row=index * 2 + 1, column=0, columnspan=2, sticky="w", pady=(1, 3))
                fields[key] = entry
        def save():
            try:
                values = {key: entry.get() for key, entry in fields.items()}
                if defaults:
                    save_context(values, self.data)
                else:
                    save_details(folder, values)
                dialog.destroy()
                self.refresh()
            except (OSError, ValueError) as error:
                messagebox.showerror("Trial details", str(error), parent=dialog)
        footer = ttk.Frame(container)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Button(footer, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(footer, text="Save local details", command=save).pack(side="right", padx=(0, 8))

    def evidence(self, kind):
        row = self.selected()
        if row is None:
            return
        folder = self.data / "live" / row["trial"]
        schema = ("Required: attempt_id,status. Status: valid, failed, or unresolved.\n"
                  "Optional timing: request_time_s,result_time_s,clock_id.\n"
                  "Use unique measurement attempts, not diagnostic messages."
                  if kind == "attempts" else
                  "Required: time_s,current_ma. Optional: voltage_v.\n"
                  "Use reflector-only instrument samples with increasing time.")
        messagebox.showinfo("Evidence CSV format", schema, parent=self.window)
        source = filedialog.askopenfilename(title=f"Import {kind} evidence", filetypes=[("CSV", "*.csv")], parent=self.window)
        if not source:
            return
        note = simpledialog.askstring("Evidence source", "Instrument/log and the trial time window it covers:", parent=self.window)
        if note is None:
            return
        complete = False
        cycles = None
        if kind == "attempts":
            complete = messagebox.askyesno("Attempt coverage", "Does this log include EVERY measurement attempt in the trial window, including failures?\n\nChoose No if coverage is incomplete or unknown. Valid percentage will stay blank.", parent=self.window)
        else:
            cycles = simpledialog.askinteger("Cycle count", "Sounding cycles inside this instrument window?\nCancel if unknown; average current can still be calculated.", minvalue=1, parent=self.window)
        try:
            attach_evidence(folder, source, kind, note=note, coverage_complete=complete, cycles=cycles)
            self.refresh()
        except (OSError, ValueError) as error:
            messagebox.showerror("Evidence import", str(error), parent=self.window)

    def open_workbook(self):
        output = self.refresh(dialog_errors=True)
        if output is not None:
            try:
                os.startfile(output)
            except OSError as error:
                messagebox.showerror("Open workbook", str(error), parent=self.window)

    def label_guide(self):
        dialog = tk.Toplevel(self.window)
        dialog.transient(self.window)
        dialog.title("Results label guide")
        dialog.geometry("900x520")
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        table = ttk.Treeview(frame, columns=("term", "meaning"), show="headings", style="Results.Treeview")
        table.heading("term", text="Label or term")
        table.heading("meaning", text="Simple meaning")
        table.column("term", width=240, minwidth=200)
        table.column("meaning", width=700, minwidth=650)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=table.xview)
        table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        table.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        definitions = {title: meaning for title, meaning in FIELDS.values()}
        definitions.update(TERMS)
        for title, meaning in sorted(definitions.items(), key=lambda pair: pair[0].casefold()):
            table.insert("", "end", values=(title, meaning))


def show_results(data=ROOT / "data"):
    window = tk.Tk()
    ResultsWindow(window, data)
    window.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Rebuild without opening the review window")
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    if args.refresh:
        rows, warnings = collect_results(args.data)
        output = write_workbook(args.data, rows, warnings)
        print(f"{output}: {len(rows)} trials; {len(warnings)} warnings")
    else:
        show_results(args.data)

