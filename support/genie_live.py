"""Follow the per-reflector JSONL recording written by CS Analyzer 1.2.2.

The Analyzer uses Chromium's save-file API: bytes are in .crswap until Stop.
Source: installed Analyzer's HadmWorkerEngine result writer and built-in help.
"""
import csv
from collections import deque
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from glob import escape
import hashlib
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
from statistics import median
import subprocess
from tempfile import TemporaryDirectory
from textwrap import wrap
from threading import Thread
import time
from uuid import uuid4

from .trial_storage import (create_condition_folder, migrate_single_trials,
                            normalize_conditions, normalize_yaw)


FIELDS = ("connection", "initiatorId", "reflectorId", "distanceMainRaw",
          "distanceMain", "distanceSubRaw", "distanceSub", "rssiDistance",
          "likelinessMain", "likelinessSub", "velocityMain", "velocitySub")
PROJECT_DATA = Path(__file__).resolve().parent.parent / "data"
HEIGHT_BOARDS = {"initiator_height_m": "Initiator", "reflector_height_m": "Reflector"}
TRIAL_CONDITION_LABELS = {
    "board_heights": "Height (m)",
    **{key: f"{board} height" for key, board in HEIGHT_BOARDS.items()},
    "initiator_yaw_deg": "Initiator yaw",
    "reflector_yaw_deg": "Reflector yaw",
    "orientation": "Orientation",
    "setup_notes": "Setup notes",
}
TAPE_REFERENCE_STYLE = {"color": "#b63131", "linestyle": ":", "linewidth": 1.7}


