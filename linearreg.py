"""Select live runs and fit measured distance against tape distance."""

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
from statistics import linear_regression, median
import tkinter as tk
from tkinter import messagebox, ttk
from zipfile import BadZipFile

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from support.genie_live import PROJECT_DATA, finite, workbook_distance
from support.trial_storage import trial_folders


FIRMWARE_ROOT = Path(__file__).resolve().parent / "firmware"
OFFSET_HEADER = "config/sl_rail_util_cs_antenna_offset_config.h"
BOARD_PROJECTS = ("bt_cs_ncp", "bt_cs_soc_reflector_studio")
ANTENNA_PAIRS = ("ANT1 ↔ ANT1", "ANT2 ↔ ANT2")
USED_OFFSETS = ("Unconfirmed", "Project offsets shown", "Zero on both boards")
READ_ERRORS = (OSError, ValueError, TypeError, KeyError, csv.Error, BadZipFile, InvalidFileException)


@dataclass
class Run:
    folder: Path
    actual_m: float | None
    yaw: tuple
    issue: str = ""
    calibration: dict | None = None
    firmware: dict | None = None
    simulated: bool = False


@dataclass
class DistanceFit:
    slope: float
    intercept: float


@dataclass
class RunGroup:
    folder: Path
    runs: list[Run]
    guided: bool = False


def group_runs(runs):
    """Show each guided batch once while keeping single trials selectable."""
    groups = {}
    for run in runs:
        batch = next((parent for parent in run.folder.parents
                      if parent.parent.name == "guided"), None)
        folder = batch or run.folder
        if folder not in groups:
            groups[folder] = RunGroup(folder, [], batch is not None)
        groups[folder].runs.append(run)
    return sorted(groups.values(), key=lambda group: (not group.guided, group.folder.name.casefold()))


def group_values(group):
    distances = sorted({run.actual_m for run in group.runs if run.actual_m is not None})
    configurations = {capture_configuration(run) for run in group.runs}
    configuration = next(iter(configurations)) if len(configurations) == 1 else "Mixed settings"
    ready = sum(not run.issue for run in group.runs)
    status = f"{ready} ready" if ready == len(group.runs) else f"{ready} ready; {len(group.runs) - ready} skipped"
    try:
        calibration_baseline(group.runs)
    except ValueError:
        status = "Settings conflict"
    if all(run.simulated for run in group.runs):
        status = "SIMULATED - " + status
    return (group.folder.name, len(group.runs), ", ".join(f"{value:g}" for value in distances),
            configuration, status)


def find_runs(live_dir):
    if not live_dir.is_dir():
        return []
    runs = []
    for folder in sorted(trial_folders(live_dir), reverse=True):
        summary_path = folder / "capture" / "summary.json"
        if not summary_path.is_file():
            summary_path = folder / "summary.json"
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
            actual_m = workbook_distance(summary["actual_distance_m"])
            conditions = summary.get("conditions")
            if not isinstance(conditions, dict):
                conditions = {}
            yaw = tuple(workbook_distance(conditions.get(f"{board}_yaw_deg"))
                        for board in ("initiator", "reflector"))
            yaw = tuple(angle % 360 if finite(angle) else None for angle in yaw)
            calibration = summary.get("calibration")
            firmware = summary.get("firmware")
            simulated = summary.get("simulated") is True
            if not finite(actual_m) or actual_m < 0:
                runs.append(Run(folder, None, yaw, "Tape distance missing", calibration, firmware, simulated))
            else:
                issue = ""
                if calibration is not None:
                    if not isinstance(calibration, dict):
                        issue = "Guided metadata unavailable"
                    elif not guided_trial_complete(summary):
                        issue = "Incomplete guided trial"
                    elif firmware is not None and firmware != calibration.get("firmware"):
                        issue = "Capture and guided firmware metadata disagree"
                runs.append(Run(folder, actual_m, yaw, issue, calibration, firmware, simulated))
        except READ_ERRORS:
            runs.append(Run(folder, None, (None, None), "Tape distance unavailable"))
    return runs


def guided_trial_complete(summary):
    return (summary.get("stop_reason") == "target_reached"
            and summary.get("target_reached") is True
            and summary.get("reports_saved") is True
            and summary.get("finite_filtered_estimate_count") == 100
            and summary.get("estimate_target") == 100)


