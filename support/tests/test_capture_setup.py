"""Saved setup stays distinct from connected-device and instrument evidence."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from support.capture_setup import print_setup_summary, setup_lines
from support.master_results import save_context


class CaptureSetupTests(unittest.TestCase):
    def test_zero_offsets_and_angles_are_shown_and_missing_values_stay_unknown(self):
        snapshot = {"mode": "isolated", "projects": {
            role: {"physical_antenna": 1, "wireless_offsets_cm": [0, 60, 0, 0]}
            for role in ("initiator", "reflector")}}
        text = "\n".join(setup_lines(snapshot, 1, {"initiator_yaw_deg": 0, "reflector_yaw_deg": 315}))
        self.assertIn("ANT1 0 cm", text)
        self.assertIn("Initiator angle: 0 deg", text)
        self.assertIn("Reflector angle: 315 deg", text)
        self.assertIn("Location: unknown", text)
        self.assertIn("not flashed-firmware or TX-power readback", text)

    def test_resumed_batch_does_not_borrow_changed_next_test_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            save_context({"environment": "new garage setup"}, data)
            output = io.StringIO()
            with redirect_stdout(output):
                print_setup_summary({"mode": "normal"}, 2, {}, data=data, use_defaults=False,
                                    profile="energy", board_pair=["initiator-id", "reflector-id"])
            self.assertIn("Location: unknown", output.getvalue())
            self.assertNotIn("new garage setup", output.getvalue())
            self.assertIn("Last capture board IDs", output.getvalue())
            self.assertIn("Energy scheduling comparison", output.getvalue())


if __name__ == "__main__":
    unittest.main()
