# Agent Notes for git-svn-sync

This file contains a short tactical summary based on repository evidence. `../llm-wiki/scripts/refresh_wiki.py` rewrites only the machine-managed block.

<!-- BEGIN MACHINE:summary -->
## Quick start
- Repository-local guidance is sufficient: start with `AGENTS.md`, `README.md`, `docs/`, build/test/config files, and the source tree.
- git-svn-sync.py is a Python utility for keeping a pair of local repositories —one Git and one Subversion—in sync. It indexes the files tracked by each VCS, compares their contents using SHA-256 checksums and timestamps, and interactively copies newer files over the older ones. When copying, the script

## Read first
- `README.md`: Primary project overview and workflow notes
- `git-svn-sync.py`: Likely operator or developer entry point
- `patch.txt`: Supporting repository evidence

## Build and test
- Unknown: no explicit build instructions were extracted from the inspected files.
- Unknown: no test workflow evidence was found in the inspected files.
- Likely run commands or operator entry points: `python git-svn-sync.py -git /path/to/git_wc -svn /path/to/svn_wc [-yes] [-dry-run] [-rebaseline]`, `python git-svn-sync.py -sdds # ~/github/SDDS ~/epics/extensions/src/SDDS`, `python git-svn-sync.py -sddsepics # ~/github/SDDS-EPICS ~/epics/extensions/src/SDDSepics`, `python git-svn-sync.py -elegant # ~/github/elegant ~/oag/apps/src/elegant`.

## Operational warnings
- Local checkout layout appears significant; avoid casual changes to sibling-repo assumptions or relative paths.

## Compatibility constraints
- Build and runtime behavior likely depends on neighboring core toolkit checkouts.

## Related knowledge
- Repository-local documentation should be treated as authoritative.
- If a shared `llm-wiki/` directory is present in this workspace or parent folder, consult [the matching repo page](../llm-wiki/repos/git-svn-sync.md) for additional architectural context.
- If no shared wiki is present, continue using repository-local evidence only.
- If available, [the SDDS concept page](../llm-wiki/concepts/sdds.md) adds broader cross-repo context.
- If available, [the EPICS concept page](../llm-wiki/concepts/epics.md) adds broader cross-repo context.
- If present in this workspace, [the cross-repo map](../llm-wiki/insights/cross-repo-map.md) helps explain related repositories.
<!-- END MACHINE:summary -->

## Human notes
Add durable repo-specific instructions here.
