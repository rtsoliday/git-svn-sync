#!/usr/bin/env python3
"""
git_svn_sync.py

Sync files between a Git working copy and an SVN working copy.

Requirements:
  - `git` and `svn` CLIs installed and on PATH
  - Two local working copies with (mostly) mirrored directory layout:
      * GIT_WC: path to Git working copy root
      * SVN_WC: path to SVN working copy root
  - Python 3.8+

What it does:
  1) Builds the sets of versioned files:
       - Git: `git ls-files`
       - SVN: `svn list -R` (from working copy)
  2) Compares file contents (SHA-256) for intersection, and finds files present only in one repo.
  3) For mismatched files:
       - Determines which repo has the most recent change and fetches its last commit message and author.
       - Prompts to copy newer -> older and commit using the same message with author noted.
  4) For files present in only one repo:
       - Prompts to add to the other repo (default) or remove from the current repo, and commits.
  5) After committing to Git, pushes the change to `origin master` so the remote trunk stays updated.

Safety:
  - Only acts on files tracked by each VCS.
    - Per-file confirmation unless -yes is given.
    - Supports -dry-run.
    - Paths listed in `~/.git-svn-sync.ignore` (absolute paths) are skipped. The file must contain entries for both
      working copies; run with -rebaseline to (re)populate it.
    - Files under any `.kilo` directory are always skipped.
  - Verifies both working copies are up to date with their remotes before running.

  Usage:
    python git_svn_sync.py -git /path/to/git_wc -svn /path/to/svn_wc [-yes] [-dry-run] [-rebaseline]
"""

import argparse
import dataclasses
import hashlib
import datetime
import contextlib
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

# Path to ignore file containing newline-separated absolute paths to ignore
IGNORE_FILE = os.path.expanduser("~/.git-svn-sync.ignore")
ALWAYS_IGNORED_DIRS = {".kilo"}


@dataclass(frozen=True)
class CommandEvent:
    cmd: Tuple[str, ...]
    cwd: Optional[str]
    stdout: str
    stderr: str
    returncode: Optional[int]
    dry_run: bool = False
    planned: bool = False


@dataclass(frozen=True)
class FileOperationEvent:
    action: str
    relpath: str
    source: Optional[str]
    destination: Optional[str]
    dry_run: bool = False
    planned: bool = False


class CommandReporter:
    """Receives workflow messages and command/file-operation events."""

    def message(self, text: str, stream: str = "stdout") -> None:
        pass

    def command(self, event: CommandEvent) -> None:
        pass

    def file_operation(self, event: FileOperationEvent) -> None:
        pass


class ConsoleReporter(CommandReporter):
    def __init__(self, show_commands: bool = False):
        self.show_commands = show_commands

    def message(self, text: str, stream: str = "stdout") -> None:
        print(text, file=sys.stderr if stream == "stderr" else sys.stdout)

    def command(self, event: CommandEvent) -> None:
        if not self.show_commands:
            return
        prefix = "[dry-run] " if event.dry_run else ""
        if event.planned:
            prefix += "planned "
        cwd = f" (cwd: {event.cwd})" if event.cwd else ""
        self.message(f"{prefix}$ {format_command(event.cmd)}{cwd}")
        if event.stdout:
            self.message(event.stdout.rstrip())
        if event.stderr:
            self.message(event.stderr.rstrip(), stream="stderr")
        if event.returncode is not None:
            self.message(f"exit code: {event.returncode}")

    def file_operation(self, event: FileOperationEvent) -> None:
        if not self.show_commands:
            return
        prefix = "[dry-run] " if event.dry_run else ""
        if event.planned:
            prefix += "planned "
        if event.source and event.destination:
            self.message(
                f"{prefix}{event.action} {event.source} -> {event.destination}"
            )
        else:
            self.message(f"{prefix}{event.action} {event.relpath}")


class CollectingReporter(CommandReporter):
    """Small reporter useful for tests and non-UI integrations."""

    def __init__(self):
        self.messages: List[Tuple[str, str]] = []
        self.commands: List[CommandEvent] = []
        self.file_operations: List[FileOperationEvent] = []

    def message(self, text: str, stream: str = "stdout") -> None:
        self.messages.append((stream, text))

    def command(self, event: CommandEvent) -> None:
        self.commands.append(event)

    def file_operation(self, event: FileOperationEvent) -> None:
        self.file_operations.append(event)


class SyncError(RuntimeError):
    pass


_ACTIVE_REPORTER: Optional[CommandReporter] = None
_ACTIVE_DRY_RUN = False
_GUI_ASKPASS_ACTIVE = False
ASKPASS_ENV = "GIT_SVN_SYNC_ASKPASS"


@contextlib.contextmanager
def workflow_context(
    reporter: Optional[CommandReporter],
    dry_run: bool,
) -> Iterator[None]:
    global _ACTIVE_REPORTER, _ACTIVE_DRY_RUN
    previous_reporter = _ACTIVE_REPORTER
    previous_dry_run = _ACTIVE_DRY_RUN
    _ACTIVE_REPORTER = reporter
    _ACTIVE_DRY_RUN = dry_run
    try:
        yield
    finally:
        _ACTIVE_REPORTER = previous_reporter
        _ACTIVE_DRY_RUN = previous_dry_run


def active_reporter(reporter: Optional[CommandReporter] = None) -> Optional[CommandReporter]:
    return reporter if reporter is not None else _ACTIVE_REPORTER


def active_dry_run(dry_run: Optional[bool] = None) -> bool:
    return _ACTIVE_DRY_RUN if dry_run is None else dry_run


@contextlib.contextmanager
def gui_askpass_context() -> Iterator[None]:
    """Route Git/SSH authentication prompts to a GUI-owned dialog."""
    global _GUI_ASKPASS_ACTIVE
    previous = _GUI_ASKPASS_ACTIVE
    _GUI_ASKPASS_ACTIVE = True
    try:
        yield
    finally:
        _GUI_ASKPASS_ACTIVE = previous


def askpass_environment() -> Dict[str, str]:
    """Return an environment that makes this script the Git/SSH askpass app."""
    env = os.environ.copy()
    script = os.path.abspath(__file__)
    env.update({
        ASKPASS_ENV: "1",
        "GIT_ASKPASS": script,
        "GIT_TERMINAL_PROMPT": "0",
        "SSH_ASKPASS": script,
        "SSH_ASKPASS_REQUIRE": "force",
    })
    # OpenSSH requires DISPLAY to be present even when the askpass program is
    # a native Tk window. macOS GUI applications commonly have no DISPLAY.
    env.setdefault("DISPLAY", ":0")
    return env


def show_askpass_dialog(prompt: str) -> int:
    """Display a foreground password prompt and print its answer for askpass."""
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    root.wm_attributes("-topmost", True)
    root.update_idletasks()
    answer = simpledialog.askstring(
        "Authentication Required",
        prompt or "Password:",
        show="*",
        parent=root,
    )
    root.destroy()
    if answer is None:
        return 1
    print(answer)
    return 0


def emit_message(
    text: str,
    reporter: Optional[CommandReporter] = None,
    stream: str = "stdout",
) -> None:
    resolved = active_reporter(reporter)
    if resolved is None:
        print(text, file=sys.stderr if stream == "stderr" else sys.stdout)
    else:
        resolved.message(text, stream=stream)


def emit_command_event(
    cmd: Iterable[str],
    cwd: Optional[str],
    stdout: str = "",
    stderr: str = "",
    returncode: Optional[int] = None,
    dry_run: Optional[bool] = None,
    planned: bool = False,
    reporter: Optional[CommandReporter] = None,
) -> None:
    resolved = active_reporter(reporter)
    if resolved is not None:
        resolved.command(
            CommandEvent(
                tuple(cmd),
                cwd,
                stdout,
                stderr,
                returncode,
                dry_run=active_dry_run(dry_run),
                planned=planned,
            )
        )


def emit_file_operation(
    action: str,
    relpath: str,
    source: Optional[str],
    destination: Optional[str],
    dry_run: Optional[bool] = None,
    planned: bool = False,
    reporter: Optional[CommandReporter] = None,
) -> None:
    resolved = active_reporter(reporter)
    if resolved is not None:
        resolved.file_operation(
            FileOperationEvent(
                action,
                relpath,
                source,
                destination,
                dry_run=active_dry_run(dry_run),
                planned=planned,
            )
        )


def format_command(cmd: Iterable[str]) -> str:
    return shlex.join(str(part) for part in cmd)

def is_always_ignored_relpath(relpath: str) -> bool:
    """Return True for relative paths excluded from every sync run."""
    parts = relpath.replace("\\", "/").split("/")
    return any(part in ALWAYS_IGNORED_DIRS for part in parts)

def load_ignore_set() -> Set[str]:
    """Return the set of absolute paths listed in the ignore file (if it exists)."""
    try:
        with open(IGNORE_FILE, "r") as f:
            return {line.strip() for line in f if line.strip() and not line.strip().startswith("#")}
    except FileNotFoundError:
        return set()

def append_to_ignore(paths: Iterable[str], existing: Optional[Set[str]] = None) -> List[str]:
    """Append the given absolute paths to the ignore file if not already present.

    Returns the list of newly added paths.
    """
    if existing is None:
        existing = load_ignore_set()
    new = [p for p in paths if p not in existing]
    if not new:
        return []
    with open(IGNORE_FILE, "a") as f:
        for p in new:
            f.write(p + "\n")
    return new

# ----- Utilities -----

def run(
    cmd: List[str],
    cwd: Optional[str] = None,
    check: bool = True,
    reporter: Optional[CommandReporter] = None,
    dry_run: Optional[bool] = None,
) -> subprocess.CompletedProcess:
    """Run a command and return the CompletedProcess. Raises on error if check=True."""
    try:
        cp = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL if _GUI_ASKPASS_ACTIVE else None,
            env=askpass_environment() if _GUI_ASKPASS_ACTIVE else None,
            check=False,
        )
    except FileNotFoundError as exc:
        emit_command_event(
            cmd,
            cwd,
            stderr=str(exc),
            returncode=None,
            dry_run=dry_run,
            reporter=reporter,
        )
        raise

    emit_command_event(
        cmd,
        cwd,
        stdout=cp.stdout,
        stderr=cp.stderr,
        returncode=cp.returncode,
        dry_run=dry_run,
        reporter=reporter,
    )
    if check and cp.returncode != 0:
        raise subprocess.CalledProcessError(
            cp.returncode,
            cmd,
            output=cp.stdout,
            stderr=cp.stderr,
        )
    return cp

def utc_iso_from_epoch(epoch: int) -> str:
    """Return a UTC ISO-8601 timestamp suitable for Git/SVN date filters."""
    dt = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def tracked_path_signature(path: str) -> Optional[Tuple[str, str]]:
    """
    Return a comparable signature for a tracked path.

    Regular files are compared by content hash. Symlinks are compared by their
    link target string so broken-but-identical symlinks are treated as equal.
    """
    if os.path.islink(path):
        return ("symlink", os.readlink(path))
    if os.path.isfile(path):
        return ("file", sha256_file(path))
    if os.path.lexists(path):
        return ("other", "")
    return None

def ensure_parent_dir(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)

def prompt_yes_no(question: str, default_yes: bool = True, auto_yes: bool = False) -> bool:
    if auto_yes:
        return True
    default = "Y/n" if default_yes else "y/N"
    while True:
        resp = input(f"{question} [{default}]: ").strip().lower()
        if not resp:
            return default_yes
        if resp in ("y", "yes"):
            return True
        if resp in ("n", "no"):
            return False
        print("Please answer y or n.")

# ----- Git helpers -----

def git_ls_files(git_root: str) -> Set[str]:
    cp = run(["git", "ls-files"], cwd=git_root)
    files = {line.strip() for line in cp.stdout.splitlines() if line.strip()}
    return files

def git_last_change(
    git_root: str, relpath: str
) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """
    Return (timestamp_epoch, message, author) for the last commit that touched relpath.
    Returns (None, None, None) if file has no history (e.g., not tracked).
    """
    try:
        info = run(
            ["git", "log", "-1", "--format=%ct%n%an", "--", relpath], cwd=git_root
        ).stdout.splitlines()
        msg = run(
            ["git", "log", "-1", "--pretty=%B", "--", relpath], cwd=git_root
        ).stdout.strip()
        if not info:
            return None, None, None
        t = info[0].strip()
        author = info[1].strip() if len(info) > 1 else None
        if not t:
            return None, None, None
        return int(t), msg, author
    except subprocess.CalledProcessError:
        return None, None, None

def git_log_messages_since(git_root: str, relpath: str, since_ts: Optional[int]) -> List[str]:
    """Return commit messages for relpath since the given timestamp (exclusive).

    Messages are returned oldest-first. If since_ts is None, all messages are
    returned.
    """
    try:
        cmd = ["git", "log", "--format=%B%x1e", "--reverse"]
        if since_ts is not None and since_ts >= 0:
            cmd.insert(2, f"--since={utc_iso_from_epoch(since_ts + 1)}")
        cmd.extend(["--", relpath])
        cp = run(cmd, cwd=git_root)
        raw = cp.stdout.split("\x1e")
        return [m.strip() for m in raw if m.strip()]
    except subprocess.CalledProcessError:
        return []

