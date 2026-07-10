import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest.mock import patch


import git_svn_sync as sync


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

    def test_mismatches_group_when_full_git_history_messages_match(self):
        statuses = [
            sync.FileStatus(
                "a.txt",
                True,
                True,
                False,
                200,
                "latest git commit",
                "git-user",
                100,
                "old svn message for a",
                "svn-user",
            ),
            sync.FileStatus(
                "b.txt",
                True,
                True,
                False,
                200,
                "latest git commit",
                "git-user",
                50,
                "old svn message for b",
                "svn-user",
            ),
        ]

        history_cutoffs = []

        def git_history(_root, _relpath, since_ts):
            history_cutoffs.append(since_ts)
            return ["latest git commit"]

        with patch.object(sync, "prompt_yes_no", return_value=True), \
             patch.object(sync, "git_log_messages_since", side_effect=git_history):
            with contextlib.redirect_stdout(io.StringIO()):
                operations = [
                    sync.handle_mismatch(status, "/git", "/svn", False)
                    for status in statuses
                ]

        self.assertEqual(history_cutoffs, [100, 50])
        self.assertEqual(
            operations,
            [
                sync.SyncOperation(
                    "a.txt",
                    "svn",
                    "copy",
                    "latest git commit\n\nOriginal author: git-user",
                ),
                sync.SyncOperation(
                    "b.txt",
                    "svn",
                    "copy",
                    "latest git commit\n\nOriginal author: git-user",
                ),
            ],
        )
        groups = sync.grouped_operations(op for op in operations if op is not None)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0], ("svn", "latest git commit\n\nOriginal author: git-user"))
        self.assertEqual({op.relpath for op in groups[0][1]}, {"a.txt", "b.txt"})

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
        self.assertIn(
            (
                [
                    "svn", "add", "--parents", "--force", "--",
                    "a.txt", "b.txt",
                ],
                "/svn",
                True,
            ),
            calls,
        )
        self.assertEqual(
            [call for call in calls if call[0][:2] == ["svn", "commit"]],
            [(["svn", "commit", "-m", "shared message", "--", "a.txt", "b.txt"], "/svn", True)],
        )

    def test_run_reports_command_event_with_output_and_dry_run_status(self):
        reporter = sync.CollectingReporter()

        with sync.workflow_context(reporter, True):
            cp = sync.run([sys.executable, "-c", "print('hello')"])

        self.assertEqual(cp.returncode, 0)
        self.assertEqual(len(reporter.commands), 1)
        event = reporter.commands[0]
        self.assertEqual(event.cmd[:2], (sys.executable, "-c"))
        self.assertEqual(event.stdout, "hello\n")
        self.assertEqual(event.stderr, "")
        self.assertEqual(event.returncode, 0)
        self.assertTrue(event.dry_run)
        self.assertFalse(event.planned)

    def test_gui_askpass_context_detaches_terminal_and_sets_helpers(self):
        completed = subprocess.CompletedProcess(["git", "fetch"], 0, "", "")

        with patch.object(sync.subprocess, "run", return_value=completed) as run_process:
            with sync.gui_askpass_context():
                sync.run(["git", "fetch"], cwd="/git")

        kwargs = run_process.call_args.kwargs
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"]["GIT_ASKPASS"], os.path.abspath(sync.__file__))
        self.assertEqual(kwargs["env"]["SSH_ASKPASS"], os.path.abspath(sync.__file__))
        self.assertEqual(kwargs["env"]["SSH_ASKPASS_REQUIRE"], "force")
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_cli_run_keeps_normal_terminal_authentication(self):
        completed = subprocess.CompletedProcess(["git", "fetch"], 0, "", "")

        with patch.object(sync.subprocess, "run", return_value=completed) as run_process:
            sync.run(["git", "fetch"], cwd="/git")

        kwargs = run_process.call_args.kwargs
        self.assertIsNone(kwargs["stdin"])
        self.assertIsNone(kwargs["env"])

    def test_askpass_invocation_routes_prompt_to_dialog(self):
        with patch.dict(os.environ, {sync.ASKPASS_ENV: "1"}), \
             patch.object(sync, "show_askpass_dialog", return_value=7) as dialog:
            result = sync.main(["Password for repository:"])

        self.assertEqual(result, 7)
        dialog.assert_called_once_with("Password for repository:")

    def test_dry_run_execute_git_group_emits_planned_events_without_running_commands(self):
        reporter = sync.CollectingReporter()
        operations = [
            sync.SyncOperation("copy.txt", "git", "copy", "shared message"),
            sync.SyncOperation("delete.txt", "git", "delete", "shared message"),
        ]

        with patch.object(sync, "run", side_effect=AssertionError("dry-run should not execute write commands")):
            with sync.workflow_context(reporter, True):
                sync.execute_operation_groups(operations, "/git", "/svn", True)

        planned = [event.cmd for event in reporter.commands if event.planned]
        self.assertIn(("git", "add", "--", "copy.txt"), planned)
        self.assertIn(("git", "rm", "--", "delete.txt"), planned)
        self.assertIn(("git", "commit", "-m", "shared message", "--", "copy.txt", "delete.txt"), planned)
        self.assertIn(("git", "push", "origin", "master"), planned)
        self.assertTrue(all(event.dry_run for event in reporter.commands))
        self.assertTrue(all(event.planned for event in reporter.commands))
        self.assertTrue(all(event.dry_run for event in reporter.file_operations))
        self.assertTrue(all(event.planned for event in reporter.file_operations))

    def test_git_dry_run_up_to_date_check_skips_fetch(self):
        calls = []
        reporter = sync.CollectingReporter()

        def fake_run(cmd, cwd=None, check=True, reporter=None, dry_run=None):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="same\n", stderr="")

        with patch.object(sync, "run", fake_run):
            with sync.workflow_context(reporter, True):
                self.assertTrue(sync.git_is_up_to_date("/git", refresh=False))

        self.assertNotIn(["git", "fetch"], calls)
        self.assertIn(["git", "rev-parse", "HEAD"], calls)
        self.assertIn(["git", "rev-parse", "@{u}"], calls)
        self.assertTrue(
            any("Skipping git fetch" in message for _stream, message in reporter.messages)
        )

    def test_rebaseline_dry_run_previews_without_writing_ignore_file(self):
        reporter = sync.CollectingReporter()
        plan = sync.SyncPlan(
            sync.SyncConfig("/git", "/svn", dry_run=True, rebaseline=True),
            None,
            0,
            0,
            (),
            (),
            (),
            (),
            (),
            ("/git/new.txt",),
            (),
        )

        with patch.object(sync, "append_to_ignore", side_effect=AssertionError("dry-run should not write ignore file")):
            added = sync.apply_rebaseline_plan(plan, reporter)

        self.assertEqual(added, ["/git/new.txt"])
        self.assertTrue(
            any("would add /git/new.txt" in message for _stream, message in reporter.messages)
        )

    def test_safe_svn_update_runs_update_when_status_is_clean(self):
        calls = []
        reporter = sync.CollectingReporter()

        def fake_run(cmd, cwd=None, check=True, reporter=None, dry_run=None):
            calls.append((cmd, cwd))
            if cmd == ["svn", "status"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd == ["svn", "update"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="Updated to revision 10.\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(sync, "run", fake_run):
            sync.safe_svn_update("/svn", reporter)

        self.assertEqual(
            calls,
            [
                (["svn", "info"], "/svn"),
                (["svn", "status"], "/svn"),
                (["svn", "update"], "/svn"),
            ],
        )
        self.assertTrue(
            any("SVN update complete" in message for _stream, message in reporter.messages)
        )

    def test_safe_svn_update_refuses_when_status_has_local_changes(self):
        calls = []
        reporter = sync.CollectingReporter()

        def fake_run(cmd, cwd=None, check=True, reporter=None, dry_run=None):
            calls.append((cmd, cwd))
            if cmd == ["svn", "status"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="M       changed.txt\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(sync, "run", fake_run):
            with self.assertRaises(sync.SyncError) as cm:
                sync.safe_svn_update("/svn", reporter)

        self.assertNotIn((["svn", "update"], "/svn"), calls)
        self.assertIn("changed.txt", str(cm.exception))
        self.assertTrue(
            any("Refusing to run svn update" in message for _stream, message in reporter.messages)
        )

    def test_safe_svn_update_ignores_untracked_status_lines(self):
        calls = []
        reporter = sync.CollectingReporter()

        def fake_run(cmd, cwd=None, check=True, reporter=None, dry_run=None):
            calls.append((cmd, cwd))
            if cmd == ["svn", "status"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="?       .codex\n?       bin\n?       build/O.Linux-x86_64\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(sync, "run", fake_run):
            sync.safe_svn_update("/svn", reporter)

        self.assertIn((["svn", "update"], "/svn"), calls)
        self.assertTrue(
            any("SVN update complete" in message for _stream, message in reporter.messages)
        )

    def test_safe_svn_update_ignores_external_status_lines(self):
        calls = []
        reporter = sync.CollectingReporter()

        def fake_run(cmd, cwd=None, check=True, reporter=None, dry_run=None):
            calls.append((cmd, cwd))
            if cmd == ["svn", "status"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="X       external-lib\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(sync, "run", fake_run):
            sync.safe_svn_update("/svn", reporter)

        self.assertIn((["svn", "update"], "/svn"), calls)

    def test_gui_row_model_selects_default_and_alternate_operations(self):
        gui = sync

        status = sync.FileStatus(
            "new.txt",
            True,
            False,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        add_op = sync.SyncOperation(
            "new.txt",
            "svn",
            "copy",
            "Add file\n\nOriginal author: user",
        )
        remove_op = sync.SyncOperation("new.txt", "git", "delete", "Remove file")
        item = sync.PlanItem("new.txt", "only_git", status, add_op, remove_op)
        row = gui.GuiPlanRow(item)

        self.assertEqual(gui.row_values(row)[2], "GIT → SVN")
        self.assertEqual(gui.row_values(row)[4], "user")
        self.assertEqual(gui.selected_operations([row]), [add_op])

        row.use_alternate = True
        self.assertEqual(gui.row_values(row)[2], "Delete from GIT")
        self.assertEqual(gui.selected_operations([row]), [remove_op])

        row.selected = False
        self.assertEqual(gui.selected_operations([row]), [])

    def test_gui_routes_svn_update_error_to_custom_dialog(self):
        gui = sync

        class FakeStatusVar:
            def __init__(self):
                self.values = []

            def set(self, value):
                self.values.append(value)

        app = gui.GitSvnSyncApp.__new__(gui.GitSvnSyncApp)
        app.busy = True
        app.status_var = FakeStatusVar()
        logged = []
        dialogs = []
        app._append_log = lambda text: logged.append(text)
        app._show_svn_update_error = lambda message: dialogs.append(message)

        with patch.object(gui.messagebox, "showerror", side_effect=AssertionError("should use custom dialog")):
            app._handle_event((
                "error",
                "Error: SVN working copy is not up to date.\nPlease run 'svn update' before running this script.",
            ))

        self.assertFalse(app.busy)
        self.assertEqual(app.status_var.values, ["Error"])
        self.assertEqual(len(dialogs), 1)
        self.assertIn("svn update", dialogs[0])
        self.assertTrue(any("ERROR:" in text for text in logged))

    def test_gui_routes_other_errors_to_standard_dialog(self):
        gui = sync

        class FakeStatusVar:
            def __init__(self):
                self.values = []

            def set(self, value):
                self.values.append(value)

        app = gui.GitSvnSyncApp.__new__(gui.GitSvnSyncApp)
        app.busy = True
        app.status_var = FakeStatusVar()
        logged = []
        shown = []
        app._append_log = lambda text: logged.append(text)
        app._show_svn_update_error = lambda message: (_ for _ in ()).throw(
            AssertionError("should not use custom dialog")
        )

        with patch.object(gui.messagebox, "showerror", side_effect=lambda title, message: shown.append((title, message))):
            app._handle_event(("error", "plain failure"))

        self.assertFalse(app.busy)
        self.assertEqual(app.status_var.values, ["Error"])
        self.assertEqual(shown, [("git-svn-sync", "plain failure")])
        self.assertTrue(any("ERROR:" in text for text in logged))

    def test_main_without_arguments_launches_gui(self):
        launched = []

        with patch.object(sync, "launch_gui", side_effect=lambda: launched.append(True)):
            sync.main([])

        self.assertEqual(launched, [True])

    def test_main_with_help_stays_in_cli_parser(self):
        with patch.object(sync, "launch_gui", side_effect=AssertionError("help should not launch GUI")):
            with self.assertRaises(SystemExit) as cm:
                with contextlib.redirect_stdout(io.StringIO()):
                    sync.main(["-h"])

        self.assertEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
