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
       - Determines which repo has the most recent change and fetches its last commit message.
       - Prompts to copy newer -> older and commit using the same message.
  4) For files present in only one repo:
       - Prompts to add to the other repo (default) or remove from the current repo, and commits.

Safety:
  - Only acts on files tracked by each VCS.
  - Per-file confirmation unless --yes is given.
  - Supports --dry-run.

Usage:
  python git_svn_sync.py --git /path/to/git_wc --svn /path/to/svn_wc [--yes] [--dry-run]
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

# ----- Utilities -----

def run(cmd: List[str], cwd: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return the CompletedProcess. Raises on error if check=True."""
    return subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

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

def git_last_change(git_root: str, relpath: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Return (timestamp_epoch, message) for the last commit that touched relpath.
    Returns (None, None) if file has no history (e.g., not tracked).
    """
    try:
        t = run(["git", "log", "-1", "--format=%ct", "--", relpath], cwd=git_root).stdout.strip()
        msg = run(["git", "log", "-1", "--pretty=%B", "--", relpath], cwd=git_root).stdout.strip()
        if not t:
            return None, None
        return int(t), msg
    except subprocess.CalledProcessError:
        return None, None

def git_add_commit(git_root: str, relpath: str, message: str, dry_run: bool):
    if dry_run:
        print(f"[dry-run] git add -- {relpath}")
        print(f"[dry-run] git commit -m {message!r} -- {relpath}")
        return
    run(["git", "add", "--", relpath], cwd=git_root)
    run(["git", "commit", "-m", message, "--", relpath], cwd=git_root)

def git_rm_commit(git_root: str, relpath: str, message: str, dry_run: bool):
    if dry_run:
        print(f"[dry-run] git rm -- {relpath}")
        print(f"[dry-run] git commit -m {message!r} -- {relpath}")
        return
    run(["git", "rm", "--", relpath], cwd=git_root)
    run(["git", "commit", "-m", message, "--", relpath], cwd=git_root)

# ----- SVN helpers -----

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

def svn_last_change(svn_root: str, relpath: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Return (timestamp_epoch, message) for the last change that touched relpath in SVN.
    Uses `svn info --show-item last-changed-date` (SVN 1.9+) for time,
    and `svn log -l 1` for message.
    """
    try:
        # Timestamp
        cp_info = run(["svn", "info", "--show-item", "last-changed-date", "--", relpath], cwd=svn_root)
        date_str = cp_info.stdout.strip()
        if not date_str:
            return None, None
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
        return ts, message
    except subprocess.CalledProcessError:
        return None, None

def extract_last_svn_log_message(log_output: str) -> str:
    """
    Parses `svn log -l 1` output to pull the commit message (between the first dashed separators).
    """
    lines = [l.rstrip("\n") for l in log_output.splitlines()]
    sep_indices = [i for i, l in enumerate(lines) if l.startswith("-" * 5)]
    if len(sep_indices) >= 2:
        start = sep_indices[0] + 2  # line after header line (author|date|rev)
        end = sep_indices[1]
        body = "\n".join(lines[start:end]).strip()
        return body
    # Fallback: entire output
    return log_output.strip()

def svn_add_commit(svn_root: str, relpath: str, message: str, dry_run: bool):
    if dry_run:
        print(f"[dry-run] svn add -- {relpath}  (if not already versioned)")
        print(f"[dry-run] svn commit -m {message!r} -- {relpath}")
        return
    # Try add; if already versioned, add will fail harmlessly
    try:
        run(["svn", "add", "--", relpath], cwd=svn_root, check=False)
    except Exception:
        pass
    run(["svn", "commit", "-m", message, "--", relpath], cwd=svn_root)

def svn_delete_commit(svn_root: str, relpath: str, message: str, dry_run: bool):
    if dry_run:
        print(f"[dry-run] svn delete -- {relpath}")
        print(f"[dry-run] svn commit -m {message!r} -- {relpath}")
        return
    run(["svn", "delete", "--", relpath], cwd=svn_root)
    run(["svn", "commit", "-m", message, "--", relpath], cwd=svn_root)

# ----- Core logic -----

@dataclass
class FileStatus:
    relpath: str
    in_git: bool
    in_svn: bool
    same_content: Optional[bool]  # None if not present in both
    git_ts: Optional[int]
    git_msg: Optional[str]
    svn_ts: Optional[int]
    svn_msg: Optional[str]

def build_index(git_root: str, svn_root: str) -> Tuple[Set[str], Set[str]]:
    git_set = git_ls_files(git_root)
    svn_set = svn_ls_files(svn_root)
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
        git_ts = git_msg = svn_ts = svn_msg = None

        if in_git and in_svn:
            git_abs = os.path.join(git_root, rel)
            svn_abs = os.path.join(svn_root, rel)
            if os.path.isfile(git_abs) and os.path.isfile(svn_abs):
                same = (sha256_file(git_abs) == sha256_file(svn_abs))
            else:
                # If one is a directory or missing on disk (shouldn't be if tracked), treat as different
                same = False

            if not same:
                git_ts, git_msg = git_last_change(git_root, rel)
                svn_ts, svn_msg = svn_last_change(svn_root, rel)

        status[rel] = FileStatus(
            relpath=rel,
            in_git=in_git,
            in_svn=in_svn,
            same_content=same,
            git_ts=git_ts,
            git_msg=git_msg,
            svn_ts=svn_ts,
            svn_msg=svn_msg,
        )

    return status

def copy_file(src_root: str, dst_root: str, relpath: str, dry_run: bool):
    src = os.path.join(src_root, relpath)
    dst = os.path.join(dst_root, relpath)
    if dry_run:
        print(f"[dry-run] copy {src} -> {dst}")
        return
    ensure_parent_dir(dst)
    shutil.copy2(src, dst)

def remove_file(root: str, relpath: str, dry_run: bool):
    path = os.path.join(root, relpath)
    if dry_run:
        print(f"[dry-run] remove {path}")
        return
    if os.path.exists(path):
        os.remove(path)

def handle_mismatch(
    st: FileStatus,
    git_root: str,
    svn_root: str,
    auto_yes: bool,
    dry_run: bool
):
    rel = st.relpath
    # Decide newer side
    git_ts = st.git_ts or -1
    svn_ts = st.svn_ts or -1

    if git_ts == -1 and svn_ts == -1:
        print(f"?? {rel}: content differs but no commit timestamps could be read. Skipping.")
        return

    newer = "git" if git_ts >= svn_ts else "svn"
    older = "svn" if newer == "git" else "git"
    newer_ts = git_ts if newer == "git" else svn_ts
    older_ts = svn_ts if newer == "git" else git_ts
    newer_msg = st.git_msg if newer == "git" else st.svn_msg

    print(f"\nDIFF: {rel}")
    print(f"  Last change: {newer.upper()} is newer ({newer_ts}), {older.upper()} older ({older_ts})")
    print(f"  Last commit message ({newer.upper()}):\n    {indent_message(newer_msg)}")

    if prompt_yes_no(f"Sync {rel}? Copy {newer.upper()} -> {older.upper()} and commit with that message.", default_yes=True, auto_yes=auto_yes):
        if newer == "git":
            # Copy git -> svn, then commit in SVN
            copy_file(git_root, svn_root, rel, dry_run)
            svn_add_commit(svn_root, rel, newer_msg or f"Sync {rel} from Git", dry_run)
        else:
            # Copy svn -> git, then commit in Git
            copy_file(svn_root, git_root, rel, dry_run)
            git_add_commit(git_root, rel, newer_msg or f"Sync {rel} from SVN", dry_run)
    else:
        print("  Skipped.")

def handle_only_in_one(
    rel: str,
    present_in: str,   # "git" or "svn"
    git_root: str,
    svn_root: str,
    auto_yes: bool,
    dry_run: bool
):
    other = "svn" if present_in == "git" else "git"
    print(f"\nONLY IN {present_in.upper()}: {rel}")

    # Offer to add to the other repo (default) or remove from the current repo
    do_add = prompt_yes_no(
        f"Add {rel} to {other.upper()}? (No = remove from {present_in.upper()})",
        default_yes=True, auto_yes=auto_yes
    )

    if present_in == "git":
        if do_add:
            # Add to SVN
            copy_file(git_root, svn_root, rel, dry_run)
            # Use the file's last commit message from Git if available, else a generic message
            ts, msg = git_last_change(git_root, rel)
            svn_add_commit(svn_root, rel, msg or f"Add {rel} (synced from Git)", dry_run)
        else:
            # Remove from Git
            ts, msg = git_last_change(git_root, rel)
            git_rm_commit(git_root, rel, msg or f"Remove {rel} (not present in SVN)", dry_run)
    else:
        if do_add:
            # Add to Git
            copy_file(svn_root, git_root, rel, dry_run)
            ts, msg = svn_last_change(svn_root, rel)
            git_add_commit(git_root, rel, msg or f"Add {rel} (synced from SVN)", dry_run)
        else:
            # Remove from SVN
            ts, msg = svn_last_change(svn_root, rel)
            svn_delete_commit(svn_root, rel, msg or f"Remove {rel} (not present in Git)", dry_run)

def indent_message(msg: Optional[str]) -> str:
    if not msg:
        return "(no message)"
    lines = msg.splitlines() or [msg]
    return "\n    ".join(lines)

def main():
    parser = argparse.ArgumentParser(description="Sync files between Git and SVN working copies.")
    parser.add_argument("--git", required=True, help="Path to Git working copy root")
    parser.add_argument("--svn", required=True, help="Path to SVN working copy root")
    parser.add_argument("--yes", action="store_true", help="Assume 'yes' for all prompts (non-interactive)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without changing anything")
    args = parser.parse_args()

    git_root = os.path.abspath(args.git)
    svn_root = os.path.abspath(args.svn)
    auto_yes = args.yes
    dry_run = args.dry_run

    # Sanity checks
    for root, name, probe in [
        (git_root, "Git", ["git", "rev-parse", "--is-inside-work-tree"]),
        (svn_root, "SVN", ["svn", "info"]),
    ]:
        try:
            run(probe, cwd=root)
        except subprocess.CalledProcessError as e:
            print(f"Error: {name} probe failed in {root}:\n{e.stderr}", file=sys.stderr)
            sys.exit(1)
        except FileNotFoundError:
            print(f"Error: Required tool for {name} not found on PATH.", file=sys.stderr)
            sys.exit(1)

    print("Indexing versioned files...")
    git_set, svn_set = build_index(git_root, svn_root)

    print(f"  Git tracked files: {len(git_set)}")
    print(f"  SVN tracked files: {len(svn_set)}")

    status = compare_and_collect(git_root, svn_root, git_set, svn_set)

    # 1) Handle diffs
    diffs = [s for s in status.values() if s.in_git and s.in_svn and s.same_content is False]
    # 2) Handle only-in-Git
    only_git = [s.relpath for s in status.values() if s.in_git and not s.in_svn]
    # 3) Handle only-in-SVN
    only_svn = [s.relpath for s in status.values() if s.in_svn and not s.in_git]

    print(f"\nSummary:")
    print(f"  Files that differ: {len(diffs)}")
    print(f"  Only in Git: {len(only_git)}")
    print(f"  Only in SVN: {len(only_svn)}")

    # Mismatched content
    for s in diffs:
        handle_mismatch(s, git_root, svn_root, auto_yes, dry_run)

    # Only in Git
    for rel in only_git:
        handle_only_in_one(rel, "git", git_root, svn_root, auto_yes, dry_run)

    # Only in SVN
    for rel in only_svn:
        handle_only_in_one(rel, "svn", git_root, svn_root, auto_yes, dry_run)

    print("\nDone.")

if __name__ == "__main__":
    main()