def git_is_up_to_date(git_root: str, refresh: bool = True) -> bool:
    """Return True if the Git working copy is up to date with its upstream."""
    try:
        if refresh:
            run(["git", "fetch"], cwd=git_root)
        else:
            emit_message(
                "[dry-run] Skipping git fetch; remote freshness was not refreshed."
            )
        local = run(["git", "rev-parse", "HEAD"], cwd=git_root).stdout.strip()
        remote = run(["git", "rev-parse", "@{u}"], cwd=git_root).stdout.strip()
        return local == remote
    except subprocess.CalledProcessError:
        return False

def git_uncommitted_files(git_root: str) -> Set[str]:
    """Return set of files with uncommitted changes in the Git working copy."""
    cp = run(["git", "status", "--porcelain"], cwd=git_root)
    files: Set[str] = set()
    for line in cp.stdout.splitlines():
        if not line or line.startswith("??"):
            continue  # ignore untracked files
        status = line[:2]
        if status.strip():
            files.add(line[3:])
    return files


def git_local_status_entries(git_root: str) -> List[str]:
    """Return tracked Git status lines that should block an automatic pull."""
    cp = run(["git", "status", "--porcelain"], cwd=git_root)
    return [
        line
        for line in cp.stdout.splitlines()
        if line.strip() and not line.startswith("??") and line[:2].strip()
    ]


def resolve_git_root(config: "SyncConfig") -> str:
    preset = PRESETS.get(config.preset_name or "")
    if config.preset_name and preset is None:
        raise ValueError(f"Unknown preset: {config.preset_name}")
    git_root_raw = preset.git_root if preset else config.git_root
    if not git_root_raw:
        raise ValueError("Must specify a Git path or a preset")
    return os.path.abspath(os.path.expanduser(git_root_raw))


def safe_git_update(
    git_root: str,
    reporter: Optional[CommandReporter] = None,
) -> None:
    """Fast-forward pull only when Git has no tracked working-copy changes."""
    with workflow_context(reporter, False):
        try:
            run(["git", "rev-parse", "--is-inside-work-tree"], cwd=git_root)
        except subprocess.CalledProcessError as e:
            raise SyncError(f"Error: Git probe failed in {git_root}:\n{e.stderr}") from e
        except FileNotFoundError as e:
            raise SyncError("Error: Required tool for Git not found on PATH.") from e

        local_entries = git_local_status_entries(git_root)
        if local_entries:
            emit_message("Refusing to update Git because tracked local changes were found:")
            for line in local_entries:
                emit_message(f"  {line}")
            raise SyncError(
                "Refusing to update Git because tracked local changes were found:\n"
                + "\n".join(f"  {line}" for line in local_entries)
            )

        emit_message("No tracked local Git changes detected. Running fast-forward-only pull...")
        run(["git", "pull", "--ff-only"], cwd=git_root)
        emit_message("Git update complete.")


def safe_git_update_from_config(
    config: "SyncConfig",
    reporter: Optional[CommandReporter] = None,
) -> None:
    safe_git_update(resolve_git_root(config), reporter=reporter)

# ----- SVN helpers -----

def svn_is_up_to_date(svn_root: str) -> bool:
    """Return True if the SVN working copy is up to date with the repository."""
    try:
        local = run(["svn", "info", "--show-item", "revision"], cwd=svn_root).stdout.strip()
        remote = run(["svn", "info", "-r", "HEAD", "--show-item", "revision"], cwd=svn_root).stdout.strip()
        return local == remote
    except subprocess.CalledProcessError:
        return False

def svn_uncommitted_files(svn_root: str) -> Set[str]:
    """Return set of files with uncommitted changes in the SVN working copy."""
    cp = run(["svn", "status"], cwd=svn_root)
    files: Set[str] = set()
    for line in cp.stdout.splitlines():
        if not line:
            continue
        code = line[0]
        if code in ("?", "X", " "):
            continue
        files.add(line[8:].strip())
    return files


def svn_local_status_entries(svn_root: str) -> List[str]:
    """Return local SVN status lines that should block an automatic update."""
    cp = run(["svn", "status"], cwd=svn_root)
    return [
        line
        for line in cp.stdout.splitlines()
        if line.strip() and line[0] not in ("?", "X", " ")
    ]


def resolve_svn_root(config: "SyncConfig") -> str:
    preset = PRESETS.get(config.preset_name or "")
    if config.preset_name and preset is None:
        raise ValueError(f"Unknown preset: {config.preset_name}")
    svn_root_raw = preset.svn_root if preset else config.svn_root
    if not svn_root_raw:
        raise ValueError("Must specify an SVN path or a preset")
    return os.path.abspath(os.path.expanduser(svn_root_raw))


def safe_svn_update(
    svn_root: str,
    reporter: Optional[CommandReporter] = None,
) -> None:
    """Run svn update only when svn status reports no local changes."""
    with workflow_context(reporter, False):
        try:
            run(["svn", "info"], cwd=svn_root)
        except subprocess.CalledProcessError as e:
            raise SyncError(f"Error: SVN probe failed in {svn_root}:\n{e.stderr}") from e
        except FileNotFoundError as e:
            raise SyncError("Error: Required tool for SVN not found on PATH.") from e

        local_entries = svn_local_status_entries(svn_root)
        if local_entries:
            emit_message("Refusing to run svn update because local SVN changes were found:")
            for line in local_entries:
                emit_message(f"  {line}")
            raise SyncError(
                "Refusing to run svn update because local SVN changes were found:\n"
                + "\n".join(f"  {line}" for line in local_entries)
            )

        emit_message("No local SVN changes detected. Running svn update...")
        run(["svn", "update"], cwd=svn_root)
        emit_message("SVN update complete.")


def safe_svn_update_from_config(
    config: "SyncConfig",
    reporter: Optional[CommandReporter] = None,
) -> None:
    safe_svn_update(resolve_svn_root(config), reporter=reporter)


def svn_ls_files(svn_root: str) -> Set[str]:
    """
    List versioned files from the local SVN working-copy metadata.

    Unlike ``svn list -R``, this does not contact the repository server.
    """
    return set(svn_file_metadata(svn_root))


def svn_file_metadata(
    svn_root: str,
) -> Dict[str, Tuple[Optional[int], Optional[str]]]:
    """Return local ``path -> (last-change timestamp, author)`` SVN metadata.

    A single recursive ``svn info --xml`` replaces both the remote recursive
    listing and two per-file ``svn info`` subprocesses used during comparison.
    """
    cp = run(["svn", "info", "--xml", "--depth", "infinity", "."], cwd=svn_root)
    root = ET.fromstring(cp.stdout)
    metadata: Dict[str, Tuple[Optional[int], Optional[str]]] = {}
    for entry in root.findall("entry"):
        if entry.get("kind") != "file":
            continue
        relpath = (entry.get("path") or "").replace(os.sep, "/")
        if not relpath or relpath == ".":
            continue
        commit = entry.find("commit")
        author: Optional[str] = None
        timestamp: Optional[int] = None
        if commit is not None:
            author = (commit.findtext("author") or "").strip() or None
            date_text = (commit.findtext("date") or "").strip()
            if date_text:
                try:
                    changed_at = datetime.datetime.fromisoformat(
                        date_text.replace("Z", "+00:00")
                    )
                    timestamp = int(changed_at.timestamp())
                except ValueError:
                    pass
        metadata[relpath] = (timestamp, author)
    return metadata

def svn_last_change(
    svn_root: str, relpath: str
) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """
    Return (timestamp_epoch, message, author) for the last change that touched relpath in SVN.
    Uses `svn info --show-item last-changed-date` and `last-changed-author` for metadata
    and `svn log -l 1` for message.
    """
    try:
        # Timestamp & author
        # Fetch last change date and author separately.  Older versions of SVN
        # only output the last requested item when multiple --show-item flags are
        # provided, so invoking once with both values would return only the
        # author (or date) and break parsing.  Querying each item individually
        # avoids that pitfall and keeps the code compatible across SVN versions.
        cp_date = run(
            ["svn", "info", "--show-item", "last-changed-date", "--", relpath],
            cwd=svn_root,
        )
        cp_author = run(
            ["svn", "info", "--show-item", "last-changed-author", "--", relpath],
            cwd=svn_root,
        )
        date_str = cp_date.stdout.strip()
        author = cp_author.stdout.strip() or None
        if not date_str:
            return None, None, None
        # Parse ISO 8601 to epoch (YYYY-MM-DDTHH:MM:SS.ZZZZZZZZZZZZ)
        # Use Python's fromisoformat after stripping timezone if present; fallback to `date`?
        # Simpler: ask svn for epoch with `--show-item last-changed-revision` then get log for that rev with --xml,
        # but we can rely on date_str being ISO8601 with timezone 'Z' or offset.
        # We'll parse robustly:
        import datetime
        try:
            dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            # Fallback to stripping fractional seconds
            if "." in date_str:
                base, tz = date_str.split(".", 1)
                # keep timezone offset part if exists
                if "+" in tz or "-" in tz:
                    # e.g., 2025-09-01T12:34:56.123456+00:00
                    frac, offset = tz[:tz.find("+") if "+" in tz else tz.find("-")], tz[tz.find("+") if "+" in tz else tz.find("-") :]
                    dt = datetime.datetime.fromisoformat(base + offset)
                else:
                    dt = datetime.datetime.fromisoformat(base)
            else:
                dt = datetime.datetime.fromisoformat(date_str)
        ts = int(dt.timestamp())

        # Message
        cp_msg = run(["svn", "log", "-l", "1", "--", relpath], cwd=svn_root)
        message = extract_last_svn_log_message(cp_msg.stdout)
        return ts, message, author
    except subprocess.CalledProcessError:
        return None, None, None

