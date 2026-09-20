> Status: integration deferred. Direct tool validation takes priority. Gump did not propagate `vars` or job environment into step conditions as expected; checkout therefore replaced the synchronized worktree with committed HEAD. No further Gump repair is part of this slice. The fixes below remain available for later work.

# Gump integration

The local runner is the sibling project `/home/wolsen/work/playground/gump`. This slice uses one VM and one workflow job, avoiding assumptions about multi-job pool freshness. The checked-in workflow grants 90 minutes for provisioning, source preparation, building, and installation checks.

Three small changes are required in that checkout:

1. `src/gump/runner/lifecycle.py`: remove the listener's independent 900-second wait. The workflow job's outer timeout remains responsible for its deadline.
2. `src/gump/runstate/artifacts.py`: optionally export finalized artifact bytes and metadata to `GUMP_ARTIFACT_DIR`. The API remains in-memory while the server runs; exported ZIPs survive server exit and VM teardown. Use a separate directory per run.
3. `src/gump/gitutil.py`: omit deleted tracked files from worktree synchronization while keeping newly added files and symlinks.

Focused regression tests cover each change. A patch is retained in `integration/gump-packaging.patch` so another checkout can review and apply the changes. No Gump commit or release has been made by this task.

Use `--preserve failed` while developing. Preservation is a debugging facility: failed VMs consume disk and memory until explicitly destroyed. Gump prints their exact IDs and inspection commands. Archive artifacts and the host control log before removing a preserved VM.

The lock files select `noble-amd64-sbuild`; the provisioning script creates it as a tarball-backed schroot. Build sessions and installation sessions use separate extracted roots. The VM provides the kernel and privileges needed for those operations without requiring nested privileged Docker on the workstation.
