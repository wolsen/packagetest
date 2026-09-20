# Roadmap

Progress is based on real build evidence, not generated command strings. See `VALIDATION.md` for completed runs.

## Working library slice

- [x] Separate release discovery from execution of reviewed, pinned build inputs.
- [x] Rebuild the published Noble `python-oslo.i18n` source in an isolated schroot.
- [x] Prepare the pinned 6.4.0 update with gbp, pristine-tar, patch validation, and a committed changelog.
- [x] Build actual binaries, validate metadata/checksums/buildinfo, and run Lintian.
- [x] Install both versions in fresh schroots and run an Oslo translation smoke test.
- [x] Retain source/binary artifacts, full logs, provenance, and failure diagnostics locally.
- [x] Repeat the upgrade from the same lock with a fresh checkout, build root, and installation session.
- [x] Generate and build the latest pinned oslo.i18n Git snapshot on Stonking, with exact installed Python version verification.
- [ ] Verify the checked-in workflow on GitHub Actions.

## Validated dependency edge

- [x] Build a pinned python-pbr producer and compatible consumer.
- [x] Exercise the implemented `--extra-package` handoff.
- [x] Require the producer's exact binary version during resolution and in the consumer's `.buildinfo`.
- [x] Prove a failed producer blocks its consumer.

## Snapshot and UCA milestones

- [x] Create reviewable snapshot locks from a branch or timezone-aware cutoff.
- [x] Generate checksum-pinned sdists with portable permissions and regression coverage.
- [x] Build the pinned Noble/UCA Epoxy library backport with explicit branch and revision policy.
- [x] Build Glance's five binary packages and run its packaging unit tests.
- [ ] Complete hosted matrix and installed Glance API validation; see `MILESTONES.md` for final evidence.

## Broader Ubuntu and Cloud Archive support

- Turn release discovery output into reviewable locks with explicit branch, epoch, prerelease, and UCA version policies.
- Pin archive snapshots for reproducible build dependency selection.
- Extend the initial Noble/UCA Epoxy profile and Glance case to additional supported series and services.
- Add dependency graph expansion and scheduling beyond the small static configuration.
- Revisit repository publication only when artifact handoff is insufficient.
- Add review-only automated failure analysis and proposed packaging patches.
- Return to Gump workflow context handling and artifact lifecycle after tool-level behavior is established.

Archive upload, signing credentials, automatic patch application, and bit-for-bit reproducibility are not part of the current slice.
