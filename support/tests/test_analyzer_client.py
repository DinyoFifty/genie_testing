"""Synthetic recorder protocol checks without Analyzer or connected boards."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from support.genie_live import AnalyzerConnection, run_live


RECORDER = r'''
import json
from pathlib import Path
import sys

active = None

def emit(event):
    print(json.dumps(event), flush=True)

def finish(reason):
    global active
    if active is not None:
        emit({"event": "stopped", "path": active, "reason": reason, "estimates": 1})
        active = None

emit({"event": "connected"})
try:
    for line in sys.stdin:
        command = json.loads(line)
        if command["command"] == "start":
            if command["max_estimates"] == 13:
                emit({"event": "error", "message": "Synthetic recorder failure"})
                break
            active = command["path"]
            Path(active).write_text('{"initiatorId":"i","reflectorId":"r","distanceMain":1}\n')
            if command["max_estimates"] == 2:
                # A completed capture may arrive before Python starts following it.
                finish("target_reached")
            else:
                emit({"event": "recording", "path": active})
                if command["max_estimates"] == 1:
                    finish("target_reached")
        elif command["command"] == "stop":
            finish("stopped")
        elif command["command"] == "quit":
            break
finally:
    finish("stopped")
'''


class AnalyzerClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def connection(self):
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", RECORDER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        connection = AnalyzerConnection(self.folder, process)

        def close():
            connection.close()
            process.stdout.close()

        self.addCleanup(close)
        connection.wait_for({"connected"}, 3)
        return connection

    def test_auto_completed_then_manual_capture_reuses_process(self):
        connection = self.connection()
        first = connection.start(1)
        self.assertEqual(first.stop(), "target_reached")
        self.assertFalse(first.running())
        self.assertTrue(connection.running())
        second = connection.start(100)
        self.assertNotEqual(first.path, second.path)
        self.assertTrue(second.running())
        self.assertEqual(second.stop(), "stopped")
        self.assertEqual(second.stop(), "stopped")
        self.assertTrue(connection.running())

    def test_stopped_event_before_recording_is_accepted(self):
        connection = self.connection()
        capture = connection.start(2)
        self.assertEqual(capture.reason, "target_reached")
        self.assertTrue(capture.path.is_file())

    def test_close_finishes_active_capture_and_joins_reader(self):
        connection = self.connection()
        capture = connection.start()
        connection.close()
        self.assertEqual(capture.reason, "stopped")
        self.assertFalse(connection.running())
        self.assertFalse(connection.reader.is_alive())

    def test_recorder_error_reaches_start_caller(self):
        connection = self.connection()
        with self.assertRaisesRegex(ValueError, "Synthetic recorder failure"):
            connection.start(13)
        self.assertFalse(connection.running())

    def test_start_rejects_nonpositive_or_noninteger_target_before_sending(self):
        connection = self.connection()
        for target in (-1, 0, True, 1.5, "100"):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    connection.start(target)
        self.assertIsNone(connection.capture)
        self.assertTrue(connection.running())

    def test_wait_ignores_recording_event_for_another_capture_path(self):
        connection = self.connection()
        wanted = self.folder / "wanted.jsonl"
        connection.events.put({"event": "recording", "path": str(self.folder / "previous.jsonl")})
        connection.events.put({"event": "recording", "path": str(wanted)})
        event = connection.wait_for({"recording"}, 1, path=wanted)
        self.assertEqual(event["path"], str(wanted))

    def test_control_stream_exit_fails_without_waiting_for_process_exit(self):
        connection = self.connection()
        connection.events.put({"event": "exited"})
        with self.assertRaisesRegex(ValueError, "control connection"):
            connection.wait_for({"recording"}, 1)
        self.assertIsNone(connection.process.poll())

    def test_cancel_during_start_still_allows_clean_session_shutdown(self):
        connection = self.connection()
        with patch.object(connection, "wait_for", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                connection.start()
        connection.close()
        self.assertEqual(connection.capture.reason, "stopped")
        self.assertFalse(connection.running())

    def test_first_100_finite_filtered_estimates_keep_full_raw_flush(self):
        path = self.folder / "record-cap.jsonl"
        event = {"initiatorId": "synthetic-i", "reflectorId": "synthetic-r", "distanceMainRaw": 1.0}
        values = [None, True, *[float(value) for value in range(120)]]
        initial = b"".join((json.dumps({**event, "distanceMain": value}) + "\n").encode() for value in values)
        tail = b"".join((json.dumps({**event, "distanceMain": 999.0}) + "\n").encode() for _ in range(5))
        path.write_bytes(initial)

        class Capture:
            def __init__(self):
                self.path = path
                self.stopped = False

            def running(self):
                return not self.stopped

            def stop(self):
                if not self.stopped:
                    with self.path.open("ab") as stream:
                        stream.write(tail)
                    self.stopped = True
                return "target_reached"

        with (
            patch("support.genie_live.write_measurements_xlsx", return_value=None),
            patch("support.genie_live.recycle_duplicate_recording"),
        ):
            session = run_live(
                recording=Capture(), out=self.folder / "live", plot=False,
                interval=0.001, actual_distance=1.0, max_estimates=100,
            )
        self.assertEqual((session / "capture" / "raw.jsonl").read_bytes(), initial + tail)
        summary = json.loads((session / "capture" / "summary.json").read_text())
        self.assertEqual(summary["records"], 127)
        self.assertEqual(summary["finite_filtered_estimate_count"], 100)
        self.assertEqual(summary["additional_recorded_results"], 25)
        self.assertTrue(summary["target_reached"])
        with (session / "capture" / "measurements.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[-1]["distanceMain"], "99.0")

    def test_nonfinite_only_batch_and_target_crossing_during_final_flush(self):
        path = self.folder / "record-flush.jsonl"
        event = {"initiatorId": "synthetic-i", "reflectorId": "synthetic-r", "distanceMainRaw": 1.0}
        initial = b"".join((json.dumps({**event, "distanceMain": value}) + "\n").encode() for value in [None, True, "1"])
        tail = b"".join((json.dumps({**event, "distanceMain": float(value)}) + "\n").encode() for value in range(105))
        path.write_bytes(initial)

        class Capture:
            def __init__(self):
                self.path = path

            def running(self):
                return False

            def stop(self):
                with self.path.open("ab") as stream:
                    stream.write(tail)
                return "stopped"

        with patch("support.genie_live.write_measurements_xlsx", return_value=None):
            session = run_live(
                recording=Capture(), out=self.folder / "live", plot=False,
                interval=0.001, actual_distance=1.0, max_estimates=100,
            )
        self.assertEqual((session / "capture" / "raw.jsonl").read_bytes(), initial + tail)
        summary = json.loads((session / "capture" / "summary.json").read_text())
        self.assertEqual(summary["records"], 108)
        self.assertEqual(summary["finite_filtered_estimate_count"], 100)
        self.assertEqual(summary["additional_recorded_results"], 5)
        self.assertEqual(summary["stop_reason"], "target_reached")
        with (session / "capture" / "measurements.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]["source_line"], "4")
        self.assertEqual(rows[-1]["distanceMain"], "99.0")


if __name__ == "__main__":
    unittest.main()
