"""Check first-time board assignment without connecting or flashing hardware."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import flash


class BoardRoleTests(unittest.TestCase):
    def test_numbers_assign_and_remember_both_roles(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "board_roles.json"
            roles = {}
            for number, role in (("1", "initiator"), ("2", "reflector")):
                with self.subTest(role=role):
                    output = io.StringIO()
                    board = {"serial_number": number}
                    with (patch.object(flash, "BOARD_ROLES", path),
                          patch("builtins.input", return_value=number) as answer,
                          redirect_stdout(output)):
                        self.assertEqual(flash.board_role(board, roles), role)
                        self.assertEqual(flash.board_role(board, roles), role)
                    answer.assert_called_once()
                    self.assertIn("Choose 1 or 2", answer.call_args.args[0])
                    self.assertIn("1. Initiator\n2. Reflector", output.getvalue())
                    self.assertTrue(output.getvalue().isascii())
                    self.assertEqual(json.loads(path.read_text())[number], role)

    def test_invalid_choice_retries_and_skip_does_not_save(self):
        roles = {}
        with (patch("builtins.input", side_effect=["3", "skip"]) as answer,
              patch.object(Path, "write_text") as save,
              redirect_stdout(io.StringIO())):
            self.assertIsNone(flash.board_role({"serial_number": "123"}, roles))
        self.assertEqual(answer.call_count, 2)
        self.assertEqual(roles, {})
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
