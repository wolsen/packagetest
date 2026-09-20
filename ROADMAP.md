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

## Next: prove a dependency edge

- Build a pinned python-pbr producer and compatible consumer.
- Exercise the implemented `--extra-package` handoff.
- Require the producer's exact binary version in the consumer's `.buildinfo`.
- Prove a failed producer blocks its consumer.

## Broader Ubuntu and Cloud Archive support

- Turn release discovery output into reviewable locks with explicit branch, epoch, prerelease, and UCA version policies.
- Pin archive snapshots for reproducible build dependency selection.
- Add target profiles and integration cases for UCA suites and service packages.
- Add dependency graph expansion and scheduling beyond the small static configuration.
- Revisit repository publication only when artifact handoff is insufficient.
- Add review-only automated failure analysis and proposed packaging patches.
- Return to Gump workflow context handling and artifact lifecycle after tool-level behavior is established.

Archive upload, signing credentials, automatic patch application, and bit-for-bit reproducibility are not part of the current slice.
