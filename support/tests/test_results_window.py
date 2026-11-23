"""Grouping, evidence selection, filters, and workbook-lock behavior without hardware."""

import tkinter as tk
import unittest
from unittest.mock import patch

from results import ResultsWindow, visible_rows
from support.tests import test_master_results as fixtures


class ResultsWindowTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.MasterResultsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.data = fixture.data
        fixture.trial("guided/Batch1/1m_Ini0_Ref0")
        fixture.trial("guided/Batch2/1m_Ini0_Ref0", simulated=True)
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("A desktop is required for the Tk window checks")
        self.root.withdraw()
        self.addCleanup(self.close_window)
        self.app = ResultsWindow(self.root, self.data)

    def close_window(self):
        if getattr(self, "app", None) and self.app.search_job:
            self.root.after_cancel(self.app.search_job)
        self.root.destroy()

    def test_default_hides_simulations_and_groups_start_collapsed(self):
        parents = self.app.tree.get_children()
        self.assertEqual(len(parents), 1)
        self.assertFalse(self.app.tree.item(parents[0], "open"))
        self.assertIn("1 simulated", self.app.status.get())
        self.assertTrue(all(str(button["state"]) == "disabled" for button in self.app.trial_buttons))
        child = self.app.tree.get_children(parents[0])[0]
        self.app.tree.selection_set(child)
        self.app.selection_changed()
        self.assertTrue(all(str(button["state"]) == "normal" for button in self.app.trial_buttons))
        self.assertIn("Attempt log: missing", self.app.selection_note.get())

    def test_filters_use_cache_and_batch_selection_cannot_edit_a_whole_batch(self):
        with patch("results.collect_results", side_effect=AssertionError("Unexpected capture rescan")), patch(
                "results.write_workbook", side_effect=AssertionError("Unexpected workbook rebuild")):
            self.app.category.set("All trials")
            self.app.render()
            self.assertEqual(len(self.app.tree.get_children()), 2)
            self.app.query.set("Batch2")
            self.app.render()
            parents = self.app.tree.get_children()
            self.assertEqual(len(parents), 1)
            self.app.tree.selection_set(parents[0])
            self.app.selection_changed()
            self.assertIsNone(self.app.selected())
            self.assertTrue(all(str(button["state"]) == "disabled" for button in self.app.trial_buttons))

    def test_locked_excel_keeps_current_capture_list_visible(self):
        with patch("results.write_workbook", side_effect=PermissionError("Excel is open")):
            self.assertIsNone(self.app.refresh())
        self.assertEqual(len(self.app.rows), 2)
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.assertIn("Close Excel", self.app.status.get())

    def test_actions_fit_the_minimum_window_size(self):
        self.root.geometry("850x560")
        self.root.update_idletasks()
        for button in self.app.trial_buttons:
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                 self.root.winfo_rooty() + self.root.winfo_height())


if __name__ == "__main__":
    unittest.main()
