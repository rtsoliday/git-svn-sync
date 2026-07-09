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
import hashlib
import datetime
import contextlib
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

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
    List versioned files in an SVN working copy by calling `svn list -R`.
    This returns repository entries relative to the given path.
    """
    cp = run(["svn", "list", "-R", "."], cwd=svn_root)
    files: Set[str] = set()
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line or line.endswith("/"):
            # Directories (svn list outputs directories with trailing slash)
            continue
        files.add(line)
    return files

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
        cmd = ["svn", "log", "--reverse"]
        if since_ts is not None and since_ts >= 0:
            cmd.extend(["-r", f"{{{utc_iso_from_epoch(since_ts + 1)}}}:HEAD"])
        cmd.extend(["--", relpath])
        cp = run(cmd, cwd=svn_root)
        return extract_svn_log_messages(cp.stdout)
    except subprocess.CalledProcessError:
        return []

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
    svn_set: Set[str]
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
                svn_ts, svn_msg, svn_author = svn_last_change(svn_root, rel)

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
        for relpath in copy_paths:
            cmd = ["svn", "add", "--", relpath]
            emit_message(
                f"[dry-run] {format_command(cmd)}  (if not already versioned)"
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

    for relpath in copy_paths:
        run(["svn", "add", "--", relpath], cwd=svn_root, check=False)
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


def operation_for_mismatch(st: FileStatus) -> Optional[SyncOperation]:
    sides = mismatch_sides(st)
    if sides is None:
        return None
    newer, older, _newer_ts, _older_ts, newer_msg, newer_author = sides
    if newer == "git":
        commit_msg = augment_message(
            newer_msg or f"Sync {st.relpath} from Git", newer_author
        )
        return SyncOperation(st.relpath, "svn", "copy", commit_msg)
    commit_msg = augment_message(
        newer_msg or f"Sync {st.relpath} from SVN", newer_author
    )
    return SyncOperation(st.relpath, "git", "copy", commit_msg)


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

    newer, older, newer_ts, older_ts, newer_msg, newer_author = sides
    commit_msg_base = newer_msg

    emit_message(f"\nDIFF: {rel}")
    emit_message(f"  Last change: {newer.upper()} is newer ({newer_ts}), {older.upper()} older ({older_ts})")
    author_str = f" by {newer_author}" if newer_author else ""
    emit_message(f"  Commit message ({newer.upper()}{author_str}):\n    {indent_message(commit_msg_base)}")

    if prompt_yes_no(f"Sync {rel}? Queue copy {newer.upper()} -> {older.upper()} with that message.", default_yes=True, auto_yes=auto_yes):
        return operation_for_mismatch(st)
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
            op = operation_for_mismatch(st)
            note = "" if op else "No Git or SVN timestamp could be read."
            items.append(PlanItem(st.relpath, "diff", st, op, None, note))
        elif st.in_git and not st.in_svn:
            items.append(
                PlanItem(
                    st.relpath,
                    "only_git",
                    st,
                    add_operation_for_only(st.relpath, "git", git_root, svn_root),
                    remove_operation_for_only(st.relpath, "git", git_root, svn_root),
                )
            )
        elif st.in_svn and not st.in_git:
            items.append(
                PlanItem(
                    st.relpath,
                    "only_svn",
                    st,
                    add_operation_for_only(st.relpath, "svn", git_root, svn_root),
                    remove_operation_for_only(st.relpath, "svn", git_root, svn_root),
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
        git_set, svn_set = build_index(git_root, svn_root)
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

        status = compare_and_collect(git_root, svn_root, git_set, svn_set)
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


def execute_plan_operations(
    plan: SyncPlan,
    operations: Iterable[SyncOperation],
    reporter: Optional[CommandReporter] = None,
) -> None:
    with workflow_context(reporter, plan.config.dry_run):
        execute_operation_groups(
            operations,
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


def launch_gui() -> None:
    import git_svn_sync_gui

    git_svn_sync_gui.main()


def main(argv: Optional[List[str]] = None):
    if argv is None:
        argv = sys.argv[1:]
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
