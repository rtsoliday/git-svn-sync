import contextlib
import io
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("git-svn-sync.py")
SPEC = importlib.util.spec_from_file_location("git_svn_sync", MODULE_PATH)
sync = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sync)


class CommitMessageTests(unittest.TestCase):
    def test_git_messages_since_uses_utc_iso_timestamp(self):
        calls = []

        def fake_run(cmd, cwd=None, check=True):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="new message\x1e", stderr="")

        with patch.object(sync, "run", fake_run):
            messages = sync.git_log_messages_since("/git", "path/file.txt", 0)

        self.assertEqual(messages, ["new message"])
        self.assertIn("--since=1970-01-01T00:00:01Z", calls[0])
        self.assertNotIn("--since=1", calls[0])

    def test_remove_from_git_uses_svn_deletion_message(self):
        with patch.object(sync, "prompt_yes_no", return_value=False), \
             patch.object(sync, "svn_last_change_or_deleted", return_value=(123, "deleted in svn", "svn-user")), \
             patch.object(sync, "git_last_change", side_effect=AssertionError("wrong source")):
            with contextlib.redirect_stdout(io.StringIO()):
                op = sync.handle_only_in_one("old/file.txt", "git", "/git", "/svn", False)

        self.assertEqual(
            op,
            sync.SyncOperation(
                "old/file.txt",
                "git",
                "delete",
                "deleted in svn\n\nOriginal author: svn-user",
            ),
        )

    def test_remove_from_svn_uses_git_deletion_message(self):
        with patch.object(sync, "prompt_yes_no", return_value=False), \
             patch.object(sync, "git_last_change", return_value=(456, "deleted in git", "git-user")), \
             patch.object(sync, "svn_last_change", side_effect=AssertionError("wrong source")):
            with contextlib.redirect_stdout(io.StringIO()):
                op = sync.handle_only_in_one("old/file.txt", "svn", "/git", "/svn", False)

        self.assertEqual(
            op,
            sync.SyncOperation(
                "old/file.txt",
                "svn",
                "delete",
                "deleted in git\n\nOriginal author: git-user",
            ),
        )

    def test_extract_svn_deleted_path_change_from_verbose_log(self):
        log_output = """------------------------------------------------------------------------
r12 | svn-user | 2025-01-02 03:04:05 +0000 (Thu, 02 Jan 2025) | 1 line
Changed paths:
   D /trunk/old/file.txt

deleted in svn
------------------------------------------------------------------------
"""

        self.assertEqual(
            sync.extract_svn_path_change(log_output, "/trunk/old/file.txt", {"D"}),
            (1735787045, "deleted in svn", "svn-user"),
        )

    def test_execute_git_group_batches_same_message(self):
        calls = []
        copies = []
        operations = [
            sync.SyncOperation("b.txt", "git", "copy", "shared message"),
            sync.SyncOperation("a.txt", "git", "copy", "shared message"),
        ]

        with patch.object(sync, "copy_file", side_effect=lambda src, dst, rel, dry: copies.append((src, dst, rel, dry))), \
             patch.object(sync, "run", side_effect=lambda cmd, cwd=None, check=True: calls.append((cmd, cwd, check)) or subprocess.CompletedProcess(cmd, 0, "", "")):
            with contextlib.redirect_stdout(io.StringIO()):
                sync.execute_operation_groups(operations, "/git", "/svn", False)

        self.assertEqual(
            copies,
            [("/svn", "/git", "a.txt", False), ("/svn", "/git", "b.txt", False)],
        )
        self.assertIn((["git", "add", "--", "a.txt", "b.txt"], "/git", True), calls)
        self.assertIn((["git", "commit", "-m", "shared message", "--", "a.txt", "b.txt"], "/git", True), calls)
        self.assertEqual(
            [call for call in calls if call[0][:2] == ["git", "commit"]],
            [(["git", "commit", "-m", "shared message", "--", "a.txt", "b.txt"], "/git", True)],
        )

    def test_execute_svn_group_batches_same_message(self):
        calls = []
        copies = []
        operations = [
            sync.SyncOperation("b.txt", "svn", "copy", "shared message"),
            sync.SyncOperation("a.txt", "svn", "copy", "shared message"),
        ]

        with patch.object(sync, "copy_file", side_effect=lambda src, dst, rel, dry: copies.append((src, dst, rel, dry))), \
             patch.object(sync, "run", side_effect=lambda cmd, cwd=None, check=True: calls.append((cmd, cwd, check)) or subprocess.CompletedProcess(cmd, 0, "", "")):
            with contextlib.redirect_stdout(io.StringIO()):
                sync.execute_operation_groups(operations, "/git", "/svn", False)

        self.assertEqual(
            copies,
            [("/git", "/svn", "a.txt", False), ("/git", "/svn", "b.txt", False)],
        )
        self.assertIn((["svn", "add", "--", "a.txt"], "/svn", False), calls)
        self.assertIn((["svn", "add", "--", "b.txt"], "/svn", False), calls)
        self.assertEqual(
            [call for call in calls if call[0][:2] == ["svn", "commit"]],
            [(["svn", "commit", "-m", "shared message", "--", "a.txt", "b.txt"], "/svn", True)],
        )


if __name__ == "__main__":
    unittest.main()
