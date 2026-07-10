# git-svn-sync

`git-svn-sync.py` is a Python utility for keeping a pair of local repositories
—one Git and one Subversion—in sync. It indexes the files tracked by each VCS,
compares their contents using SHA-256 checksums and timestamps, and
interactively copies newer files over the older ones. When copying, the script
replays the original commit message and author in the destination repository.
Approved changes with the same final commit message are grouped into a single
commit per destination repository instead of being committed one file at a time.
Files that exist only in one working copy can be added or removed after
confirmation.

Before performing any changes the script verifies that both working copies are
clean and up to date with their remotes. Only version-controlled files are
considered, paths listed in `~/.git-svn-sync.ignore` are skipped, and files
under any `.kilo` directory are always ignored. The script supports `-dry-run`
to preview actions without copying, staging, committing, pushing, or writing the
ignore file, `-yes` to auto-approve prompts, and `-rebaseline` to populate the
ignore file for a new pair of repositories.

## Usage

```
python git-svn-sync.py -git /path/to/git_wc -svn /path/to/svn_wc [-yes] [-dry-run] [-rebaseline]
```

## GUI

Run without command-line options to open the Tkinter GUI:

```
python git-svn-sync.py
```

The GUI lets you choose a preset or interactively set manual Git/SVN
working-copy paths with text fields and Browse buttons, scan for differences,
review and select planned operations, run selected operations, run rebaseline,
and update the SVN working copy. Dry run is enabled by default in the GUI. The
lower log pane shows the underlying Git/SVN commands and file operations with
stdout, stderr, exit codes, and dry-run/planned markers.

The `Update SVN` button first runs `svn status`. If tracked local changes are
found, it refuses to run `svn update` and reports the blocking paths. Untracked
`?` entries and external `X` entries are ignored for this safety check. If the
status is otherwise clean, it runs `svn update` and logs the command output.
When Scan detects that SVN needs an update, the error popup includes an
`Update SVN` button for the same safe workflow.

The GUI uses the same sync core as the command-line interface. Discovery
commands still run in dry-run mode so the plan is based on repository state, but
dry-run skips `git fetch` and reports that remote freshness was not refreshed.
Git and SSH authentication requested by GUI-launched commands is shown in a
foreground password dialog instead of the terminal that launched the GUI. CLI
runs retain their normal terminal authentication behavior.

In addition to passing explicit paths, several preset options are provided for
common repository pairs:

```
python git-svn-sync.py -sdds       # ~/github/SDDS <-> ~/epics/extensions/src/SDDS
python git-svn-sync.py -sddsepics  # ~/github/SDDS-EPICS <-> ~/epics/extensions/src/SDDSepics
python git-svn-sync.py -elegant    # ~/github/elegant <-> ~/oag/apps/src/elegant
python git-svn-sync.py -spiffe     # ~/github/spiffe <-> ~/oag/apps/src/spiffe
python git-svn-sync.py -clinchor   # ~/github/clinchor <-> ~/oag/apps/src/clinchor
python git-svn-sync.py -shield     # ~/github/shield <-> ~/oag/apps/src/shield
python git-svn-sync.py -oag        # ~/github/oag-src <-> ~/oag/apps/src
```

Each preset is equivalent to invoking the script with the corresponding `-git`
and `-svn` arguments.

The `-oag` preset also ignores SVN-only top-level entries under
`~/oag/apps/src`, so unrelated sibling trees and root-level files in that
working copy do not need to be added to `~/.git-svn-sync.ignore`.