def calibration_baseline(runs):
    """Read capture-time settings for guided and single trials, never today's headers."""
    usable = [run for run in runs if not run.issue]
    from support.master_results import experiment_signature
    if len({experiment_signature(run.folder) for run in usable}) > 1:
        raise ValueError("Select trials with the same Analyzer profile and test setup together. Compare different settings separately.")
    if len({run.simulated for run in usable}) > 1:
        raise ValueError("Select simulated and real measurements separately.")
    captured = [run for run in usable if run.calibration is not None or run.firmware is not None]
    if not captured:
        return None
    if len(captured) != len(usable):
        raise ValueError("Select folders with captured settings together. Analyze older folders separately.")
    baselines = []
    batches = set()
    for run in captured:
        try:
            firmware = run.firmware
            if run.calibration is not None:
                metadata = run.calibration
                firmware = metadata["firmware"]
                batch = metadata["batch_id"]
                if not isinstance(batch, str) or not batch:
                    raise ValueError
                batches.add(batch)
            projects = firmware["projects"]
            mode = firmware["mode"]
            if mode not in ("isolated", "normal"):
                raise ValueError
            count = 1 if mode == "isolated" else 2
            values, antennas = [], []
            for board in ("initiator", "reflector"):
                project = projects[board]
                antenna = project["physical_antenna"]
                offsets = project["wireless_offsets_cm"]
                if (project["available_antennas"] != count
                        or project["aox_control_pins"] != count - 1
                        or project["offset_config_type"] != "wireless"
                        or not isinstance(offsets, list) or len(offsets) != 4
                        or any(type(value) is not int or not -32768 <= value <= 32767 for value in offsets)):
                    raise ValueError
                if mode == "isolated":
                    if type(antenna) is not int or antenna not in (1, 2):
                        raise ValueError
                elif antenna is not None:
                    raise ValueError
                values.append(tuple(offsets))
                antennas.append(antenna)
            if antennas[0] != antennas[1]:
                raise ValueError
            baselines.append((mode, antennas[0], tuple(values), json.dumps(firmware, sort_keys=True)))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"{run.folder.name} has incomplete calibration baseline metadata.") from error
    if any(baseline != baselines[0] for baseline in baselines[1:]):
        raise ValueError("Selected folders use different antennas or firmware baselines. Select one baseline together.")
    mode, antenna, offsets, _ = baselines[0]
    return {"mode": mode, "pair": ANTENNA_PAIRS[antenna - 1] if mode == "isolated" else None,
            "offsets": offsets, "batches": sorted(batches), "simulated": captured[0].simulated}


def capture_configuration(run):
    firmware = run.calibration.get("firmware") if isinstance(run.calibration, dict) else run.firmware
    if firmware is None:
        return "Unknown - older recording"
    try:
        projects = firmware["projects"]
        if firmware["mode"] == "normal":
            values = ["/".join(map(str, projects[role]["wireless_offsets_cm"][:2]))
                      for role in ("initiator", "reflector")]
            return f"Dual - Ini {values[0]} / Ref {values[1]} cm"
        antenna = projects["initiator"]["physical_antenna"]
        if type(antenna) is not int or antenna not in (1, 2):
            raise ValueError
        values = [projects[role]["wireless_offsets_cm"][antenna - 1] for role in ("initiator", "reflector")]
        return f"ANT{antenna}-ANT{antenna} - Ini {values[0]} / Ref {values[1]} cm"
    except (KeyError, TypeError, IndexError, ValueError):
        return "Configuration unavailable"


def filtered_readings(rows, headers):
    columns = {name: index for index, name in enumerate(headers)}
    if "distanceMain" not in columns:
        raise ValueError("distanceMain column is missing")
    distance_column = columns["distanceMain"]
    id_columns = [columns[key] for key in ("initiatorId", "reflectorId") if key in columns]
    values, board_pairs = [], set()
    for row in rows:
        if distance_column >= len(row):
            continue
        value = workbook_distance(row[distance_column])
        if not finite(value):
            continue
        values.append(value)
        if len(id_columns) == 2 and max(id_columns) < len(row):
            ids = tuple(row[index] for index in id_columns)
            if all(ids):
                board_pairs.add(ids)
    if not values:
        raise ValueError("no finite filtered readings")
    return median(values), board_pairs


def run_measurement(folder):
    workbook_path = folder / "measurements.xlsx"
    if workbook_path.is_file():
        workbook = load_workbook(workbook_path, read_only=True, data_only=True)
        try:
            rows = iter(workbook["Measurements"].values)
            return filtered_readings(rows, next(rows, ()))
        finally:
            workbook.close()
    csv_path = folder / "capture" / "measurements.csv"
    if not csv_path.is_file():
        csv_path = folder / "measurements.csv"
    with csv_path.open(newline="", encoding="utf-8-sig") as stream:
        rows = csv.reader(stream)
        return filtered_readings(rows, next(rows, ()))


