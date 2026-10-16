# Active roadmap

Current behavior is documented in [DESIGN.md](DESIGN.md).

1. Reduce the largest complexity exceptions, starting with quilt refresh,
   remediation evidence selection, and nightly catalog/planning orchestration.
   Preserve behavior with focused component tests before extracting helpers.
2. Expand typed records from target/source identities to complete dependency
   decisions and result transitions. Extend strict type checking as modules migrate.
3. Recompute and validate runtime and autopkgtest relationships after remediation;
   support explicit replanning when candidate requirements change.
4. Extend relation handling for virtual/versioned Provides and Multi-Arch provider
   selection. Architecture and profile restrictions are already evaluated.
5. Pin archive dependency snapshots and define reproducibility acceptance criteria.
6. Add regress-stack as an explicit functional gate; keep it distinct from build
   success and accepted superficial/missing package tests.
7. Define a reviewed publication/signing design before adding credentials or uploads.

Do not keep unused implementations for these future milestones. Add abstractions
when their consumers and validation criteria exist.
