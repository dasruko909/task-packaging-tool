"""API history report counts only saved request events."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from task_packer.models import ProjectConfig, Subtask
from task_packer.verification import Verification


class ApiHistoryRecordTests(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.temp = tempfile.TemporaryDirectory()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.previous)
        self.addCleanup(self.temp.cleanup)
        package = Path("output/demo")
        package.mkdir(parents=True)
        self.verification = Verification(ProjectConfig(
            codename="demo", title="Demo", origin="test", language_code="en",
            original_statement="Synthetic", subtasks=[Subtask(1, "Full", 100, "")],
        ))
        self.history = Path(".packer-projects/demo/history/api-calls")
        self.history.mkdir(parents=True)

    def write_event(self, name, **fields):
        event = {
            "model": "gpt-test", "started_at": 1.5,
            "max_output_tokens": 100, "status": "completed",
            **fields,
        }
        (self.history / name).write_text(json.dumps(event), encoding="utf-8")

    def test_report_counts_only_valid_completed_failed_and_empty_events(self):
        self.write_event("completed.txt")
        self.write_event("failed.txt", status="failed", model="gpt-failed")
        self.write_event("empty.txt", status="empty", model="gpt-empty")
        (self.history / "response-dump.txt").write_text("plain response", encoding="utf-8")
        (self.history / "malformed.txt").write_text("{broken", encoding="utf-8")
        (self.history / "array.txt").write_text("[]", encoding="utf-8")
        self.write_event("started.txt", status="started")
        self.write_event("missing-model.txt", model=None)
        self.write_event("bad-status.txt", status=["completed"])
        self.write_event("bad-timestamp.txt", started_at="yesterday")
        self.write_event("bad-token-limit.txt", max_output_tokens="100")

        details = self.verification._ai_details()

        self.assertEqual(details["recorded_calls"], 3)
        self.assertEqual(details["completed_calls"], 1)
        self.assertEqual(details["failed_calls"], 2)
        self.assertEqual(details["models"], ["gpt-empty", "gpt-failed", "gpt-test"])

    def test_symlink_history_path_is_rejected(self):
        outside = Path("outside")
        outside.mkdir()
        self.history.rmdir()
        self.history.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "Symbolic link is forbidden"):
            self.verification._api_history()

    def test_symlink_event_file_is_rejected(self):
        outside = Path("outside.txt")
        outside.write_text("{}", encoding="utf-8")
        (self.history / "linked.txt").symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "Symbolic link is forbidden"):
            self.verification._api_history()


if __name__ == "__main__":
    unittest.main()