def write_measurements_xlsx(csv_path, output=None):
    """Create an Excel view of a live CSV with readable auto-sized columns."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("Excel workbook not created; install dependencies with: python -m pip install -r support/requirements.txt", flush=True)
        return None
    csv_path = Path(csv_path)
    with csv_path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        headers = reader.fieldnames or []
        rows = list(reader)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Measurements"
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(header, "") for header in headers])
    sheet.freeze_panes = "A2"
    if headers:
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(1, sheet.max_row)}"
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.row_dimensions[1].height = 32
    for index, header in enumerate(headers, 1):
        values = [str(row.get(header, "") or "") for row in rows]
        width = max([len(str(header))] + [len(value) for value in values] + [10]) + 2
        sheet.column_dimensions[get_column_letter(index)].width = min(width, 45)
    output = csv_path.with_suffix(".xlsx") if output is None else Path(output)
    workbook.save(output)
    return output


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def last_board_heights(out):
    from .trial_storage import trial_folders

    latest_heights = {}
    latest_saved_at = -1
    for session in trial_folders(out):
        path = session / "capture/summary.json"
        try:
            saved_at = path.stat().st_mtime_ns
            if saved_at <= latest_saved_at:
                continue
            summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        conditions = summary.get("conditions") if isinstance(summary, dict) else None
        if not isinstance(conditions, dict):
            continue
        heights = {key: conditions[key] for key in HEIGHT_BOARDS
                   if finite(conditions.get(key)) and conditions[key] >= 0}
        if heights:
            latest_heights = heights
            latest_saved_at = saved_at
    return latest_heights


def prompt_trial_details(out=PROJECT_DATA / "live"):
    print("Trial setup. Type skip to leave a field blank. Enter uses the default when shown.", flush=True)

    def answer(question, default=None):
        value = input(question).strip()
        if value.casefold() == "skip":
            return None
        return value or default

    def number_answer(question, hint, *, nonnegative=False, default=None):
        while True:
            value = answer(question, default)
            if value is None:
                return None
            try:
                number = float(value)
            except ValueError:
                number = None
            if finite(number) and (not nonnegative or number >= 0):
                return number
            print(hint, flush=True)

    def prompt_heights():
        previous = last_board_heights(out)
        if previous:
            displayed = ", ".join(f"{board} {previous[key]:g} m" if key in previous else f"{board} unset"
                                  for key, board in HEIGHT_BOARDS.items())
            while True:
                choice = input(f"Have board heights changed ({displayed})? [yes/no/skip, Enter = no] ").strip().casefold()
                if choice in ("", "no"):
                    return previous
                if choice == "skip":
                    return {}
                if choice == "yes":
                    break
                print("Enter yes, no, or skip.", flush=True)

        heights = {}
        for key, board in HEIGHT_BOARDS.items():
            height = previous.get(key)
            default = f" [{height:g}]" if height is not None else ""
            heights[key] = number_answer(f"{board} board height in meters{default}? ",
                                         "Enter a nonnegative height in meters, such as 1.5, or type skip.",
                                         nonnegative=True, default=height)
        return heights

    actual_distance = number_answer("Actual tape-measured distance in meters? ",
                                    "Enter a nonnegative number in meters, such as 1 or 1.0, or type skip.",
                                    nonnegative=True)
    heights = prompt_heights()
    print("Measure yaw clockwise from each board's 0-degree baseline, viewed from above (0 to 359 degrees).", flush=True)
    yaw_hint = "Enter an angle such as 0, 45, or 315, or type skip."
    initiator_yaw = number_answer("Initiator yaw in degrees? ", yaw_hint)
    reflector_yaw = number_answer("Reflector yaw in degrees? ", yaw_hint)
    notes = answer("Any other test conditions to note (e.g. coin cell, obstacle, settings change)? ")
    conditions = {**heights, "initiator_yaw_deg": initiator_yaw,
                  "reflector_yaw_deg": reflector_yaw, "setup_notes": notes}
    return actual_distance, normalize_conditions({key: value for key, value in conditions.items() if value is not None})


def workbook_distance(value):
    # The live Excel export stores CSV values as text. Excel edits can use numeric cells.
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return math.nan
    return value if finite(value) else math.nan


def write_measurement_plots(workbook_path, actual_distance=None, conditions=None):
    """Save two report PNGs using the rows in the live Measurements worksheet."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import MaxNLocator
    from openpyxl import load_workbook

    workbook_path = Path(workbook_path)
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        rows = workbook["Measurements"].values
        columns = {header: i for i, header in enumerate(next(rows, ()))}
        raw_column = columns.get("distanceMainRaw")
        filtered_column = columns.get("distanceMain")
        raw, filtered = [], []
        for row in rows:
            raw.append(workbook_distance(row[raw_column]) if raw_column is not None else math.nan)
            filtered.append(workbook_distance(row[filtered_column]) if filtered_column is not None else math.nan)
    finally:
        workbook.close()

    raw_values = [value for value in raw if finite(value)]
    filtered_values = [value for value in filtered if finite(value)]
    reading_numbers = range(1, len(filtered) + 1)
    session_id = workbook_path.parent.name
    blue = "#185589"
    subtitle = f"Session {session_id}  |  {len(filtered)} recorded rows"
    reference_label = f"Tape distance ({actual_distance:g} m)" if actual_distance is not None else None
    condition_lines = []
    for key, label in TRIAL_CONDITION_LABELS.items():
        value = (conditions or {}).get(key)
        if value is None or value == "":
            continue
        if key.endswith("_yaw_deg") and finite(value):
            value = f"{normalize_yaw(value):g}\N{DEGREE SIGN}"
        if key in HEIGHT_BOARDS and finite(value):
            value = f"{value:g} m"
        condition_lines.extend(wrap(f"{label} = {value}", width=72))
    extra_height = 0.20 * len(condition_lines)

    def style(axes):
        axes.set_axisbelow(True)
        axes.grid(axis="y", color="#dde5ec", linewidth=0.8)
        axes.spines[["top", "right"]].set_visible(False)

    def empty_message(axes, message):
        axes.text(0.5, 0.5, message, transform=axes.transAxes, ha="center", va="center")

    # Keep the full range visible. Add a filtered detail panel when an extreme
    # raw reading substantially compresses the filtered trace.
    context = filtered_values.copy()
    if reference_label:
        context.append(actual_distance)
    detail = False
    if raw_values and filtered_values:
        context_span = max(context) - min(context)
        full_span = max(max(raw_values), max(context)) - min(min(raw_values), min(context))
        detail = full_span > 4 * max(context_span, 0.05)

    order_fig = Figure(figsize=(7.5, (5.3 if detail else 4.5) + extra_height), layout="constrained")
    FigureCanvasAgg(order_fig)
    order_fig.suptitle("Distance readings in capture order", fontweight="bold")
    order_axes = order_fig.subplots(2 if detail else 1, sharex=True, squeeze=False)[:, 0]
    order = order_axes[0]
    order.set_title("\n".join([subtitle, *condition_lines]), fontsize=9.5, color="#46576a", parse_math=False)
    if raw_values:
        order.scatter(reading_numbers, raw, s=9, alpha=0.55, linewidths=0,
                      color="#697887", label="Raw (distanceMainRaw)")
    if filtered_values:
        order.plot(reading_numbers, filtered, ".-", color=blue, linewidth=1.25,
                   markersize=2.5, label="Filtered (distanceMain)")
    if reference_label:
        order.axhline(actual_distance, label=reference_label, **TAPE_REFERENCE_STYLE)
    if raw_values or filtered_values or reference_label:
        order.legend(loc="best", frameon=False, fontsize=9)
    if not (raw_values or filtered_values):
        empty_message(order, "No finite main distance estimates")
    if detail:
        zoom = order_axes[1]
        zoom.set_title("Filtered detail", loc="left", fontsize=10)
        zoom.plot(reading_numbers, filtered, ".-", color=blue, linewidth=1.25, markersize=2.5)
        if reference_label:
            zoom.axhline(actual_distance, **TAPE_REFERENCE_STYLE)
    for axes in order_axes:
        axes.set_ylabel("Reported distance (m)")
        style(axes)
    order_axes[-1].set_xlabel("Reading number")
    order_axes[-1].set_xlim(0, len(filtered) + 1)
    order_axes[-1].xaxis.set_major_locator(MaxNLocator(integer=True))

    hist_fig = Figure(figsize=(7.5, 4.5 + extra_height), layout="constrained")
    FigureCanvasAgg(hist_fig)
    hist_subtitle = f"{subtitle}  |  {len(filtered_values)} finite filtered estimates"
    hist_fig.suptitle("Distribution of filtered distance estimates", fontweight="bold")
    histogram = hist_fig.subplots()
    histogram.set_title("\n".join([hist_subtitle, *condition_lines]), fontsize=9.5, color="#46576a", parse_math=False)
    if filtered_values:
        midpoint = median(filtered_values)
        histogram.hist(filtered_values, bins="sturges", color="#6698bc", edgecolor="white")
        histogram.axvline(midpoint, color=blue, linewidth=2, label=f"Filtered median ({midpoint:.2f} m)")
    else:
        empty_message(histogram, "No finite distanceMain estimates")
    if reference_label:
        histogram.axvline(actual_distance, label=reference_label, **TAPE_REFERENCE_STYLE)
    if filtered_values or reference_label:
        histogram.legend(loc="best", frameon=False, fontsize=9)
    histogram.set(xlabel="Filtered distance, distanceMain (m)", ylabel="Number of readings")
    histogram.yaxis.set_major_locator(MaxNLocator(integer=True))
    style(histogram)

    figures = {"readings_in_order.png": order_fig, "filtered_distance_distribution.png": hist_fig}
    with TemporaryDirectory(dir=workbook_path.parent) as temp:
        for name, figure in figures.items():
            figure.savefig(Path(temp) / name, dpi=300, facecolor="white")
        for name in figures:
            (Path(temp) / name).replace(workbook_path.parent / name)