def regression_data(runs):
    calibration_baseline(runs)
    angles_by_distance = defaultdict(lambda: defaultdict(list))
    board_pairs, skipped = set(), []
    trial_points = []
    for run in runs:
        if run.issue:
            skipped.append(f"{run.folder.name} · {run.issue}")
            continue
        try:
            measured_m, run_board_pairs = run_measurement(run.folder)
        except READ_ERRORS as error:
            skipped.append(f"{run.folder.name} · {error}")
            continue
        # Give each recorded orientation equal weight even when some runs are longer.
        angles_by_distance[run.actual_m][run.yaw].append(measured_m)
        trial_points.append((run.actual_m, measured_m))
        board_pairs.update(run_board_pairs)
    actual = sorted(angles_by_distance)
    measured = [
        median(median(trials) for trials in angles_by_distance[distance].values())
        for distance in actual
    ]
    if len(actual) == 1:
        # Retain every trial for the repeated-distance plot and median bias.
        actual = [point[0] for point in trial_points]
        measured = [point[1] for point in trial_points]
    return actual, measured, board_pairs, skipped


def distance_fit(actual, measured):
    if len(set(actual)) == 1:
        return DistanceFit(1.0, median(measured) - actual[0])
    slope, intercept = linear_regression(actual, measured)
    return DistanceFit(slope, intercept)


def project_offsets(workspace=FIRMWARE_ROOT):
    offsets = []
    for project in BOARD_PROJECTS:
        header = workspace / project / OFFSET_HEADER
        if project == BOARD_PROJECTS[1] and not header.is_file():
            header = workspace / "bt_cs_soc_reflector" / OFFSET_HEADER
        try:
            source = header.read_text(encoding="utf-8")
            physical_offsets = [re.search(
                rf"^\s*#define\s+GENIE_ANT{antenna}_WIRELESS_OFFSET_CM\s+(-?\d+)\s*$",
                source, re.MULTILINE,
            ) for antenna in (1, 2)]
            match = re.search(
                r"^\s*#define\s+SL_RAIL_UTIL_CS_ANTENNA_OFFSET_WIRELESS_CM\s+\{([^}]+)\}",
                source, re.MULTILINE,
            )
            if all(physical_offsets):
                values = (int(physical_offsets[0][1]), int(physical_offsets[1][1]), 0, 0)
            else:
                values = tuple(int(value) for value in match[1].split(",")) if match else ()
        except (OSError, ValueError):
            values = ()
        offsets.append(values if len(values) == 4 else None)
    return tuple(offsets)


def recommended_offsets(old, tested_pair, used_offsets, actual, fit, board_pairs):
    if tested_pair not in ANTENNA_PAIRS or used_offsets not in USED_OFFSETS[1:]:
        return None, "Confirm the isolated antenna pair\nand the offsets used for the runs."
    if len(board_pairs) > 1:
        return None, "The runs contain multiple board pairs."
    if not actual or fit is None:
        return None, "Select folders with usable tape distances and readings."
    if fit.slope <= 0:
        return None, "The fitted slope must be positive."
    if None in old:
        return None, "Project offset arrays are unavailable."
    # A positive intercept is extra measured distance. Firmware compensates that
    # delay using the sum of the two boards' offsets for this physical path.
    index = ANTENNA_PAIRS.index(tested_pair)
    change_cm = fit.intercept * 100 / 2
    result = []
    for values in old:
        updated = list(values)
        baseline = values[index] if used_offsets == USED_OFFSETS[1] else 0
        updated[index] = round(baseline + change_cm)
        if not -32768 <= updated[index] <= 32767:
            return None, "The fitted offset exceeds the firmware range."
        result.append(tuple(updated))
    return tuple(result), f"{tested_pair} · {change_cm:+.1f} cm per board"


def offset_text(offsets):
    def array(values):
        return "{ " + ", ".join(map(str, values)) + " }" if values else "Unavailable"

    if offsets[0] == offsets[1]:
        return array(offsets[0])
    return f"Initiator  {array(offsets[0])}\nReflector  {array(offsets[1])}"


