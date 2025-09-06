# git-svn-sync

`git-svn-sync.py` is a Python utility for keeping a pair of local repositories
—one Git and one Subversion—in sync. It indexes the files tracked by each VCS,
compares their contents using SHA-256 checksums and timestamps, and
interactively copies newer files over the older ones. When copying, the script
replays the original commit message and author in the destination repository.
Files that exist only in one working copy can be added or removed after
confirmation.

Before performing any changes the script verifies that both working copies are
clean and up to date with their remotes. Only version-controlled files are
considered and paths listed in `~/.git-svn-sync.ignore` are skipped. The script
supports `-dry-run` to preview actions, `-yes` to auto-approve prompts, and
`-rebaseline` to populate the ignore file for a new pair of repositories.

## Usage

```
python git-svn-sync.py -git /path/to/git_wc -svn /path/to/svn_wc [-yes] [-dry-run] [-rebaseline]
```

In addition to passing explicit paths, several preset options are provided for
common repository pairs:

```
python git-svn-sync.py -sdds       # ~/github/SDDS <-> ~/epics/extensions/src/SDDS
python git-svn-sync.py -sddsepics  # ~/github/SDDS-EPICS <-> ~/epics/extensions/src/SDDSepics
python git-svn-sync.py -elegant    # ~/github/elegant <-> ~/oag/apps/src/elegant
python git-svn-sync.py -spiffe     # ~/github/spiffe <-> ~/oag/apps/src/spiffe
python git-svn-sync.py -clinchor   # ~/github/clinchor <-> ~/oag/apps/src/clinchor
python git-svn-sync.py -shield     # ~/github/shield <-> ~/oag/apps/src/shield
```

Each preset is equivalent to invoking the script with the corresponding `-git`
and `-svn` arguments.