def find_recording(folder):
    candidates = list(Path(folder).glob("record-*.jsonl*"))
    candidates = [p for p in candidates if p.is_file() and
                  (p.name.endswith(".jsonl") or ".jsonl." in p.name and p.name.endswith(".crswap"))]
    if not candidates:
        raise ValueError(f"No Analyzer recording in {folder}. Click the reflector's record button and save there first.")
    newest = max(candidates, key=lambda p: p.stat().st_mtime_ns)
    return newest.with_name(newest.name.split(".jsonl", 1)[0] + ".jsonl")


def recording_swaps(path):
    return path.parent.glob(escape(path.name) + "*.crswap")


def same_recording_bytes(source, saved):
    if source.stat().st_size != saved.stat().st_size:
        return False
    with source.open("rb") as original, saved.open("rb") as captured:
        for block in iter(lambda: original.read(65536), b""):
            if block != captured.read(len(block)):
                return False
        return not captured.read(1)


def recycle_file(path):
    # Pass the filename as data. Analyzer filenames may contain shell metacharacters.
    command = """$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName Microsoft.VisualBasic
[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
    $env:GENIE_ANALYZER_RECYCLE_PATH,
    [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
    [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)"""
    env = {**os.environ, "GENIE_ANALYZER_RECYCLE_PATH": str(path.resolve())}
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        env=env, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode or path.exists():
        raise OSError(result.stderr.strip() or "Windows did not recycle the recording")