def show_result(root, actual, measured, board_pairs, skipped, baseline=None):
    normal = baseline is not None and baseline.get("mode") == "normal"
    single_distance = len(set(actual)) == 1
    title = ("Genie accuracy check" if normal else
             "Genie repeated-distance analysis" if single_distance else "Genie distance regression")
    if baseline and baseline.get("simulated"):
        title += " - SIMULATED"
    fit = distance_fit(actual, measured)
    window = tk.Toplevel(root)
    window.title(title)
    window.geometry("1130x700")
    window.minsize(900, 565)
    figure = Figure(figsize=(11, 6), layout="constrained", facecolor="white")
    plot, panel = figure.subplots(1, 2, gridspec_kw={"width_ratios": [2.4, 1.15]})
    panel.set_axis_off()
    if single_distance:
        plot.scatter(range(1, len(measured) + 1), measured, color="#185589",
                     zorder=3, label="Trial filtered medians")
        plot.axhline(actual[0], color="#b63131", linestyle=":",
                     label=f"Tape distance ({actual[0]:g} m)")
        plot.axhline(actual[0] + fit.intercept, color="#185589", linestyle="--",
                     label="Median of trial medians")
        plot.set(title=f"Repeated trials at {actual[0]:g} m", xlabel="Trial",
                 ylabel="Median filtered distance (m)")
        plot.xaxis.set_major_locator(MaxNLocator(integer=True))
    else:
        low, high = min(actual + measured), max(actual + measured)
        padding = max((high - low) * 0.07, 0.1)
        bounds = (low - padding, high + padding)
        plot.plot(bounds, bounds, color="#b63131", linestyle=":", label="Ideal reading (y = x)")
        plot.scatter(actual, measured, color="#185589", zorder=3, label="Filtered medians")
        endpoints = (actual[0], actual[-1])
        plot.plot(endpoints, [fit.slope * x + fit.intercept for x in endpoints],
                  color="#185589", label="Linear fit")
        plot.set(title=title, xlabel="Actual tape distance (m)",
                 ylabel="Median filtered distance (m)", xlim=bounds, ylim=bounds)
        plot.set_aspect("equal", adjustable="box")
    plot.grid(alpha=0.25)
    plot.legend(loc="best" if single_distance else "upper left")
    old = baseline["offsets"] if baseline else project_offsets()

    controls = ttk.Frame(window, padding=(10, 6))
    controls.pack(side="bottom", fill="x")
    if baseline:
        pair_label = "ANT1 + ANT2" if normal else baseline["pair"]
        ttk.Label(controls, text=f"{pair_label} · Saved capture configuration").pack(side="left")
    else:
        ttk.Label(controls, text="Legacy recording: confirm antenna pair").pack(side="left")
        pair_box = ttk.Combobox(controls, values=("Accuracy check", *ANTENNA_PAIRS),
                                state="readonly", width=16)
        pair_box.current(0)
        pair_box.pack(side="left", padx=(6, 14))
        ttk.Label(controls, text="Offsets used during capture").pack(side="left")
        offsets_box = ttk.Combobox(controls, values=USED_OFFSETS, state="readonly", width=24)
        offsets_box.current(0)
        offsets_box.pack(side="left", padx=6)
    if skipped:
        ttk.Button(
            controls, text=f"Skipped folders ({len(skipped)})",
            command=lambda: messagebox.showinfo(
                "Skipped folders", "\n".join(skipped), parent=window),
        ).pack(side="right")

    canvas = FigureCanvasTkAgg(figure, master=window)
    NavigationToolbar2Tk(canvas, window)
    canvas.get_tk_widget().pack(fill="both", expand=True)

    def update_panel(event=None):
        panel.clear()
        panel.set_axis_off()
        transform = panel.transAxes
        if single_distance:
            heading = f"{len(actual)} trials at {actual[0]:g} m"
            fit_text = f"Median bias {fit.intercept:+.3f} m"
            if not normal:
                fit_text += "\nOffset estimate assumes slope 1."
        else:
            heading = f"{len(actual)} distinct tape distances"
            fit_text = (f"y = {fit.slope:.4f}x {fit.intercept:+.3f}\n"
                        f"Intercept {fit.intercept:+.3f} m")
        if normal:
            new = None
            detail = "ANT1 and ANT2 were used together.\nOffset calibration uses an isolated antenna pair."
        else:
            pair = baseline["pair"] if baseline else pair_box.get()
            used = USED_OFFSETS[1] if baseline else offsets_box.get()
            new, detail = recommended_offsets(old, pair, used,
                                              actual, fit, board_pairs)
        panel.text(0, 0.89, heading, transform=transform,
                   weight="bold", fontsize=11)
        panel.text(0, 0.80, fit_text, transform=transform, va="top", fontsize=10)
        offset_label = "Captured wireless offsets (cm)" if baseline else "Current project offsets (cm)"
        panel.text(0, 0.60, offset_label, transform=transform,
                   weight="bold", fontsize=11)
        panel.text(0, 0.54, offset_text(old), transform=transform, va="top", fontsize=10)
        offset_heading = "Estimated new offsets (cm)" if single_distance else "Recommended new offsets (cm)"
        if normal:
            offset_heading = "Normal dual-antenna testing"
        panel.text(0, 0.37, offset_heading, transform=transform,
                   weight="bold", fontsize=11)
        if new:
            panel.text(0, 0.31, offset_text(new), transform=transform, va="top", fontsize=10)
        panel.text(0, 0.19 if new else 0.31, detail, transform=transform, va="top", fontsize=9)
        if baseline:
            provenance = "Batch " + ", ".join(baseline["batches"]) if baseline["batches"] else "Single-trial capture configuration"
            panel.text(0, 0.02, provenance,
                       transform=transform, va="bottom", fontsize=8, wrap=True)
        canvas.draw_idle()

    if not baseline:
        pair_box.bind("<<ComboboxSelected>>", update_panel)
        offsets_box.bind("<<ComboboxSelected>>", update_panel)
    update_panel()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", nargs="?", type=Path, default=PROJECT_DATA / "live",
                        help="Live folder or guided batch to analyze; defaults to this project's live results.")
    live_dir = parser.parse_args().folder
    root = tk.Tk()
    root.title("Genie calibration and accuracy check")
    root.geometry("1130x430")
    root.minsize(940, 330)
    frame = ttk.Frame(root, padding=14)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Click a batch to analyze it. For single trials, select rows and click Analyze selected.").pack(
        anchor="w", pady=(0, 8))
    ttk.Label(frame, text="Calibration estimates offsets. Normal-mode analysis checks accuracy and keeps offsets fixed.").pack(
        anchor="w", pady=(0, 10))
    table_frame = ttk.Frame(frame)
    table_frame.pack(fill="both", expand=True)
    table_frame.columnconfigure(0, weight=1)
    table_frame.rowconfigure(0, weight=1)
    style = ttk.Style(root)
    style.configure("Regression.Treeview", font=("Segoe UI", 10), rowheight=28)
    style.configure("Regression.Treeview.Heading", font=("Segoe UI", 10, "bold"))
    tree = ttk.Treeview(table_frame, columns=("folder", "trials", "tape", "configuration", "status"),
                        show="headings", selectmode="extended", style="Regression.Treeview")
    scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
    horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
    for key, heading, width in (
        ("folder", "Guided batch / single trial", 330), ("trials", "Trials", 55),
        ("tape", "Actual distances (m)", 150),
        ("configuration", "Saved configuration", 310), ("status", "Status", 215),
    ):
        tree.heading(key, text=heading)
        tree.column(key, width=width, minwidth=width, stretch=key == "folder", anchor="w")
    tree.grid(row=0, column=0, sticky="nsew")
    scrollbar.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    runs = find_runs(live_dir)
    groups = group_runs(runs)
    for index, group in enumerate(groups):
        tree.insert("", "end", iid=str(index), values=group_values(group))

    def fit_selection():
        selected = [run for index in tree.selection() for run in groups[int(index)].runs]
        if not selected:
            messagebox.showinfo("Select folders", "Select the live folders to fit together.", parent=root)
            return
        try:
            actual, measured, board_pairs, skipped = regression_data(selected)
            baseline = calibration_baseline(selected)
        except ValueError as error:
            messagebox.showerror("Folder selection", str(error), parent=root)
            return
        if not actual:
            messagebox.showinfo("No usable runs", "\n".join(skipped), parent=root)
            return
        show_result(root, actual, measured, board_pairs, skipped, baseline)

    def click_batch(event):
        row = tree.identify_row(event.y)
        if (row and len(tree.selection()) == 1 and row in tree.selection()
                and groups[int(row)].guided and not event.state & 0x0005):
            fit_selection()

    footer = ttk.Frame(frame)
    footer.pack(fill="x", pady=(10, 0))
    batch_count = sum(group.guided for group in groups)
    ttk.Label(footer, text=f"{batch_count} guided batches; {len(groups) - batch_count} single trials").pack(side="left")
    ttk.Button(footer, text="Analyze selected", command=fit_selection).pack(side="right")
    tree.bind("<ButtonRelease-1>", click_batch)
    tree.bind("<Control-a>", lambda event: tree.selection_set(tree.get_children()))
    tree.bind("<Return>", lambda event: fit_selection())
    tree.focus_set()
    root.mainloop()


if __name__ == "__main__":
    main()
