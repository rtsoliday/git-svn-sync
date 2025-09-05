# git-svn-sync

Convenience wrapper to synchronize files between paired Git and SVN working
copies. In addition to passing explicit `-git` and `-svn` paths, the script
now supports preset options for common repository pairs:

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