def recycle_duplicate_recording(source, saved):
    if (os.name != "nt" or source.parent != PROJECT_DATA.resolve()
            or not source.match("record-*.jsonl")):
        return
    staged = saved.with_name(source.name)
    try:
        if any(recording_swaps(source)):
            print(f"Kept {source.name} because Analyzer is still recording.", flush=True)
            return
        if source.is_symlink() or not source.is_file() or not same_recording_bytes(source, saved):
            print(f"Kept {source.name} because it could not be verified against the saved capture.", flush=True)
            return

        # Check again after moving the candidate to this session. A replacement
        # or append during the first comparison must survive cleanup.
        source.rename(staged)
        try:
            if staged.is_symlink() or any(recording_swaps(source)) or not same_recording_bytes(staged, saved):
                print(f"Kept {source.name} because it changed during cleanup.", flush=True)
                return
            recycle_file(staged)
            print(f"Sent the duplicate Analyzer recording {source.name} to the Recycle Bin.", flush=True)
        finally:
            if staged.exists() or staged.is_symlink():
                # On Windows rename fails if Analyzer already recreated source.
                # In that case the original remains in the session's capture folder.
                staged.rename(source)
    except OSError as error:
        print(f"Analyzer file cleanup incomplete for {source.name}. "
              f"Check {source.parent} and {saved.parent}. {error}", flush=True)