def svn_log_messages_since(svn_root: str, relpath: str, since_ts: Optional[int]) -> List[str]:
    """Return commit messages for relpath since the given timestamp (exclusive).

    Messages are returned oldest-first. If since_ts is None, all messages are
    returned.
    """
    try:
        cmd = ["svn", "log", "--xml"]
        if since_ts is not None and since_ts >= 0:
            # An SVN date resolves to the youngest repository revision at or
            # before that time, so the response can contain one older entry.
            # Query from the cutoff itself and filter exact entry timestamps
            # below instead of relying on date-to-revision rounding.
            cmd.extend(["-r", f"{{{utc_iso_from_epoch(since_ts)}}}:HEAD"])
        else:
            cmd.extend(["-r", "1:HEAD"])
        cmd.extend(["--", relpath])
        cp = run(cmd, cwd=svn_root)
        root = ET.fromstring(cp.stdout)
        messages: List[str] = []
        for entry in root.findall("logentry"):
            if since_ts is not None and since_ts >= 0:
                date_text = entry.findtext("date") or ""
                try:
                    changed_at = datetime.datetime.fromisoformat(
                        date_text.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    continue
                if changed_at <= since_ts:
                    continue
            message = (entry.findtext("msg") or "").strip()
            if message:
                messages.append(message)
        return messages
    except (subprocess.CalledProcessError, ET.ParseError):
        return []


def svn_log_messages_since_many(
    svn_root: str,
    cutoffs: Dict[str, int],
) -> Dict[str, List[str]]:
    """Return per-file SVN messages with one remote history request.

    SVN accepts multiple paths when they follow a repository URL. Verbose XML
    identifies the exact changed paths in each returned revision, allowing the
    combined response to be distributed back to individual files.
    """
    if not cutoffs:
        return {}

    results = {relpath: [] for relpath in cutoffs}
    root_rel = svn_relative_url(svn_root)
    if root_rel is None:
        return results

    nonnegative_cutoffs = [cutoff for cutoff in cutoffs.values() if cutoff >= 0]
    if len(nonnegative_cutoffs) == len(cutoffs):
        earliest = min(nonnegative_cutoffs)
        revision_range = f"{{{utc_iso_from_epoch(earliest)}}}:HEAD"
    else:
        revision_range = "1:HEAD"

    base_url = f"^/{root_rel}" if root_rel else "^/"
    relpaths = sorted(cutoffs)
    cmd = [
        "svn", "log", "--xml", "-v", "-r", revision_range,
        "--", base_url, *relpaths,
    ]
    try:
        cp = run(cmd, cwd=svn_root)
        root = ET.fromstring(cp.stdout)
    except (subprocess.CalledProcessError, ET.ParseError):
        return results

    repo_prefix = f"/{root_rel.strip('/')}" if root_rel else ""
    repo_paths = {
        f"{repo_prefix}/{relpath.replace(os.sep, '/').lstrip('/')}": relpath
        for relpath in relpaths
    }
    for entry in root.findall("logentry"):
        date_text = entry.findtext("date") or ""
        try:
            changed_at = datetime.datetime.fromisoformat(
                date_text.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            continue
        message = (entry.findtext("msg") or "").strip()
        if not message:
            continue
        changed_repo_paths = {
            (path.text or "").rstrip("/")
            for path in entry.findall("./paths/path")
        }
        for repo_path in changed_repo_paths:
            relpath = repo_paths.get(repo_path)
            if relpath is not None and changed_at > cutoffs[relpath]:
                results[relpath].append(message)
    return results

def svn_relative_url(svn_root: str) -> Optional[str]:
    """Return the repository-relative URL for an SVN working copy root."""
    try:
        relative = run(["svn", "info", "--show-item", "relative-url"], cwd=svn_root).stdout.strip()
    except subprocess.CalledProcessError:
        relative = ""

    if not relative:
        try:
            info = run(["svn", "info"], cwd=svn_root).stdout.splitlines()
        except subprocess.CalledProcessError:
            return None
        url = None
        repo_root = None
        for line in info:
            if line.startswith("Relative URL:"):
                relative = line.split(":", 1)[1].strip()
                break
            if line.startswith("URL:"):
                url = line.split(":", 1)[1].strip()
            elif line.startswith("Repository Root:"):
                repo_root = line.split(":", 1)[1].strip()
        if not relative and url and repo_root and url.startswith(repo_root):
            relative = "^/" + url[len(repo_root):].strip("/")

    if relative.startswith("^/"):
        return relative[2:].strip("/")
    if relative.startswith("^"):
        return relative[1:].strip("/")
    return relative.strip("/") if relative else None

def svn_repo_path_for_relpath(svn_root: str, relpath: str) -> Optional[str]:
    """Return the absolute repository path SVN verbose logs use for relpath."""
    root_rel = svn_relative_url(svn_root)
    if root_rel is None:
        return None
    rel = relpath.replace("\\", "/").strip("/")
    pieces = [p for p in (root_rel, rel) if p]
    return "/" + "/".join(pieces)

def parse_svn_log_date(date_text: str) -> Optional[int]:
    """Parse an SVN log header date to epoch seconds."""
    date_text = date_text.split(" (", 1)[0].strip()
    try:
        dt = datetime.datetime.strptime(date_text, "%Y-%m-%d %H:%M:%S %z")
        return int(dt.timestamp())
    except ValueError:
        try:
            dt = datetime.datetime.fromisoformat(date_text.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except ValueError:
            return None

def extract_svn_path_change(
    log_output: str,
    repo_path: str,
    actions: Set[str],
) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Return metadata for the first verbose SVN log entry touching repo_path."""
    lines = [l.rstrip("\n") for l in log_output.splitlines()]
    sep_indices = [i for i, l in enumerate(lines) if l.startswith("-" * 5)]
    normalized_repo_path = repo_path.rstrip("/")

    for start, end in zip(sep_indices, sep_indices[1:]):
        if start + 1 >= end:
            continue
        header = [part.strip() for part in lines[start + 1].split("|")]
        if len(header) < 3:
            continue
        author = header[1] or None
        ts = parse_svn_log_date(header[2])

        i = start + 2
        changed_paths: List[Tuple[str, str]] = []
        if i < end and lines[i].strip() == "Changed paths:":
            i += 1
            while i < end and lines[i].strip():
                changed = lines[i].strip()
                parts = changed.split(None, 1)
                if len(parts) == 2:
                    action = parts[0][:1]
                    path = parts[1].split(" (from ", 1)[0].rstrip("/")
                    changed_paths.append((action, path))
                i += 1

        message_start = i + 1 if i < end and not lines[i].strip() else i
        message = "\n".join(lines[message_start:end]).strip()
        for action, changed_path in changed_paths:
            if action in actions and changed_path == normalized_repo_path:
                return ts, message or None, author

    return None, None, None

def svn_deleted_change(
    svn_root: str,
    relpath: str,
) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Return SVN metadata for the revision that deleted relpath, if found."""
    repo_path = svn_repo_path_for_relpath(svn_root, relpath)
    if repo_path is None:
        return None, None, None

    parent = os.path.dirname(relpath.replace("\\", "/")) or "."
    anchors = [parent, "."]
    seen: Set[str] = set()
    for anchor in anchors:
        if anchor in seen:
            continue
        seen.add(anchor)
        try:
            cp = run(["svn", "log", "-v", "--", anchor], cwd=svn_root)
        except subprocess.CalledProcessError:
            continue
        ts, msg, author = extract_svn_path_change(cp.stdout, repo_path, {"D", "R"})
        if ts is not None or msg is not None or author is not None:
            return ts, msg, author

    return None, None, None

def svn_last_change_or_deleted(
    svn_root: str,
    relpath: str,
) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Return last SVN metadata for an existing path, or deletion metadata for a missing path."""
    ts, msg, author = svn_last_change(svn_root, relpath)
    if ts is not None or msg is not None or author is not None:
        return ts, msg, author
    return svn_deleted_change(svn_root, relpath)

def extract_svn_log_messages(log_output: str) -> List[str]:
    """Parse `svn log` output and return a list of commit messages."""
    lines = [l.rstrip("\n") for l in log_output.splitlines()]
    sep_indices = [i for i, l in enumerate(lines) if l.startswith("-" * 5)]
    messages: List[str] = []
    for start, end in zip(sep_indices, sep_indices[1:]):
        msg_start = start + 2
        msg_end = end
        msg = "\n".join(lines[msg_start:msg_end]).strip()
        if msg:
            messages.append(msg)
    return messages

def extract_last_svn_log_message(log_output: str) -> str:
    """Extract the first commit message from `svn log` output."""
    msgs = extract_svn_log_messages(log_output)
    if msgs:
        return msgs[0]
    return log_output.strip()

# ----- Core logic -----

@dataclass
class FileStatus:
    relpath: str
    in_git: bool
    in_svn: bool
    same_content: Optional[bool]  # None if not present in both
    git_ts: Optional[int]
    git_msg: Optional[str]
    git_author: Optional[str]
    svn_ts: Optional[int]
    svn_msg: Optional[str]
    svn_author: Optional[str]


@dataclass(frozen=True)
class SyncOperation:
    relpath: str
    destination: str  # "git" or "svn"
    action: str       # "copy" or "delete"
    message: str


@dataclass(frozen=True)
class Preset:
    git_root: str
    svn_root: str
    allow_extra_svn_top_entries: bool = False
    require_ignore_rebaseline: bool = True
    ignored_relpaths: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SyncConfig:
    git_root: Optional[str] = None
    svn_root: Optional[str] = None
    preset_name: Optional[str] = None
    dry_run: bool = False
    rebaseline: bool = False
    auto_yes: bool = False


@dataclass(frozen=True)
class PlanItem:
    relpath: str
    kind: str
    status: FileStatus
    suggested_operation: Optional[SyncOperation]
    alternate_operation: Optional[SyncOperation] = None
    note: str = ""


@dataclass(frozen=True)
class SyncPlan:
    config: SyncConfig
    preset: Optional[Preset]
    git_tracked_count: int
    svn_tracked_count: int
    dirty_git: Tuple[str, ...]
    dirty_svn: Tuple[str, ...]
    ignored_git: Tuple[str, ...]
    ignored_svn: Tuple[str, ...]
    items: Tuple[PlanItem, ...]
    rebaseline_paths: Tuple[str, ...] = ()
    rebaseline_placeholders: Tuple[str, ...] = ()

    @property
    def diffs(self) -> Tuple[PlanItem, ...]:
        return tuple(item for item in self.items if item.kind == "diff")

    @property
    def only_git(self) -> Tuple[PlanItem, ...]:
        return tuple(item for item in self.items if item.kind == "only_git")

    @property
    def only_svn(self) -> Tuple[PlanItem, ...]:
        return tuple(item for item in self.items if item.kind == "only_svn")


PRESETS: Dict[str, Preset] = {
    "sdds": Preset("~/github/SDDS", "~/epics/extensions/src/SDDS"),
    "sddsepics": Preset("~/github/SDDS-EPICS", "~/epics/extensions/src/SDDSepics"),
    "elegant": Preset("~/github/elegant", "~/oag/apps/src/elegant"),
    "spiffe": Preset("~/github/spiffe", "~/oag/apps/src/spiffe"),
    "clinchor": Preset("~/github/clinchor", "~/oag/apps/src/clinchor"),
    "shield": Preset("~/github/shield", "~/oag/apps/src/shield"),
    "oag": Preset(
        "~/github/oag-src",
        "~/oag/apps/src",
        allow_extra_svn_top_entries=True,
        require_ignore_rebaseline=False,
        ignored_relpaths=(
            "Makefile",
            "Makefile.build",
            "Makefile.oag-build",
            "Makefile.rules",
        ),
    ),
}


def resolve_config(config: SyncConfig) -> Tuple[SyncConfig, Optional[Preset]]:
    preset = PRESETS.get(config.preset_name or "")
    if config.preset_name and preset is None:
        raise ValueError(f"Unknown preset: {config.preset_name}")
    if preset:
        if config.git_root or config.svn_root:
            raise ValueError("Cannot combine preset options with Git/SVN paths")
        git_root_raw, svn_root_raw = preset.git_root, preset.svn_root
    else:
        if not (config.git_root and config.svn_root):
            raise ValueError("Must specify Git and SVN paths or a preset")
        git_root_raw, svn_root_raw = config.git_root, config.svn_root

    return (
        SyncConfig(
            git_root=os.path.abspath(os.path.expanduser(git_root_raw)),
            svn_root=os.path.abspath(os.path.expanduser(svn_root_raw)),
            preset_name=config.preset_name,
            dry_run=config.dry_run,
            rebaseline=config.rebaseline,
            auto_yes=config.auto_yes,
        ),
        preset,
    )


def preset_ignored_svn_paths(
    preset: Optional["Preset"],
    git_set: Set[str],
    svn_set: Set[str],
) -> Set[str]:
    """
    Return SVN-relative paths that should be ignored for the selected preset.

    Some SVN working copies intentionally contain sibling top-level entries
    that are outside the Git mirror. In that case, ignore SVN files whose
    first path component is not tracked anywhere in the Git mirror.
    """
    if not preset or not preset.allow_extra_svn_top_entries:
        return set()

    git_top_entries = {path.split("/", 1)[0] for path in git_set}
    if not git_top_entries:
        return set()

    return {
        relpath
        for relpath in svn_set
        if relpath.split("/", 1)[0] not in git_top_entries
    }

def build_index(git_root: str, svn_root: str) -> Tuple[Set[str], Set[str]]:
    git_set = {p for p in git_ls_files(git_root) if not is_always_ignored_relpath(p)}
    svn_set = {p for p in svn_ls_files(svn_root) if not is_always_ignored_relpath(p)}
    return git_set, svn_set

def compare_and_collect(
    git_root: str,
    svn_root: str,
    git_set: Set[str],
    svn_set: Set[str],
    svn_metadata: Optional[
        Dict[str, Tuple[Optional[int], Optional[str]]]
    ] = None,
) -> Dict[str, FileStatus]:
    all_paths = sorted(git_set.union(svn_set))
    status: Dict[str, FileStatus] = {}

    for rel in all_paths:
        in_git = rel in git_set
        in_svn = rel in svn_set
        same: Optional[bool] = None
        git_ts = git_msg = git_author = svn_ts = svn_msg = svn_author = None

        if in_git and in_svn:
            git_abs = os.path.join(git_root, rel)
            svn_abs = os.path.join(svn_root, rel)
            git_sig = tracked_path_signature(git_abs)
            svn_sig = tracked_path_signature(svn_abs)
            if git_sig is not None and svn_sig is not None:
                same = (git_sig == svn_sig)
            else:
                # If one is a directory or missing on disk (shouldn't be if tracked), treat as different
                same = False

            if not same:
                git_ts, git_msg, git_author = git_last_change(git_root, rel)
                if svn_metadata is not None and rel in svn_metadata:
                    svn_ts, svn_author = svn_metadata[rel]
                    # The message is loaded with the source history only if SVN
                    # proves newer. Avoiding an unconditional remote log lookup
                    # here is the main Scan performance improvement.
                    svn_msg = None
                else:
                    svn_ts, svn_msg, svn_author = svn_last_change(svn_root, rel)
        elif in_svn and svn_metadata is not None and rel in svn_metadata:
            svn_ts, svn_author = svn_metadata[rel]

        status[rel] = FileStatus(
            relpath=rel,
            in_git=in_git,
            in_svn=in_svn,
            same_content=same,
            git_ts=git_ts,
            git_msg=git_msg,
            git_author=git_author,
            svn_ts=svn_ts,
            svn_msg=svn_msg,
            svn_author=svn_author,
        )

    return status

def copy_file(src_root: str, dst_root: str, relpath: str, dry_run: bool):
    src = os.path.join(src_root, relpath)
    dst = os.path.join(dst_root, relpath)
    if dry_run:
        emit_message(f"[dry-run] copy {src} -> {dst}")
        emit_file_operation("copy", relpath, src, dst, dry_run=True, planned=True)
        return
    emit_file_operation("copy", relpath, src, dst, dry_run=False, planned=False)
    ensure_parent_dir(dst)
    if os.path.lexists(dst):
        os.remove(dst)
    if os.path.islink(src):
        os.symlink(os.readlink(src), dst)
        return
    shutil.copy2(src, dst)

def grouped_operations(
    operations: Iterable[SyncOperation],
) -> List[Tuple[Tuple[str, str], List[SyncOperation]]]:
    groups: Dict[Tuple[str, str], List[SyncOperation]] = {}
    for op in operations:
        groups.setdefault((op.destination, op.message), []).append(op)
    return list(groups.items())

def execute_operation_groups(
    operations: Iterable[SyncOperation],
    git_root: str,
    svn_root: str,
    dry_run: bool,
):
    groups = grouped_operations(operations)
    if not groups:
        emit_message("\nNo approved changes.")
        return

    emit_message(f"\nCommitting {len(groups)} grouped change set(s).")
    for (destination, message), group in groups:
        relpaths = sorted({op.relpath for op in group})
        emit_message(f"\nGROUP: {destination.upper()} commit for {len(relpaths)} file(s)")
        for relpath in relpaths:
            emit_message(f"  {relpath}")
        emit_message(f"  Commit message:\n    {indent_message(message)}")

        if destination == "git":
            execute_git_group(group, git_root, svn_root, message, dry_run)
        else:
            execute_svn_group(group, git_root, svn_root, message, dry_run)

def execute_git_group(
    operations: List[SyncOperation],
    git_root: str,
    svn_root: str,
    message: str,
    dry_run: bool,
):
    copy_paths = sorted(op.relpath for op in operations if op.action == "copy")
    delete_paths = sorted(op.relpath for op in operations if op.action == "delete")
    commit_paths = sorted(set(copy_paths + delete_paths))

    for relpath in copy_paths:
        copy_file(svn_root, git_root, relpath, dry_run)

    if dry_run:
        if copy_paths:
            cmd = ["git", "add", "--", *copy_paths]
            emit_message(f"[dry-run] {format_command(cmd)}")
            emit_command_event(cmd, git_root, dry_run=True, planned=True)
        if delete_paths:
            cmd = ["git", "rm", "--", *delete_paths]
            emit_message(f"[dry-run] {format_command(cmd)}")
            emit_command_event(cmd, git_root, dry_run=True, planned=True)
            for relpath in delete_paths:
                emit_file_operation(
                    "delete", relpath, os.path.join(git_root, relpath), None,
                    dry_run=True, planned=True
                )
        cmd = ["git", "commit", "-m", message, "--", *commit_paths]
        emit_message(f"[dry-run] {format_command(cmd)}")
        emit_command_event(cmd, git_root, dry_run=True, planned=True)
        cmd = ["git", "push", "origin", "master"]
        emit_message(f"[dry-run] {format_command(cmd)}")
        emit_command_event(cmd, git_root, dry_run=True, planned=True)
        return

    if copy_paths:
        run(["git", "add", "--", *copy_paths], cwd=git_root)
    if delete_paths:
        for relpath in delete_paths:
            emit_file_operation(
                "delete", relpath, os.path.join(git_root, relpath), None,
                dry_run=False, planned=False
            )
        run(["git", "rm", "--", *delete_paths], cwd=git_root)
    run(["git", "commit", "-m", message, "--", *commit_paths], cwd=git_root)
    run(["git", "push", "origin", "master"], cwd=git_root)

def execute_svn_group(
    operations: List[SyncOperation],
    git_root: str,
    svn_root: str,
    message: str,
    dry_run: bool,
):
    copy_paths = sorted(op.relpath for op in operations if op.action == "copy")
    delete_paths = sorted(op.relpath for op in operations if op.action == "delete")
    commit_paths = sorted(set(copy_paths + delete_paths))

    for relpath in copy_paths:
        copy_file(git_root, svn_root, relpath, dry_run)

    if dry_run:
        if copy_paths:
            cmd = ["svn", "add", "--parents", "--force", "--", *copy_paths]
            emit_message(
                f"[dry-run] {format_command(cmd)}"
            )
            emit_command_event(cmd, svn_root, dry_run=True, planned=True)
        if delete_paths:
            cmd = ["svn", "delete", "--", *delete_paths]
            emit_message(f"[dry-run] {format_command(cmd)}")
            emit_command_event(cmd, svn_root, dry_run=True, planned=True)
            for relpath in delete_paths:
                emit_file_operation(
                    "delete", relpath, os.path.join(svn_root, relpath), None,
                    dry_run=True, planned=True
                )
        cmd = ["svn", "commit", "-m", message, "--", *commit_paths]
        emit_message(f"[dry-run] {format_command(cmd)}")
        emit_command_event(cmd, svn_root, dry_run=True, planned=True)
        return

    if copy_paths:
        # --force makes already-versioned files a no-op, while --parents stages
        # any missing directories needed by genuinely new files.
        run(
            ["svn", "add", "--parents", "--force", "--", *copy_paths],
            cwd=svn_root,
        )

        # SVN will not commit a newly added file unless its newly added parent
        # directories are also explicit commit targets.  Limit the status
        # query to parents of this group's files so unrelated working-copy
        # changes can never expand the commit scope.
        parent_paths: Set[str] = set()
        for relpath in copy_paths:
            parent = os.path.dirname(relpath)
            while parent:
                parent_paths.add(parent)
                parent = os.path.dirname(parent)
        if parent_paths:
            ordered_parents = sorted(parent_paths)
            cp = run(
                ["svn", "status", "--depth", "empty", "--", *ordered_parents],
                cwd=svn_root,
            )
            added_parents = {
                line[8:].strip()
                for line in cp.stdout.splitlines()
                if line and line[0] == "A"
            }
            commit_paths = sorted(set(commit_paths).union(added_parents))
    if delete_paths:
        for relpath in delete_paths:
            emit_file_operation(
                "delete", relpath, os.path.join(svn_root, relpath), None,
                dry_run=False, planned=False
            )
        run(["svn", "delete", "--", *delete_paths], cwd=svn_root)
    run(["svn", "commit", "-m", message, "--", *commit_paths], cwd=svn_root)


def mismatch_sides(
    st: FileStatus,
) -> Optional[Tuple[str, str, int, int, Optional[str], Optional[str]]]:
    """Return (newer, older, newer_ts, older_ts, message, author) for a diff."""
    git_ts = st.git_ts or -1
    svn_ts = st.svn_ts or -1
    if git_ts == -1 and svn_ts == -1:
        return None
    newer = "git" if git_ts >= svn_ts else "svn"
    older = "svn" if newer == "git" else "git"
    newer_ts = git_ts if newer == "git" else svn_ts
    older_ts = svn_ts if newer == "git" else git_ts
    newer_msg = st.git_msg if newer == "git" else st.svn_msg
    newer_author = st.git_author if newer == "git" else st.svn_author
    return newer, older, newer_ts, older_ts, newer_msg, newer_author


def operation_for_mismatch(
    st: FileStatus,
    git_root: Optional[str] = None,
    svn_root: Optional[str] = None,
    history_messages: Optional[List[str]] = None,
) -> Optional[SyncOperation]:
    """Build a copy operation using all source commits after the older change.

    The last change on the destination side is the best cross-VCS baseline
    available because Git and SVN do not share revision identifiers.  Keep the
    optional roots for backwards compatibility with callers that only need the
    latest-message behavior.
    """
    sides = mismatch_sides(st)
    if sides is None:
        return None
    newer, older, _newer_ts, older_ts, newer_msg, newer_author = sides

    messages: List[str] = []
    if newer == "git":
        if history_messages is not None:
            messages = history_messages
        elif git_root is not None:
            messages = git_log_messages_since(git_root, st.relpath, older_ts)
        fallback = newer_msg or f"Sync {st.relpath} from Git"
        destination = "svn"
    else:
        if history_messages is not None:
            messages = history_messages
        elif svn_root is not None:
            messages = svn_log_messages_since(svn_root, st.relpath, older_ts)
        fallback = newer_msg or f"Sync {st.relpath} from SVN"
        destination = "git"

    combined = "\n\n".join(messages) if messages else fallback
    commit_msg = augment_message(combined, newer_author)
    return SyncOperation(
        st.relpath,
        destination,
        "copy",
        commit_msg,
    )


def add_operation_for_only(
    rel: str,
    present_in: str,
    git_root: str,
    svn_root: str,
) -> SyncOperation:
    if present_in == "git":
        ts, last_msg, author = git_last_change(git_root, rel)
        msgs = git_log_messages_since(git_root, rel, None)
        combined = "\n\n".join(msgs) if msgs else last_msg
        commit_msg = augment_message(combined or f"Add {rel} (synced from Git)", author)
        return SyncOperation(rel, "svn", "copy", commit_msg)
    ts, last_msg, author = svn_last_change(svn_root, rel)
    msgs = svn_log_messages_since(svn_root, rel, None)
    combined = "\n\n".join(msgs) if msgs else last_msg
    commit_msg = augment_message(combined or f"Add {rel} (synced from SVN)", author)
    return SyncOperation(rel, "git", "copy", commit_msg)


def preview_add_operation_for_only(
    st: FileStatus,
    present_in: str,
    git_root: str,
) -> SyncOperation:
    """Build a fast Scan-time add operation without remote history reads."""
    rel = st.relpath
    if present_in == "git":
        _ts, latest_message, author = git_last_change(git_root, rel)
        commit_msg = augment_message(
            latest_message or f"Add {rel} (synced from Git)", author
        )
        return SyncOperation(rel, "svn", "copy", commit_msg)
    commit_msg = augment_message(
        st.svn_msg or f"Add {rel} (synced from SVN)", st.svn_author
    )
    return SyncOperation(rel, "git", "copy", commit_msg)


def preview_remove_operation_for_only(rel: str, present_in: str) -> SyncOperation:
    """Build a Scan-time removal preview without repository history reads."""
    if present_in == "git":
        return SyncOperation(rel, "git", "delete", f"Remove {rel} (not present in SVN)")
    return SyncOperation(rel, "svn", "delete", f"Remove {rel} (not present in Git)")


def remove_operation_for_only(
    rel: str,
    present_in: str,
    git_root: str,
    svn_root: str,
) -> SyncOperation:
    if present_in == "git":
        ts, msg, author = svn_last_change_or_deleted(svn_root, rel)
        commit_msg = augment_message(msg or f"Remove {rel} (not present in SVN)", author)
        return SyncOperation(rel, "git", "delete", commit_msg)
    ts, msg, author = git_last_change(git_root, rel)
    commit_msg = augment_message(msg or f"Remove {rel} (not present in Git)", author)
    return SyncOperation(rel, "svn", "delete", commit_msg)


def handle_mismatch(
    st: FileStatus,
    git_root: str,
    svn_root: str,
    auto_yes: bool
) -> Optional[SyncOperation]:
    rel = st.relpath
    # Decide newer side
    sides = mismatch_sides(st)
    if sides is None:
        emit_message(
            f"?? {rel}: content differs but no commit timestamps could be read. Skipping."
        )
        return None

    newer, older, newer_ts, older_ts, _newer_msg, newer_author = sides
    operation = operation_for_mismatch(st, git_root, svn_root)
    if operation is None:
        return None

    emit_message(f"\nDIFF: {rel}")
    emit_message(f"  Last change: {newer.upper()} is newer ({newer_ts}), {older.upper()} older ({older_ts})")
    author_str = f" by {newer_author}" if newer_author else ""
    emit_message(
        f"  Commit message(s) since the last {older.upper()} change "
        f"({newer.upper()}{author_str}):\n    {indent_message(operation.message)}"
    )

    if prompt_yes_no(f"Sync {rel}? Queue copy {newer.upper()} -> {older.upper()} with those messages.", default_yes=True, auto_yes=auto_yes):
        return operation
    else:
        emit_message("  Skipped.")
    return None

def handle_only_in_one(
    rel: str,
    present_in: str,   # "git" or "svn"
    git_root: str,
    svn_root: str,
    auto_yes: bool
) -> Optional[SyncOperation]:
    other = "svn" if present_in == "git" else "git"
    emit_message(f"\nONLY IN {present_in.upper()}: {rel}")

    # Offer to add to the other repo (default) or remove from the current repo
    do_add = prompt_yes_no(
        f"Add {rel} to {other.upper()}? (No = remove from {present_in.upper()})",
        default_yes=True, auto_yes=auto_yes
    )

    if do_add:
        return add_operation_for_only(rel, present_in, git_root, svn_root)
    return remove_operation_for_only(rel, present_in, git_root, svn_root)

def indent_message(msg: Optional[str]) -> str:
    if not msg:
        return "(no message)"
    lines = msg.splitlines() or [msg]
    return "\n    ".join(lines)


def augment_message(msg: str, author: Optional[str]) -> str:
    """Append original author information to commit message if provided."""
    if author:
        return f"{msg}\n\nOriginal author: {author}"
    return msg


def _path_under(root: str, path: str) -> bool:
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


def build_plan_items(
    status: Dict[str, FileStatus],
    git_root: str,
    svn_root: str,
) -> Tuple[PlanItem, ...]:
    items: List[PlanItem] = []
    for st in status.values():
        if st.in_git and st.in_svn and st.same_content is False:
            # Full history is intentionally deferred until execution. Scan only
            # needs direction, latest metadata, and a useful preview message.
            op = operation_for_mismatch(st)
            note = "" if op else "No Git or SVN timestamp could be read."
            items.append(PlanItem(st.relpath, "diff", st, op, None, note))
        elif st.in_git and not st.in_svn:
            items.append(
                PlanItem(
                    st.relpath,
                    "only_git",
                    st,
                    preview_add_operation_for_only(st, "git", git_root),
                    preview_remove_operation_for_only(st.relpath, "git"),
                )
            )
        elif st.in_svn and not st.in_git:
            items.append(
                PlanItem(
                    st.relpath,
                    "only_svn",
                    st,
                    preview_add_operation_for_only(st, "svn", git_root),
                    preview_remove_operation_for_only(st.relpath, "svn"),
                )
            )
    return tuple(items)


def prepare_sync_plan(
    config: SyncConfig,
    reporter: Optional[CommandReporter] = None,
) -> SyncPlan:
    resolved_config, chosen_preset = resolve_config(config)
    git_root = resolved_config.git_root or ""
    svn_root = resolved_config.svn_root or ""

    with workflow_context(reporter, resolved_config.dry_run):
        for root, name, probe in [
            (git_root, "Git", ["git", "rev-parse", "--is-inside-work-tree"]),
            (svn_root, "SVN", ["svn", "info"]),
        ]:
            try:
                run(probe, cwd=root)
            except subprocess.CalledProcessError as e:
                raise SyncError(
                    f"Error: {name} probe failed in {root}:\n{e.stderr}"
                ) from e
            except FileNotFoundError as e:
                raise SyncError(
                    f"Error: Required tool for {name} not found on PATH."
                ) from e

        if not git_is_up_to_date(git_root, refresh=not resolved_config.dry_run):
            raise SyncError(
                f"Error: Git working copy in {git_root} is not up to date with its upstream.\n"
                f"Please run 'git pull origin master' in {git_root} before running this script."
            )

        if not svn_is_up_to_date(svn_root):
            raise SyncError(
                f"Error: SVN working copy in {svn_root} is not up to date with the repository.\n"
                f"Please run 'svn update' in {svn_root} before running this script."
            )

        dirty_git = {
            p for p in git_uncommitted_files(git_root)
            if not is_always_ignored_relpath(p)
        }
        dirty_svn = {
            p for p in svn_uncommitted_files(svn_root)
            if not is_always_ignored_relpath(p)
        }
        dirty_all = dirty_git.union(dirty_svn)
        if dirty_all:
            emit_message("The following files have uncommitted changes and will be ignored:")
            if dirty_git:
                emit_message("  Git:")
                for p in sorted(dirty_git):
                    emit_message(f"    {p}")
            if dirty_svn:
                emit_message("  SVN:")
                for p in sorted(dirty_svn):
                    emit_message(f"    {p}")
        else:
            emit_message("No uncommitted changes detected.")

        emit_message("Indexing versioned files...")
        git_set = {
            p for p in git_ls_files(git_root)
            if not is_always_ignored_relpath(p)
        }
        svn_metadata = {
            p: info for p, info in svn_file_metadata(svn_root).items()
            if not is_always_ignored_relpath(p)
        }
        svn_set = set(svn_metadata)
        git_tracked_count = len(git_set)
        svn_tracked_count = len(svn_set)

        emit_message(f"  Git tracked files: {git_tracked_count}")
        emit_message(f"  SVN tracked files: {svn_tracked_count}")

        ignore_set_abs = load_ignore_set()
        ignore_git: Set[str] = set()
        ignore_svn: Set[str] = set()
        for p in ignore_set_abs:
            rel_git = os.path.relpath(p, git_root)
            if rel_git != "." and not rel_git.startswith("..") and not os.path.isabs(rel_git):
                ignore_git.add(rel_git)
            rel_svn = os.path.relpath(p, svn_root)
            if rel_svn != "." and not rel_svn.startswith("..") and not os.path.isabs(rel_svn):
                ignore_svn.add(rel_svn)

        preset_ignore_svn = preset_ignored_svn_paths(chosen_preset, git_set, svn_set)
        if preset_ignore_svn:
            emit_message(
                f"Ignoring {len(preset_ignore_svn)} SVN files under top-level entries not present in Git."
            )
            ignore_svn |= preset_ignore_svn

        if chosen_preset and chosen_preset.ignored_relpaths:
            preset_ignore_both = set(chosen_preset.ignored_relpaths)
            ignored_present = sorted(
                p for p in preset_ignore_both if p in git_set or p in svn_set
            )
            if ignored_present:
                emit_message(
                    "Ignoring preset-specific paths: "
                    + ", ".join(ignored_present)
                )
            ignore_git |= preset_ignore_both
            ignore_svn |= preset_ignore_both

        require_ignore_rebaseline = (
            chosen_preset.require_ignore_rebaseline if chosen_preset else True
        )
        if (
            not resolved_config.rebaseline
            and require_ignore_rebaseline
            and (not ignore_git or not ignore_svn)
        ):
            missing = (
                f"{'Git' if not ignore_git else ''}"
                f"{' and ' if not ignore_git and not ignore_svn else ''}"
                f"{'SVN' if not ignore_svn else ''}"
            )
            raise SyncError(
                f"Error: {IGNORE_FILE} lacks entries for {missing}.\n"
                "Please run with -rebaseline"
            )

        git_set -= ignore_git
        svn_set -= ignore_svn
        git_set -= dirty_all
        svn_set -= dirty_all

        if resolved_config.rebaseline:
            only_git = sorted(git_set - svn_set)
            only_svn = sorted(svn_set - git_set)
            to_add_abs = (
                [os.path.join(git_root, p) for p in only_git]
                + [os.path.join(svn_root, p) for p in only_svn]
            )
            rebaseline_paths = tuple(p for p in to_add_abs if p not in ignore_set_abs)
            placeholders: Tuple[str, ...] = ()
            if not rebaseline_paths:
                placeholder_list: List[str] = []
                if not any(_path_under(git_root, p) for p in ignore_set_abs):
                    placeholder_list.append(os.path.join(git_root, ".ignore"))
                if not any(_path_under(svn_root, p) for p in ignore_set_abs):
                    placeholder_list.append(os.path.join(svn_root, ".ignore"))
                placeholders = tuple(
                    p for p in placeholder_list if p not in ignore_set_abs
                )
            return SyncPlan(
                resolved_config,
                chosen_preset,
                git_tracked_count,
                svn_tracked_count,
                tuple(sorted(dirty_git)),
                tuple(sorted(dirty_svn)),
                tuple(sorted(ignore_git)),
                tuple(sorted(ignore_svn)),
                (),
                rebaseline_paths,
                placeholders,
            )

        status = compare_and_collect(
            git_root,
            svn_root,
            git_set,
            svn_set,
            svn_metadata=svn_metadata,
        )
        return SyncPlan(
            resolved_config,
            chosen_preset,
            git_tracked_count,
            svn_tracked_count,
            tuple(sorted(dirty_git)),
            tuple(sorted(dirty_svn)),
            tuple(sorted(ignore_git)),
            tuple(sorted(ignore_svn)),
            build_plan_items(status, git_root, svn_root),
        )


def apply_rebaseline_plan(
    plan: SyncPlan,
    reporter: Optional[CommandReporter] = None,
) -> List[str]:
    paths = list(plan.rebaseline_paths or plan.rebaseline_placeholders)
    with workflow_context(reporter, plan.config.dry_run):
        if plan.config.dry_run:
            if paths:
                for path in paths:
                    emit_message(f"[dry-run] would add {path} to {IGNORE_FILE}")
                return paths
            emit_message("No new paths to add to ignore file.")
            return []

        if paths:
            added = append_to_ignore(paths, existing=load_ignore_set())
            if added:
                if plan.rebaseline_placeholders and not plan.rebaseline_paths:
                    emit_message(
                        f"Added placeholder paths to {IGNORE_FILE} to record rebaseline."
                    )
                else:
                    emit_message(f"Added {len(added)} paths to {IGNORE_FILE}")
            else:
                emit_message("No new paths to add to ignore file.")
            return added

        emit_message("No new paths to add to ignore file.")
        return []


def plan_summary(plan: SyncPlan) -> str:
    return (
        "\nSummary:\n"
        f"  Files that differ: {len(plan.diffs)}\n"
        f"  Only in Git: {len(plan.only_git)}\n"
        f"  Only in SVN: {len(plan.only_svn)}"
    )


def choose_operations_from_plan(plan: SyncPlan) -> List[SyncOperation]:
    operations: List[SyncOperation] = []
    for item in plan.items:
        st = item.status
        if item.kind == "diff":
            sides = mismatch_sides(st)
            if sides is None:
                emit_message(
                    f"?? {item.relpath}: content differs but no commit timestamps could be read. Skipping."
                )
                continue
            newer, older, newer_ts, older_ts, newer_msg, newer_author = sides
            emit_message(f"\nDIFF: {item.relpath}")
            emit_message(
                f"  Last change: {newer.upper()} is newer ({newer_ts}), {older.upper()} older ({older_ts})"
            )
            author_str = f" by {newer_author}" if newer_author else ""
            emit_message(
                f"  Commit message ({newer.upper()}{author_str}):\n    {indent_message(newer_msg)}"
            )
            if item.suggested_operation and prompt_yes_no(
                f"Sync {item.relpath}? Queue copy {newer.upper()} -> {older.upper()} with that message.",
                default_yes=True,
                auto_yes=plan.config.auto_yes,
            ):
                operations.append(item.suggested_operation)
            else:
                emit_message("  Skipped.")
            continue

        present_in = "git" if item.kind == "only_git" else "svn"
        other = "svn" if present_in == "git" else "git"
        emit_message(f"\nONLY IN {present_in.upper()}: {item.relpath}")
        do_add = prompt_yes_no(
            f"Add {item.relpath} to {other.upper()}? (No = remove from {present_in.upper()})",
            default_yes=True,
            auto_yes=plan.config.auto_yes,
        )
        op = item.suggested_operation if do_add else item.alternate_operation
        if op:
            operations.append(op)
    return operations


def hydrate_operation_messages(
    plan: SyncPlan,
    operations: Iterable[SyncOperation],
) -> List[SyncOperation]:
    """Load full source histories for selected copy operations at run time."""
    selected = list(operations)
    items_by_path = {item.relpath: item for item in plan.items}
    svn_cutoffs: Dict[str, int] = {}
    for operation in selected:
        if operation.action != "copy" or operation.destination != "git":
            continue
        item = items_by_path.get(operation.relpath)
        if item is None:
            continue
        if item.kind == "diff":
            sides = mismatch_sides(item.status)
            if sides is not None and sides[0] == "svn":
                svn_cutoffs[operation.relpath] = sides[3]
        elif item.kind == "only_svn":
            svn_cutoffs[operation.relpath] = -1

    if any(operation.action == "copy" for operation in selected):
        emit_message("Loading complete commit history for selected files...")
    svn_histories = svn_log_messages_since_many(
        plan.config.svn_root or "", svn_cutoffs
    )

    hydrated: List[SyncOperation] = []
    for operation in selected:
        if operation.action != "copy":
            item = items_by_path.get(operation.relpath)
            if item is not None and item.kind in ("only_git", "only_svn"):
                present_in = "git" if item.kind == "only_git" else "svn"
                hydrated.append(
                    remove_operation_for_only(
                        operation.relpath,
                        present_in,
                        plan.config.git_root or "",
                        plan.config.svn_root or "",
                    )
                )
            else:
                hydrated.append(operation)
            continue
        item = items_by_path.get(operation.relpath)
        if item is None:
            hydrated.append(operation)
            continue

        st = item.status
        if item.kind == "diff":
            sides = mismatch_sides(st)
            prefetched = (
                svn_histories.get(operation.relpath, [])
                if sides is not None and sides[0] == "svn"
                else None
            )
            full_operation = operation_for_mismatch(
                st,
                plan.config.git_root,
                plan.config.svn_root,
                history_messages=prefetched,
            )
            hydrated.append(full_operation or operation)
            continue

        if item.kind == "only_git":
            hydrated.append(
                add_operation_for_only(
                    operation.relpath,
                    "git",
                    plan.config.git_root or "",
                    plan.config.svn_root or "",
                )
            )
            continue

        if item.kind == "only_svn":
            messages = svn_histories.get(operation.relpath, [])
            combined = "\n\n".join(messages) if messages else (
                st.svn_msg or f"Add {operation.relpath} (synced from SVN)"
            )
            hydrated.append(
                SyncOperation(
                    operation.relpath,
                    "git",
                    "copy",
                    augment_message(combined, st.svn_author),
                )
            )
            continue

        hydrated.append(operation)
    return hydrated


def execute_plan_operations(
    plan: SyncPlan,
    operations: Iterable[SyncOperation],
    reporter: Optional[CommandReporter] = None,
) -> None:
    with workflow_context(reporter, plan.config.dry_run):
        operations_with_history = hydrate_operation_messages(plan, operations)
        execute_operation_groups(
            operations_with_history,
            plan.config.git_root or "",
            plan.config.svn_root or "",
            plan.config.dry_run,
        )


def run_sync(
    config: SyncConfig,
    reporter: Optional[CommandReporter] = None,
) -> SyncPlan:
    plan = prepare_sync_plan(config, reporter=reporter)
    if plan.config.rebaseline:
        apply_rebaseline_plan(plan, reporter=reporter)
        return plan

    with workflow_context(reporter, plan.config.dry_run):
        emit_message(plan_summary(plan))
        operations = choose_operations_from_plan(plan)
    execute_plan_operations(plan, operations, reporter=reporter)
    with workflow_context(reporter, plan.config.dry_run):
        emit_message("\nDone.")
    return plan


# Self-alias keeps the consolidated GUI implementation compatible with the
# former git_svn_sync_gui module's qualified references.
sync = sys.modules[__name__]

MANUAL_PRESET_LABEL = "Manual paths"
SVN_UPDATE_HINT = "Please run 'svn update'"
GIT_UPDATE_HINT = "Please run 'git pull"
FILTER_ALL = "all"
FILTER_DIFF = "diff"
FILTER_ONLY_GIT = "only_git"
FILTER_ONLY_SVN = "only_svn"

COLORS = {
    "background": "#F4F7FB",
    "surface": "#FFFFFF",
    "surface_muted": "#F8FAFC",
    "border": "#DCE3EC",
    "text": "#172033",
    "muted": "#64748B",
    "primary": "#2563EB",
    "primary_hover": "#1D4ED8",
    "success": "#16A34A",
    "success_hover": "#15803D",
    "warning": "#D97706",
    "selection": "#DBEAFE",
    "console": "#111827",
    "console_text": "#D1D5DB",
}


@dataclass
class GuiPlanRow:
    item: sync.PlanItem
    selected: bool = True
    use_alternate: bool = False

    @property
    def operation(self) -> Optional[sync.SyncOperation]:
        if self.use_alternate and self.item.alternate_operation:
            return self.item.alternate_operation
        return self.item.suggested_operation


def operation_label(operation: Optional[sync.SyncOperation]) -> str:
    if operation is None:
        return "Skip"
    if operation.action == "copy":
        source = "SVN" if operation.destination == "git" else "GIT"
        return f"{source} → {operation.destination.upper()}"
    return f"Delete from {operation.destination.upper()}"


def operation_author(operation: Optional[sync.SyncOperation]) -> str:
    if operation is None:
        return ""
    marker = "\nOriginal author: "
    if marker in operation.message:
        return operation.message.rsplit(marker, 1)[1].strip()
    return ""


def operation_summary(operation: Optional[sync.SyncOperation]) -> str:
    if operation is None or not operation.message:
        return ""
    return operation.message.splitlines()[0]


def row_values(row: GuiPlanRow) -> tuple:
    operation = row.operation
    status = {
        "diff": "Different",
        "only_git": "Only in Git",
        "only_svn": "Only in SVN",
    }.get(row.item.kind, row.item.kind)
    return (
        "[x]" if row.selected else "[ ]",
        status,
        operation_label(operation),
        row.item.relpath,
        operation_author(operation),
        operation_summary(operation),
    )


def selected_operations(rows: Iterable[GuiPlanRow]) -> List[sync.SyncOperation]:
    operations: List[sync.SyncOperation] = []
    for row in rows:
        if row.selected and row.operation:
            operations.append(row.operation)
    return operations


def row_matches_filter(
    row: GuiPlanRow,
    filter_name: str,
    query: str,
) -> bool:
    """Return whether a row belongs in the current results view."""
    if filter_name != FILTER_ALL and row.item.kind != filter_name:
        return False
    normalized_query = query.strip().casefold()
    if not normalized_query:
        return True
    operation = row.operation
    searchable = "\n".join((
        row.item.relpath,
        operation_label(operation),
        operation_author(operation),
        operation.message if operation else "",
    )).casefold()
    return normalized_query in searchable


def is_svn_update_required_error(message: str) -> bool:
    return SVN_UPDATE_HINT in message


def is_git_update_required_error(message: str) -> bool:
    return GIT_UPDATE_HINT in message


def message_needs_tooltip(
    full_message: str,
    displayed_message: str,
    cell_width: int,
    measure_text: Callable[[str], int],
    horizontal_padding: int = 12,
) -> bool:
    """Return whether a Message cell hides any of its underlying text."""
    if not full_message or not displayed_message:
        return False
    if full_message.strip() != displayed_message.strip():
        return True
    available_width = max(0, cell_width - horizontal_padding)
    return measure_text(displayed_message) > available_width


class TkQueueReporter(sync.CommandReporter):
    def __init__(self, events: "queue.Queue[tuple]"):
        self.events = events

    def message(self, text: str, stream: str = "stdout") -> None:
        self.events.put(("message", stream, text))

    def command(self, event: sync.CommandEvent) -> None:
        self.events.put(("command", event))

    def file_operation(self, event: sync.FileOperationEvent) -> None:
        self.events.put(("file_operation", event))


class ScanCommandReporter(sync.CommandReporter):
    """Report only command lines while a Scan is running."""

    def __init__(self, events: "queue.Queue[tuple]"):
        self.events = events

    def message(self, text: str, stream: str = "stdout") -> None:
        pass

    def command(self, event: sync.CommandEvent) -> None:
        self.events.put((
            "command",
            dataclasses.replace(event, stdout="", stderr="", returncode=None),
        ))

    def file_operation(self, event: sync.FileOperationEvent) -> None:
        pass


class TreeMessageTooltip:
    """Show complete Message text when a Treeview cell cannot show it all."""

    def __init__(
        self,
        tree: ttk.Treeview,
        message_for_item: Callable[[str], str],
        delay_ms: int = 500,
    ):
        self.tree = tree
        self.message_for_item = message_for_item
        self.delay_ms = delay_ms
        self.after_id: Optional[str] = None
        self.window: Optional[tk.Toplevel] = None
        self.candidate: Optional[tuple] = None

        columns = list(tree.cget("columns"))
        self.message_column = "message"
        self.message_column_number = f"#{columns.index(self.message_column) + 1}"
        font_spec = ttk.Style(tree).lookup("Treeview", "font") or "TkDefaultFont"
        try:
            self.font = tkfont.nametofont(font_spec)
        except tk.TclError:
            self.font = tkfont.Font(font=font_spec)

        tree.bind("<Motion>", self._on_motion, add="+")
        tree.bind("<Leave>", self.hide, add="+")
        tree.bind("<ButtonPress>", self.hide, add="+")
        tree.bind("<MouseWheel>", self.hide, add="+")
        tree.bind("<Configure>", self.hide, add="+")

    def _on_motion(self, event) -> None:
        if (
            self.tree.identify_region(event.x, event.y) != "cell"
            or self.tree.identify_column(event.x) != self.message_column_number
        ):
            self.hide()
            return

        item_id = self.tree.identify_row(event.y)
        if not item_id:
            self.hide()
            return
        bbox = self.tree.bbox(item_id, self.message_column)
        if not bbox:
            self.hide()
            return
        displayed = str(self.tree.set(item_id, self.message_column))
        full_message = self.message_for_item(item_id)
        if not message_needs_tooltip(
            full_message, displayed, bbox[2], self.font.measure
        ):
            self.hide()
            return

        candidate = (item_id, displayed, full_message)
        if candidate == self.candidate:
            return
        self.hide()
        self.candidate = candidate
        self.after_id = self.tree.after(
            self.delay_ms,
            lambda: self._show(candidate, event.x_root + 12, event.y_root + 16),
        )

    def _show(self, candidate: tuple, x: int, y: int) -> None:
        self.after_id = None
        if candidate != self.candidate or not self.tree.winfo_exists():
            return
        full_message = candidate[2]
        self.window = tk.Toplevel(self.tree)
        self.window.wm_overrideredirect(True)
        self.window.wm_attributes("-topmost", True)
        label = tk.Label(
            self.window,
            text=full_message,
            justify="left",
            anchor="w",
            background="#ffffe0",
            foreground="#000000",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=4,
            wraplength=640,
        )
        label.pack()
        self.window.update_idletasks()
        width = self.window.winfo_reqwidth()
        height = self.window.winfo_reqheight()
        x = min(x, max(0, self.window.winfo_screenwidth() - width - 4))
        if y + height > self.window.winfo_screenheight():
            y = max(0, y - height - 28)
        self.window.wm_geometry(f"+{x}+{y}")

    def hide(self, _event=None) -> None:
        if self.after_id is not None:
            self.tree.after_cancel(self.after_id)
            self.after_id = None
        if self.window is not None:
            self.window.destroy()
            self.window = None
        self.candidate = None


class GitSvnSyncApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Git ↔ SVN Sync")
        self.root.geometry("1240x820")
        self.root.minsize(980, 660)
        self.root.configure(background=COLORS["background"])
        self.root.option_add("*tearOff", False)

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.reporter = TkQueueReporter(self.events)
        self.current_plan: Optional[sync.SyncPlan] = None
        self.rows: List[GuiPlanRow] = []
        self.busy = False
        self.activity_expanded = True
        self.activity_sash_position: Optional[int] = None

        self.preset_var = tk.StringVar(value=MANUAL_PRESET_LABEL)
        self.git_var = tk.StringVar()
        self.svn_var = tk.StringVar()
        self.dry_run_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready to scan")
        self.filter_var = tk.StringVar(value=FILTER_ALL)
        self.search_var = tk.StringVar()
        self.all_filter_text = tk.StringVar(value="All  0")
        self.diff_filter_text = tk.StringVar(value="Different  0")
        self.git_filter_text = tk.StringVar(value="Only Git  0")
        self.svn_filter_text = tk.StringVar(value="Only SVN  0")
        self.results_title_var = tk.StringVar(value="Changes")
        self.selection_var = tk.StringVar(value="No changes selected")

        self._configure_styles()
        self._build_ui()
        self.search_var.trace_add("write", lambda *_args: self._reload_tree())
        self.dry_run_var.trace_add("write", lambda *_args: self._refresh_action_states())
        self._refresh_action_states()
        self._poll_events()

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure("App.TFrame", background=COLORS["background"])
        style.configure(
            "Card.TFrame",
            background=COLORS["surface"],
            borderwidth=1,
            relief="solid",
            bordercolor=COLORS["border"],
        )
        style.configure("App.TLabel", background=COLORS["background"], foreground=COLORS["text"])
        style.configure("Card.TLabel", background=COLORS["surface"], foreground=COLORS["text"])
        style.configure(
            "Title.App.TLabel",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=("TkDefaultFont", 19, "bold"),
        )
        style.configure(
            "Subtitle.App.TLabel",
            background=COLORS["background"],
            foreground=COLORS["muted"],
            font=("TkDefaultFont", 10),
        )
        style.configure(
            "Section.Card.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            font=("TkDefaultFont", 12, "bold"),
        )
        style.configure(
            "Muted.Card.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["muted"],
        )
        style.configure(
            "GitChip.Card.TLabel",
            background="#DBEAFE",
            foreground="#1D4ED8",
            font=("TkDefaultFont", 9, "bold"),
            padding=(8, 4),
        )
        style.configure(
            "SvnChip.Card.TLabel",
            background="#EDE9FE",
            foreground="#6D28D9",
            font=("TkDefaultFont", 9, "bold"),
            padding=(8, 4),
        )
        style.configure(
            "DryRun.TCheckbutton",
            background=COLORS["background"],
            foreground=COLORS["warning"],
            font=("TkDefaultFont", 9, "bold"),
            padding=(10, 6),
        )
        style.map("DryRun.TCheckbutton", background=[("active", COLORS["background"])])

        for name, color, hover in (
            ("Primary", COLORS["primary"], COLORS["primary_hover"]),
            ("Success", COLORS["success"], COLORS["success_hover"]),
        ):
            style.configure(
                f"{name}.TButton",
                background=color,
                foreground="#FFFFFF",
                borderwidth=0,
                focusthickness=0,
                padding=(16, 9),
                font=("TkDefaultFont", 10, "bold"),
            )
            style.map(
                f"{name}.TButton",
                background=[("active", hover), ("disabled", "#AAB6C5")],
                foreground=[("disabled", "#EEF2F7")],
            )
        style.configure(
            "Secondary.TButton",
            background=COLORS["surface_muted"],
            foreground=COLORS["text"],
            borderwidth=1,
            bordercolor=COLORS["border"],
            padding=(13, 8),
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#E8EEF6"), ("disabled", "#F1F5F9")],
            foreground=[("disabled", "#94A3B8")],
        )
        style.configure(
            "Ghost.TButton",
            background=COLORS["surface"],
            foreground=COLORS["muted"],
            borderwidth=0,
            padding=(9, 6),
        )
        style.map("Ghost.TButton", background=[("active", COLORS["surface_muted"])])
        style.configure(
            "Filter.Toolbutton",
            background=COLORS["surface"],
            foreground=COLORS["muted"],
            borderwidth=1,
            bordercolor=COLORS["border"],
            padding=(11, 6),
        )
        style.map(
            "Filter.Toolbutton",
            background=[("selected", COLORS["selection"]), ("active", COLORS["surface_muted"])],
            foreground=[("selected", COLORS["primary"])],
        )
        style.configure(
            "Modern.TEntry",
            fieldbackground=COLORS["surface"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            padding=7,
        )
        style.configure(
            "Modern.TCombobox",
            fieldbackground=COLORS["surface"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            padding=6,
        )
        style.configure(
            "Modern.Treeview",
            background=COLORS["surface"],
            fieldbackground=COLORS["surface"],
            foreground=COLORS["text"],
            borderwidth=0,
            rowheight=34,
            font=("TkDefaultFont", 10),
        )
        style.map(
            "Modern.Treeview",
            background=[("selected", COLORS["selection"])],
            foreground=[("selected", COLORS["text"])],
        )
        style.configure(
            "Modern.Treeview.Heading",
            background=COLORS["surface_muted"],
            foreground="#475569",
            borderwidth=0,
            relief="flat",
            padding=(8, 9),
            font=("TkDefaultFont", 9, "bold"),
        )
        style.map("Modern.Treeview.Heading", background=[("active", "#EEF2F7")])
        style.configure(
            "Modern.Horizontal.TProgressbar",
            background=COLORS["primary"],
            troughcolor=COLORS["border"],
            borderwidth=0,
            thickness=3,
        )

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.shell = ttk.Frame(
            self.root, style="App.TFrame", padding=(18, 14, 18, 16)
        )
        self.shell.grid(row=0, column=0, sticky="nsew")
        self.shell.columnconfigure(0, weight=1)
        self.shell.rowconfigure(2, weight=1)

        self._build_header()
        self._build_repository_card()
        self.content_pane = tk.PanedWindow(
            self.shell,
            orient=tk.VERTICAL,
            background="#CBD5E1",
            borderwidth=0,
            relief="flat",
            sashwidth=7,
            sashrelief="flat",
            showhandle=True,
            handlesize=12,
            handlepad=10,
            opaqueresize=True,
        )
        self.content_pane.grid(row=2, column=0, sticky="nsew")
        self._build_results_card()
        self._build_activity_card()
        self.root.after_idle(self._set_initial_panel_ratio)

    def _build_header(self) -> None:
        header = ttk.Frame(self.shell, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Git ↔ SVN Sync", style="Title.App.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="Review and synchronize changes between working copies",
            style="Subtitle.App.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.dry_run_toggle = ttk.Checkbutton(
            header,
            text="DRY RUN",
            variable=self.dry_run_var,
            style="DryRun.TCheckbutton",
        )
        self.dry_run_toggle.grid(row=0, column=1, rowspan=2, sticky="e")

    def _build_repository_card(self) -> None:
        card = ttk.Frame(self.shell, style="Card.TFrame", padding=(18, 14))
        card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        card.columnconfigure(1, weight=1)
        card.columnconfigure(2, weight=1)
        ttk.Label(card, text="Repository pair", style="Section.Card.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(
            card,
            text="Choose a preset or enter two working-copy paths",
            style="Muted.Card.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))

        ttk.Label(card, text="Preset", style="Muted.Card.TLabel").grid(
            row=0, column=2, sticky="e", padx=(12, 8)
        )
        preset_values = [MANUAL_PRESET_LABEL] + list(sync.PRESETS.keys())
        self.preset = ttk.Combobox(
            card,
            textvariable=self.preset_var,
            values=preset_values,
            state="readonly",
            width=20,
            style="Modern.TCombobox",
        )
        self.preset.grid(row=0, column=3, sticky="e")
        self.preset.bind("<<ComboboxSelected>>", self._on_preset_changed)

        ttk.Label(card, text="GIT", style="GitChip.Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=(0, 7)
        )
        self.git_entry = ttk.Entry(card, textvariable=self.git_var, style="Modern.TEntry")
        self.git_entry.grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(10, 8), pady=(0, 7)
        )
        self.git_browse_button = ttk.Button(
            card,
            text="Browse",
            command=lambda: self._browse(self.git_var),
            style="Secondary.TButton",
        )
        self.git_browse_button.grid(row=2, column=3, sticky="e", pady=(0, 7))

        ttk.Label(card, text="SVN", style="SvnChip.Card.TLabel").grid(
            row=3, column=0, sticky="w"
        )
        self.svn_entry = ttk.Entry(card, textvariable=self.svn_var, style="Modern.TEntry")
        self.svn_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(10, 8))
        self.svn_browse_button = ttk.Button(
            card,
            text="Browse",
            command=lambda: self._browse(self.svn_var),
            style="Secondary.TButton",
        )
        self.svn_browse_button.grid(row=3, column=3, sticky="e")

        action_bar = ttk.Frame(card, style="Card.TFrame")
        action_bar.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(13, 0))
        action_bar.columnconfigure(5, weight=1)
        self.scan_button = ttk.Button(
            action_bar, text="Scan", command=self.scan, style="Primary.TButton"
        )
        self.scan_button.grid(row=0, column=0, sticky="w")
        self.run_button = ttk.Button(
            action_bar,
            text="Run selected",
            command=self.run_selected,
            style="Success.TButton",
        )
        self.run_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.update_git_button = ttk.Button(
            action_bar,
            text="Update GIT",
            command=self.update_git,
            style="Secondary.TButton",
        )
        self.update_git_button.grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.update_button = ttk.Button(
            action_bar,
            text="Update SVN",
            command=self.update_svn,
            style="Secondary.TButton",
        )
        self.update_button.grid(row=0, column=3, sticky="w", padx=(8, 0))
        maintenance_menu = tk.Menu(self.root)
        maintenance_menu.add_command(
            label="Rebaseline ignore file", command=self.rebaseline
        )
        self.more_button = ttk.Menubutton(
            action_bar,
            text="More ▾",
            menu=maintenance_menu,
            style="Secondary.TButton",
        )
        self.more_button.grid(row=0, column=4, sticky="w", padx=(8, 0))
        self.progress = ttk.Progressbar(
            action_bar,
            mode="indeterminate",
            length=150,
            style="Modern.Horizontal.TProgressbar",
        )
        self.progress.grid(row=0, column=6, sticky="e", padx=(12, 0))
        self.progress.grid_remove()
        ttk.Label(
            action_bar, textvariable=self.status_var, style="Muted.Card.TLabel"
        ).grid(row=0, column=7, sticky="e", padx=(12, 0))

    def _build_results_card(self) -> None:
        card = ttk.Frame(self.content_pane, style="Card.TFrame")
        self.results_card = card
        self.content_pane.add(card, minsize=170, stretch="always")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(2, weight=1)

        header = ttk.Frame(card, style="Card.TFrame", padding=(16, 13, 16, 8))
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header, textvariable=self.results_title_var, style="Section.Card.TLabel"
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header, textvariable=self.selection_var, style="Muted.Card.TLabel"
        ).grid(row=0, column=1, sticky="e")

        filter_bar = ttk.Frame(card, style="Card.TFrame", padding=(16, 0, 16, 10))
        filter_bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        filter_bar.columnconfigure(5, weight=1)
        filters = (
            (self.all_filter_text, FILTER_ALL),
            (self.diff_filter_text, FILTER_DIFF),
            (self.git_filter_text, FILTER_ONLY_GIT),
            (self.svn_filter_text, FILTER_ONLY_SVN),
        )
        self.filter_buttons = []
        for column, (text_var, value) in enumerate(filters):
            button = ttk.Radiobutton(
                filter_bar,
                textvariable=text_var,
                variable=self.filter_var,
                value=value,
                command=self._reload_tree,
                style="Filter.Toolbutton",
            )
            button.grid(row=0, column=column, sticky="w", padx=(0, 6))
            self.filter_buttons.append(button)
        ttk.Label(filter_bar, text="Search", style="Muted.Card.TLabel").grid(
            row=0, column=6, sticky="e", padx=(8, 7)
        )
        self.search_entry = ttk.Entry(
            filter_bar,
            textvariable=self.search_var,
            width=28,
            style="Modern.TEntry",
        )
        self.search_entry.grid(row=0, column=7, sticky="e")

        columns = ("selected", "status", "action", "path", "author", "message")
        self.tree = ttk.Treeview(
            card,
            columns=columns,
            show="headings",
            selectmode="extended",
            style="Modern.Treeview",
        )
        headings = {
            "selected": "SEL",
            "status": "STATUS",
            "action": "DIRECTION",
            "path": "PATH",
            "author": "AUTHOR",
            "message": "MESSAGE",
        }
        widths = {
            "selected": (52, False),
            "status": (112, False),
            "action": (125, False),
            "path": (330, True),
            "author": (125, False),
            "message": (380, True),
        }
        for column in columns:
            width, stretch = widths[column]
            self.tree.heading(column, text=headings[column], anchor="w")
            self.tree.column(
                column,
                width=width,
                minwidth=width if not stretch else 140,
                anchor="center" if column == "selected" else "w",
                stretch=stretch,
            )
        self.tree.grid(row=2, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(card, orient="vertical", command=self.tree.yview)
        yscroll.grid(row=2, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.tag_configure("even", background=COLORS["surface"])
        self.tree.tag_configure("odd", background="#FAFCFF")
        self.tree.bind("<Double-1>", self._toggle_selected)
        self.tree.bind("<space>", self._toggle_selected)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._refresh_action_states())
        self.message_tooltip = TreeMessageTooltip(self.tree, self._message_tooltip_text)

        result_actions = ttk.Frame(card, style="Card.TFrame", padding=(12, 8))
        result_actions.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.toggle_selected_button = ttk.Button(
            result_actions,
            text="Toggle selected",
            command=self._toggle_selected,
            style="Ghost.TButton",
        )
        self.toggle_selected_button.pack(side="left")
        self.toggle_action_button = ttk.Button(
            result_actions,
            text="Switch add / remove",
            command=self._toggle_action,
            style="Ghost.TButton",
        )
        self.toggle_action_button.pack(side="left", padx=(5, 0))

    def _build_activity_card(self) -> None:
        self.activity_card = ttk.Frame(self.content_pane, style="Card.TFrame")
        self.content_pane.add(self.activity_card, minsize=52, stretch="always")
        self.activity_card.columnconfigure(0, weight=1)
        self.activity_card.rowconfigure(1, weight=1)
        header = ttk.Frame(self.activity_card, style="Card.TFrame", padding=(16, 9))
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Activity", style="Section.Card.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            header, text="Clear", command=self._clear_log, style="Ghost.TButton"
        ).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(
            header, text="Copy", command=self._copy_log, style="Ghost.TButton"
        ).grid(row=0, column=2, padx=(2, 0))
        self.activity_toggle_button = ttk.Button(
            header,
            text="Hide",
            command=self._toggle_activity,
            style="Ghost.TButton",
        )
        self.activity_toggle_button.grid(row=0, column=3, padx=(2, 0))

        self.activity_body = ttk.Frame(self.activity_card, style="Card.TFrame")
        self.activity_body.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.activity_body.columnconfigure(0, weight=1)
        self.activity_body.rowconfigure(0, weight=1)
        self.log = tk.Text(
            self.activity_body,
            height=9,
            wrap="none",
            font=("TkFixedFont", 10),
            background=COLORS["console"],
            foreground=COLORS["console_text"],
            insertbackground="#FFFFFF",
            selectbackground="#334155",
            borderwidth=0,
            highlightthickness=0,
            padx=10,
            pady=8,
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        log_y = ttk.Scrollbar(self.activity_body, orient="vertical", command=self.log.yview)
        log_y.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_y.set)

    def _config(self, rebaseline: bool = False) -> sync.SyncConfig:
        preset = self.preset_var.get()
        preset_name = None if preset == MANUAL_PRESET_LABEL else preset
        return sync.SyncConfig(
            git_root=None if preset_name else self.git_var.get().strip(),
            svn_root=None if preset_name else self.svn_var.get().strip(),
            preset_name=preset_name,
            dry_run=self.dry_run_var.get(),
            rebaseline=rebaseline,
            auto_yes=True,
        )

    def _browse(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory()
        if path:
            var.set(path)

    def _on_preset_changed(self, _event=None) -> None:
        preset_name = self.preset_var.get()
        if preset_name == MANUAL_PRESET_LABEL:
            self.git_entry.configure(state="normal")
            self.svn_entry.configure(state="normal")
            if hasattr(self, "git_browse_button"):
                self.git_browse_button.configure(state="normal")
                self.svn_browse_button.configure(state="normal")
            return
        preset = sync.PRESETS[preset_name]
        self.git_var.set(preset.git_root)
        self.svn_var.set(preset.svn_root)
        self.git_entry.configure(state="disabled")
        self.svn_entry.configure(state="disabled")
        if hasattr(self, "git_browse_button"):
            self.git_browse_button.configure(state="disabled")
            self.svn_browse_button.configure(state="disabled")

    def scan(self) -> None:
        if self._start_worker("Scanning..."):
            # A new scan invalidates any previously executable plan, even if
            # the new scan later reports an error.
            self.current_plan = None
            config = self._config(rebaseline=False)
            scan_reporter = ScanCommandReporter(self.events)
            self._run_worker(
                lambda: self.events.put((
                    "plan",
                    sync.prepare_sync_plan(config, scan_reporter),
                ))
            )

    def rebaseline(self) -> None:
        if self._start_worker("Rebaselining..."):
            config = self._config(rebaseline=True)

            def work() -> None:
                plan = sync.prepare_sync_plan(config, self.reporter)
                added = sync.apply_rebaseline_plan(plan, self.reporter)
                self.events.put(("rebaseline_done", plan, added))

            self._run_worker(work)

    def update_git(self) -> None:
        if self._start_worker("Updating GIT..."):
            config = self._config(rebaseline=False)

            def work() -> None:
                sync.safe_git_update_from_config(config, self.reporter)
                self.events.put(("git_update_done",))

            self._run_worker(work)

    def update_svn(self) -> None:
        if self._start_worker("Updating SVN..."):
            config = self._config(rebaseline=False)

            def work() -> None:
                sync.safe_svn_update_from_config(config, self.reporter)
                self.events.put(("svn_update_done",))

            self._run_worker(work)

    def run_selected(self) -> None:
        if not self.current_plan:
            messagebox.showinfo("git-svn-sync", "Scan before running selected operations.")
            return
        operations = selected_operations(self.rows)
        if not operations:
            messagebox.showinfo("git-svn-sync", "No selected operations to run.")
            return
        if self._start_worker("Running selected operations..."):
            plan = dataclasses.replace(
                self.current_plan,
                config=dataclasses.replace(
                    self.current_plan.config,
                    dry_run=self.dry_run_var.get(),
                ),
            )

            def work() -> None:
                sync.execute_plan_operations(plan, operations, self.reporter)
                self.events.put(("run_done",))

            self._run_worker(work)

    def _start_worker(self, status: str) -> bool:
        if self.busy:
            return False
        self.busy = True
        self.status_var.set(status)
        if hasattr(self, "progress"):
            self.progress.grid()
            self.progress.start(12)
        self._refresh_action_states()
        return True

    def _finish_worker(self, status: str) -> None:
        self.busy = False
        self.status_var.set(status)
        if hasattr(self, "progress"):
            self.progress.stop()
            self.progress.grid_remove()
        self._refresh_action_states()

    def _run_worker(self, target) -> None:
        def wrapped() -> None:
            try:
                with sync.gui_askpass_context():
                    target()
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=wrapped, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "message":
            _kind, stream, text = event
            self._append_log(text + "\n")
        elif kind == "command":
            self._append_command(event[1])
        elif kind == "file_operation":
            self._append_file_operation(event[1])
        elif kind == "plan":
            self.current_plan = event[1]
            self._load_plan(self.current_plan)
            self._finish_worker(
                f"Scan complete: {len(self.rows)} item(s), dry run {'on' if self.current_plan.config.dry_run else 'off'}"
            )
        elif kind == "rebaseline_done":
            _kind, plan, added = event
            self.current_plan = plan
            self.rows = []
            self._reload_tree()
            self._finish_worker(f"Rebaseline complete: {len(added)} path(s)")
        elif kind == "run_done":
            if hasattr(self, "dry_run_var") and not self.dry_run_var.get():
                self.current_plan = None
            self._finish_worker("Run complete")
        elif kind == "svn_update_done":
            self.current_plan = None
            self._finish_worker("SVN update complete; scan again when ready")
        elif kind == "git_update_done":
            self.current_plan = None
            self._finish_worker("Git update complete; scan again when ready")
        elif kind == "error":
            self._finish_worker("Error")
            message = event[1]
            self._append_log(f"ERROR: {message}\n")
            if is_svn_update_required_error(message):
                self._show_svn_update_error(message)
            elif is_git_update_required_error(message):
                self._show_git_update_error(message)
            else:
                messagebox.showerror("git-svn-sync", message)

    def _show_svn_update_error(self, message: str) -> None:
        self._show_update_error(
            title="SVN Update Required",
            message=message,
            safety_text=(
                "Update SVN will first run svn status. If tracked local changes "
                "are found, it will refuse to run svn update."
            ),
            button_text="Update SVN",
            action=self.update_svn,
        )

    def _show_git_update_error(self, message: str) -> None:
        self._show_update_error(
            title="Git Update Required",
            message=message,
            safety_text=(
                "Update GIT will first inspect git status. If tracked or staged "
                "local changes are found, it will refuse to pull. The pull is "
                "restricted to fast-forward updates."
            ),
            button_text="Update GIT",
            action=self.update_git,
        )

    def _show_update_error(
        self,
        title: str,
        message: str,
        safety_text: str,
        button_text: str,
        action: Callable[[], None],
    ) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.columnconfigure(0, weight=1)

        body = ttk.Frame(dialog, padding=12)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)

        ttk.Label(
            body,
            text=message,
            wraplength=560,
            justify="left",
        ).grid(row=0, column=0, sticky="ew")
        ttk.Label(
            body,
            text=safety_text,
            wraplength=560,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(10, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=2, column=0, sticky="e", pady=(14, 0))

        def run_update() -> None:
            dialog.destroy()
            action()

        ttk.Button(buttons, text=button_text, command=run_update).pack(side="right")
        ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side="right", padx=(0, 8))

        dialog.grab_set()
        dialog.wait_visibility()
        dialog.focus_set()

    def _load_plan(self, plan: sync.SyncPlan) -> None:
        self.rows = [
            GuiPlanRow(item, selected=item.suggested_operation is not None)
            for item in plan.items
        ]
        self.filter_var.set(FILTER_ALL)
        self.search_var.set("")
        self._update_result_summary()
        self._reload_tree()

    def _reload_tree(self) -> None:
        if not hasattr(self, "tree"):
            return
        self.message_tooltip.hide()
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        visible_index = 0
        for index, row in enumerate(self.rows):
            if not row_matches_filter(
                row, self.filter_var.get(), self.search_var.get()
            ):
                continue
            tag = "even" if visible_index % 2 == 0 else "odd"
            self.tree.insert(
                "", "end", iid=str(index), values=row_values(row), tags=(tag,)
            )
            visible_index += 1
        self.results_title_var.set(
            f"Changes  ·  {visible_index} shown"
            if self.rows else "Changes"
        )
        self._refresh_action_states()

    def _message_tooltip_text(self, item_id: str) -> str:
        try:
            operation = self.rows[int(item_id)].operation
        except (ValueError, IndexError):
            return ""
        return operation.message if operation else ""

    def _toggle_selected(self, _event=None) -> None:
        for item_id in self.tree.selection():
            row = self.rows[int(item_id)]
            row.selected = not row.selected
            self.tree.item(item_id, values=row_values(row))
        self._update_result_summary()
        self._refresh_action_states()

    def _toggle_action(self) -> None:
        for item_id in self.tree.selection():
            row = self.rows[int(item_id)]
            if row.item.alternate_operation:
                row.use_alternate = not row.use_alternate
                self.tree.item(item_id, values=row_values(row))
        self._refresh_action_states()

    def _update_result_summary(self) -> None:
        diff_count = sum(row.item.kind == FILTER_DIFF for row in self.rows)
        git_count = sum(row.item.kind == FILTER_ONLY_GIT for row in self.rows)
        svn_count = sum(row.item.kind == FILTER_ONLY_SVN for row in self.rows)
        selected_count = len(selected_operations(self.rows))
        self.all_filter_text.set(f"All  {len(self.rows)}")
        self.diff_filter_text.set(f"Different  {diff_count}")
        self.git_filter_text.set(f"Only Git  {git_count}")
        self.svn_filter_text.set(f"Only SVN  {svn_count}")
        self.selection_var.set(
            f"{selected_count} selected" if selected_count else "No changes selected"
        )
        if hasattr(self, "run_button"):
            self.run_button.configure(
                text=f"Run {selected_count} selected" if selected_count else "Run selected"
            )

    def _refresh_action_states(self) -> None:
        if not hasattr(self, "scan_button"):
            return
        selected_count = len(selected_operations(self.rows))
        normal_if_idle = "disabled" if self.busy else "normal"
        self.scan_button.configure(state=normal_if_idle)
        self.update_git_button.configure(state=normal_if_idle)
        self.update_button.configure(state=normal_if_idle)
        self.more_button.configure(state=normal_if_idle)
        self.dry_run_toggle.configure(state=normal_if_idle)
        self.preset.configure(state="disabled" if self.busy else "readonly")
        manual_paths = self.preset_var.get() == MANUAL_PRESET_LABEL
        path_state = "normal" if not self.busy and manual_paths else "disabled"
        self.git_entry.configure(state=path_state)
        self.svn_entry.configure(state=path_state)
        self.git_browse_button.configure(state=path_state)
        self.svn_browse_button.configure(state=path_state)
        self.search_entry.configure(state=normal_if_idle)
        for button in self.filter_buttons:
            button.configure(state=normal_if_idle)
        self.run_button.configure(
            state="normal"
            if not self.busy and self.current_plan is not None and selected_count
            else "disabled"
        )
        has_tree_selection = bool(self.tree.selection()) and not self.busy
        self.toggle_selected_button.configure(
            state="normal" if has_tree_selection else "disabled"
        )
        can_switch = has_tree_selection and any(
            self.rows[int(item_id)].item.alternate_operation is not None
            for item_id in self.tree.selection()
        )
        self.toggle_action_button.configure(
            state="normal" if can_switch else "disabled"
        )

    def _set_initial_panel_ratio(self) -> None:
        if not hasattr(self, "content_pane") or len(self.content_pane.panes()) < 2:
            return
        height = self.content_pane.winfo_height()
        if height > 1:
            self.content_pane.sash_place(0, 0, int(height * 0.68))

    def _restore_activity_sash(self) -> None:
        height = self.content_pane.winfo_height()
        if height <= 1:
            return
        position = self.activity_sash_position
        if position is None:
            position = int(height * 0.68)
        self.content_pane.sash_place(0, 0, min(position, height - 52))

    def _collapse_activity_sash(self) -> None:
        height = self.content_pane.winfo_height()
        if height > 1:
            self.content_pane.sash_place(0, 0, max(0, height - 52))

    def _toggle_activity(self) -> None:
        self.activity_expanded = not self.activity_expanded
        if self.activity_expanded:
            self.activity_body.grid()
            self.activity_toggle_button.configure(text="Hide")
            self.root.after_idle(self._restore_activity_sash)
        else:
            if len(self.content_pane.panes()) >= 2:
                self.activity_sash_position = self.content_pane.sash_coord(0)[1]
            self.activity_body.grid_remove()
            self.activity_toggle_button.configure(text="Show")
            self.root.after_idle(self._collapse_activity_sash)

    def _append_log(self, text: str) -> None:
        self.log.insert("end", text)
        self.log.see("end")

    def _append_command(self, event: sync.CommandEvent) -> None:
        prefix = "[dry-run] " if event.dry_run else ""
        if event.planned:
            prefix += "planned "
        cwd = f" (cwd: {event.cwd})" if event.cwd else ""
        self._append_log(f"{prefix}$ {sync.format_command(event.cmd)}{cwd}\n")
        if event.stdout:
            self._append_log(event.stdout.rstrip() + "\n")
        if event.stderr:
            self._append_log(event.stderr.rstrip() + "\n")
        if event.returncode is not None:
            self._append_log(f"exit code: {event.returncode}\n")

    def _append_file_operation(self, event: sync.FileOperationEvent) -> None:
        prefix = "[dry-run] " if event.dry_run else ""
        if event.planned:
            prefix += "planned "
        if event.source and event.destination:
            self._append_log(f"{prefix}{event.action} {event.source} -> {event.destination}\n")
        else:
            self._append_log(f"{prefix}{event.action} {event.relpath}\n")

    def _clear_log(self) -> None:
        self.log.delete("1.0", "end")

    def _copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end-1c"))


def launch_gui() -> None:
    root = tk.Tk()
    GitSvnSyncApp(root)
    root.mainloop()


def main(argv: Optional[List[str]] = None):
    if argv is None:
        argv = sys.argv[1:]
    if os.environ.get(ASKPASS_ENV) == "1":
        return show_askpass_dialog(argv[0] if argv else "Password:")
    if not argv:
        launch_gui()
        return

    parser = argparse.ArgumentParser(description="Sync files between Git and SVN working copies.")
    parser.add_argument("-git", help="Path to Git working copy root")
    parser.add_argument("-svn", help="Path to SVN working copy root")
    for name, preset in PRESETS.items():
        parser.add_argument(
            f"-{name}",
            action="store_true",
            help=f"Shortcut for -git {preset.git_root} -svn {preset.svn_root}",
        )
    parser.add_argument("-yes", action="store_true", help="Assume 'yes' for all prompts (non-interactive)")
    parser.add_argument("-dry-run", action="store_true", help="Show what would happen without changing anything")
    parser.add_argument(
        "-rebaseline",
        action="store_true",
        help="Update ignore list with files present only in one repo and exit",
    )
    args = parser.parse_args(argv)

    chosen_preset_name = None
    for name in PRESETS:
        if getattr(args, name):
            if chosen_preset_name is not None:
                parser.error("Multiple preset options specified; choose only one")
            chosen_preset_name = name

    config = SyncConfig(
        git_root=args.git,
        svn_root=args.svn,
        preset_name=chosen_preset_name,
        dry_run=args.dry_run,
        rebaseline=args.rebaseline,
        auto_yes=args.yes,
    )
    try:
        run_sync(config, reporter=ConsoleReporter(show_commands=False))
    except ValueError as e:
        parser.error(str(e))
    except SyncError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