class RecordingReader:
    """Read complete lines once, including across Chromium's final rename."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        if self.path.name.endswith(".crswap"):
            self.path = self.path.with_name(self.path.name.split(".jsonl", 1)[0] + ".jsonl")
        self.offset = 0
        self.pending = b""
        self.line = 0
        self.digest = hashlib.sha256()
        self.active = None

    def poll(self):
        swaps = list(recording_swaps(self.path))
        if len(swaps) > 1:
            raise ValueError("Multiple active temporary recordings; specify an unambiguous recording")
        source = swaps[0] if swaps else self.path
        try:
            with source.open("rb") as stream:
                size = stream.seek(0, 2)
                if size < self.offset:
                    raise ValueError("Recording was truncated. Start a new live session for the new recording.")
                stream.seek(0)
                if source != self.active and self.offset:
                    digest = hashlib.sha256()
                    remaining = self.offset
                    while remaining:
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            raise ValueError("Recording changed during rename")
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if digest.digest() != self.digest.digest():
                        raise ValueError("Recording was replaced with different data; start a new live session")
                else:
                    stream.seek(self.offset)
                chunk = stream.read(65536)
        except FileNotFoundError:
            return []  # Save/rename can briefly make the path disappear.
        self.active = source
        self.offset += len(chunk)
        self.digest.update(chunk)
        self.pending += chunk
        lines = self.pending.split(b"\n")
        self.pending = lines.pop()
        result = []
        for raw in lines:
            self.line += 1
            result.append((self.line, raw + b"\n"))
        return result


def create_live_session(out, actual_distance=None, conditions=None):
    migrate_single_trials(out)
    return create_condition_folder(out, actual_distance, conditions or {})


class AnalyzerCapture:
    def __init__(self, path, process):
        self.path = path
        self.process = process

    def running(self):
        return self.process.poll() is None

    def stop(self, check=True):
        # EOF asks the automation process to click Stop and flush Analyzer's stream.
        with suppress(OSError):
            self.process.stdin.close()
        try:
            result = self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            result = self.process.wait()
        if check:
            reason = self.process.stdout.readline().strip()
            if result or not self.path.is_file():
                print("Analyzer stopped before closing the recording. Exporting saved readings.", flush=True)
                return "no_readings" if reason == "no_readings" else "analyzer_interrupted"
            return reason or "stopped"


class SessionCapture:
    """One recording within a connected Analyzer session."""

    def __init__(self, connection, path):
        self.connection = connection
        self.path = path
        self.reason = None
        self.stop_sent = False

    def running(self):
        self.connection.drain()
        return self.reason is None and self.connection.running()

    def stop(self, check=True):
        self.connection.drain()
        if self.reason is None and self.connection.running() and not self.stop_sent:
            self.connection.send({"command": "stop"})
            self.stop_sent = True
        deadline = time.monotonic() + 20
        while self.reason is None and self.connection.running() and time.monotonic() < deadline:
            self.connection.receive(0.25)
        self.connection.drain()
        if self.reason is None:
            self.connection.close()
        return self.reason or "analyzer_interrupted"


class AnalyzerConnection:
    """Exchange recording commands while keeping the boards connected."""

    def __init__(self, folder, process):
        self.folder = folder
        self.process = process
        self.events = Queue()
        self.capture = None
        self.failure = None
        self.reader = Thread(target=self.read_events, daemon=True)
        self.reader.start()

    def read_events(self):
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict) or "event" not in event:
                        raise ValueError("missing event name")
                except ValueError as error:
                    event = {"event": "error", "message": f"Invalid Analyzer response. {error}"}
                self.events.put(event)
        finally:
            self.events.put({"event": "exited"})

    def receive(self, timeout=0):
        try:
            event = self.events.get(timeout=timeout)
        except Empty:
            return None
        if event["event"] == "error":
            self.failure = event.get("message", "Analyzer session failed")
        if event["event"] == "exited":
            self.failure = self.failure or "Analyzer session closed its control connection."
        if (event["event"] == "stopped" and self.capture is not None
                and event.get("path") == str(self.capture.path)):
            self.capture.reason = event.get("reason", "stopped")
        return event

    def drain(self):
        while self.receive() is not None:
            pass

    def running(self):
        return self.failure is None and self.process.poll() is None

    def send(self, command):
        if not self.running():
            raise ValueError(self.failure or "Analyzer session has ended. Restart Genie to reconnect.")
        self.process.stdin.write(json.dumps(command) + "\n")
        self.process.stdin.flush()

    def wait_for(self, names, timeout, path=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.receive(min(0.25, max(0, deadline - time.monotonic())))
            if (event is not None and event["event"] in names
                    and (path is None or event.get("path") == str(path))):
                return event
            if self.failure or not self.running():
                raise ValueError(self.failure or "Analyzer session ended before it was ready.")
        raise ValueError("Analyzer session timed out. Restart Genie to reconnect.")

    def start(self, max_estimates=100):
        if type(max_estimates) is not int or max_estimates <= 0:
            raise ValueError("Estimate target must be a positive integer")
        self.drain()
        if self.capture is not None and self.capture.reason is None:
            raise ValueError("Finish the current recording before starting another.")
        path = self.folder / f"record-genie-{uuid4().hex}.jsonl"
        self.capture = SessionCapture(self, path)
        self.send({"command": "start", "path": str(path), "max_estimates": max_estimates})
        self.wait_for({"recording", "stopped"}, 40, path=path)
        return self.capture

    def close(self):
        if self.process.poll() is None:
            with suppress(OSError, ValueError):
                self.send({"command": "quit"})
            with suppress(OSError):
                self.process.stdin.close()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=1)
        self.drain()


def analyzer_executable():
    if os.name != "nt":
        raise ValueError("Automatic Analyzer recording requires Windows. Use analyzer.py live for manual recordings.")
    analyzer = (Path.home() / ".silabs" / "slt" / "installs" / "archive"
                / "cs_analyzer-v1.2.2" / "CS_Analyzer.exe")
    if not analyzer.is_file():
        raise ValueError("Automatic recording requires the installed Channel Sounding Analyzer 1.2.2.")
    return analyzer


@contextmanager
def analyzer_session(folder):
    analyzer = analyzer_executable()
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).with_name("genie_analyzer.cjs")
    print("Opening Channel Sounding Analyzer for this guided session.", flush=True)
    with TemporaryDirectory(prefix="genie-analyzer-", ignore_cleanup_errors=True) as profile:
        with subprocess.Popen(
            [str(analyzer), str(script), profile, str(folder), "--session"],
            env={**os.environ, "ELECTRON_RUN_AS_NODE": "1"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW,
        ) as process:
            connection = AnalyzerConnection(folder, process)
            try:
                connection.wait_for({"connected"}, 100)
                yield connection
            finally:
                connection.close()


@contextmanager
def automatic_recording(folder):
    analyzer = analyzer_executable()
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = folder / f"record-genie-{now}-{uuid4().hex[:8]}.jsonl"
    script = Path(__file__).with_name("genie_analyzer.cjs")
    print("Opening Channel Sounding Analyzer for this trial.", flush=True)
    with TemporaryDirectory(prefix="genie-analyzer-", ignore_cleanup_errors=True) as profile:
        with subprocess.Popen(
            [str(analyzer), str(script), profile, str(path)],
            env={**os.environ, "ELECTRON_RUN_AS_NODE": "1"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW,
        ) as process:
            capture = AnalyzerCapture(path, process)
            try:
                if process.stdout.readline().strip() != "READY":
                    raise ValueError("Analyzer did not start recording. Check the Analyzer startup details above for the connection or ranging failure.")
                yield capture
            finally:
                capture.stop(check=False)


def run_live(log=None, folder="data", out="data/live", duration=None,
             interval=0.5, plot=True, actual_distance=None, field="distanceMainRaw",
             *, recording=None, conditions=None, max_estimates=None, calibration=None):
    if not finite(interval) or interval <= 0:
        raise ValueError("Polling interval must be positive and finite")
    if duration is not None and (not finite(duration) or duration <= 0):
        raise ValueError("Duration must be positive and finite")
    if actual_distance is not None and (not finite(actual_distance) or actual_distance < 0):
        raise ValueError("Actual tape distance must be finite and nonnegative")
    if max_estimates is not None and (isinstance(max_estimates, bool)
                                     or not isinstance(max_estimates, int) or max_estimates <= 0):
        raise ValueError("Estimate target must be a positive integer")
    reader = RecordingReader(recording.path if recording is not None else log or find_recording(folder))
    if not reader.path.exists() and not any(recording_swaps(reader.path)):
        raise ValueError(f"Recording does not exist: {reader.path}")
    plt = None
    if plot:
        import matplotlib.pyplot as plt
        plt.ion()
        fig, ax = plt.subplots()
        fig.canvas.manager.set_window_title("Genie live Analyzer measurements")
        traces = {key: ax.plot([], [], ".", label=label)[0] for key, label in
                  (("distanceMainRaw", "Main raw"), ("distanceMain", "Main filtered"),
                   ("rssiDistance", "RSSI distance"))}
        if actual_distance is not None:
            ax.axhline(actual_distance, label=f"Tape distance ({actual_distance:g} m)", **TAPE_REFERENCE_STYLE)
        ax.set(xlabel="Recording sample number (source has no timestamps)", ylabel="Distance (m)")
        ax.legend()
        plt.show(block=False)
    started_at_utc = datetime.now(timezone.utc)
    conditions = normalize_conditions(conditions)
    session = create_live_session(out, actual_distance, conditions)
    capture = session / "capture"
    capture.mkdir()
    raw_path = capture / "raw.jsonl"
    csv_path = capture / "measurements.csv"
    columns = ["source_line", "read_at_utc", *FIELDS]
    points = deque(maxlen=2000)
    count = 0
    selected_count = 0
    filtered_count = 0
    additional_results = 0
    mean = 0.0
    abs_error_sum = squared_error_sum = signed_error_sum = 0.0
    peer = None
    started = time.monotonic()
    last_message = started
    plots_saved = False
    stop_reason = None
    user_stopped = False
    print(f"Following {reader.path}\nCapture: {session}\nReading existing records, then following new ones. Ctrl+C or close the graph to stop.", flush=True)

    def summary():
        return {"source": str(reader.path), "session_started_at_utc": started_at_utc.isoformat(),
                "records": count, "selected_field": field, "stop_reason": stop_reason,
                "finite_reported_distance_count": selected_count,
                "finite_filtered_estimate_count": filtered_count,
                "estimate_target": max_estimates,
                "target_reached": max_estimates is not None and filtered_count >= max_estimates,
                "additional_recorded_results": additional_results,
                "reports_saved": plots_saved,
                "user_stopped": user_stopped,
                "board_pair": list(peer) if peer is not None else None,
                "calibration": calibration,
                "mean_reported_distance_m": mean if selected_count else None,
                "actual_distance_m": actual_distance, "conditions": conditions or {},
                "mean_signed_error_m": signed_error_sum / selected_count if selected_count and actual_distance is not None else None,
                "mae_m": abs_error_sum / selected_count if selected_count and actual_distance is not None else None,
                "rmse_m": math.sqrt(squared_error_sum / selected_count) if selected_count and actual_distance is not None else None,
                "source_bytes_read": reader.offset, "pending_partial_line_bytes": len(reader.pending),
                "limitation": "Includes existing recording history. Read time is host observation time, not measurement time. Finite reported results do not prove valid status or complete attempts. No reliability, latency, or rate inferred."}

    def save_summary():
        (capture / "summary.json").write_text(json.dumps(summary(), indent=2) + "\n", encoding="utf-8")

    def save_batch(batch, raw_capture, writer):
        nonlocal count, selected_count, filtered_count, additional_results
        nonlocal mean, peer, signed_error_sum, abs_error_sum, squared_error_sum
        # Preserve the entire polled batch before parsing, including any tail
        # already consumed by the reader when an earlier line is malformed.
        raw_capture.writelines(raw for _, raw in batch)
        raw_capture.flush()
        for line, raw in batch:
            try:
                obj = json.loads(raw.decode("utf-8-sig"))
            except (ValueError, UnicodeError) as error:
                raise ValueError(f"Invalid JSON at source line {line}; raw bytes preserved in {capture}") from error
            if not isinstance(obj, dict) or not obj.get("reflectorId") or not obj.get("initiatorId") or not any(k in obj for k in ("distanceMain", "distanceMainRaw", "distanceSub", "distanceSubRaw")):
                raise ValueError(f"Line {line}: expected a per-reflector Analyzer result recording, not a BGAPI log")
            identity = (obj["initiatorId"], obj["reflectorId"])
            if peer is not None and peer != identity:
                raise ValueError("Recording contains multiple peers; use a separate recording per reflector")
            peer = identity
            count += 1
            if max_estimates is not None and filtered_count >= max_estimates:
                additional_results += 1
                continue
            if max_estimates is not None and not finite(obj.get("distanceMain")):
                continue
            row = {key: obj.get(key) for key in FIELDS}
            row.update(source_line=line, read_at_utc=datetime.now(timezone.utc).isoformat())
            writer.writerow(row)
            if finite(obj.get("distanceMain")):
                filtered_count += 1
            value = obj.get(field)
            if finite(value):
                selected_count += 1
                mean += (value - mean) / selected_count
                if actual_distance is not None:
                    error = value - actual_distance
                    signed_error_sum += error
                    abs_error_sum += abs(error)
                    squared_error_sum += error * error
            points.append((line, obj))

    def monitor(raw_capture, csv_file, writer):
        nonlocal last_message, user_stopped
        while ((duration is None or time.monotonic() - started < duration)
               and (max_estimates is None or filtered_count < max_estimates)):
            batch = reader.poll()
            save_batch(batch, raw_capture, writer)
            csv_file.flush()
            now = time.monotonic()
            if batch:
                save_summary()
                latest = points[-1][1] if points else {}
                target_text = f" | {filtered_count}/{max_estimates} filtered estimates" if max_estimates else ""
                print((f"{count} records | {field}={latest.get(field)} m | mean={mean:.4f} m"
                       if selected_count else f"{count} records | no finite {field} values") + target_text, flush=True)
                last_message = now
            elif recording is None and now - last_message >= 5:
                print(f"Waiting for new Analyzer records ({count} read). Check that measurements and reflector recording are running.", flush=True)
                last_message = now
            if recording is not None and not recording.running():
                break
            if plt is not None:
                if not plt.fignum_exists(fig.number):
                    user_stopped = True
                    break
                for key, trace in traces.items():
                    trace.set_data([p[0] for p in points], [p[1].get(key) if finite(p[1].get(key)) else float("nan") for p in points])
                ax.relim()
                ax.autoscale_view()
                ax.set_title(f"{peer[1] if peer else 'Waiting for measurements'} | {count} recorded results")
                fig.canvas.draw_idle()
                plt.pause(interval)
            else:
                time.sleep(interval)

    try:
        with raw_path.open("wb") as raw_capture, csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=columns)
            writer.writeheader()
            try:
                monitor(raw_capture, csv_file, writer)
            except KeyboardInterrupt:
                user_stopped = True
            finally:
                if recording is not None:
                    stop_reason = recording.stop()
                    # The recorder has closed. Preserve its complete byte stream,
                    # including results queued while Stop was being flushed.
                    while True:
                        previous = reader.offset
                        save_batch(reader.poll(), raw_capture, writer)
                        if reader.offset == previous:
                            break
            if reader.pending:
                stop_reason = "partial_line"
            elif (max_estimates is not None and filtered_count >= max_estimates
                  and stop_reason in (None, "stopped", "target_reached", "no_readings", "analyzer_ended")):
                stop_reason = "target_reached"
    finally:
        if reader.pending:
            (capture / "partial_line.bin").write_bytes(reader.pending)
        save_summary()
        if plt is not None:
            plt.close(fig)
        workbook_path = write_measurements_xlsx(csv_path, session / "measurements.xlsx")
        if workbook_path is not None:
            try:
                write_measurement_plots(workbook_path, actual_distance, conditions)
                plots_saved = True
            except ImportError:
                print("Report plots not created. Install dependencies with: python -m pip install -r support/requirements.txt", flush=True)
            except OSError as error:
                print(f"Could not save report plots in {session}: {error}", flush=True)
        save_summary()
    if plots_saved and count and count == reader.line and not reader.pending:
        recycle_duplicate_recording(reader.path, raw_path)
    print(f"Saved {count} reported results to {session}", flush=True)
    return session
