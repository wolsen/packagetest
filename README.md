# packagetest

Experimental automation for the **real Ubuntu/OpenStack Debian packaging workflow** on GitHub-hosted Actions runners.

This project does **not** replace Debian packaging tools. It orchestrates familiar tooling (`gbp`, `pristine-tar`, `gbp pq`, `debuild`, `sbuild`, APT repository metadata generation) and records every command for inspection.

## Current vertical slice

The first slice models and orchestrates a small real dependency chain:

- `pbr`
- `python-oslo.i18n`
- `python-oslo.serialization`
- `glance`

This demonstrates:

- dependency-aware planning (DAG)
- blocked/ready/build/publish state transitions
- OpenStack release discovery from `openstack/releases` series status and deliverables
- target resolution for cycle, beta, rc, final, and snapshot-based upstream refs
- branch-aware packaging repository orchestration (`packaging`, `upstream`, `pristine-tar`)
- Debian-versioned changelog update commands for OpenStack targets
- command-level execution logs
- generation manifest output
- failure bundle output (`failure.json`, `analysis.md`, `proposed-fix.patch`)
- artifact-backed APT repository command generation

## Important scope boundaries

- Runs on **GitHub-hosted** runners only.
- No Launchpad upload (`dput`) in this repository.
- No self-hosted runners, no long-lived signing credentials.
- Any signing step uses short-lived key material for build/repo metadata only.
- AI remediation is artifact-only and review-only (no automatic patch application).

## Quick start

```bash
python -m pip install -e .

# Create a build plan
packaging plan \
  --openstack-target 2027.1-b1 \
  --ubuntu-release noble \
  glance

# Record a reproducible snapshot using resolved upstream SHAs
packaging plan \
  --openstack-target 2027.1 \
  --snapshot-at 2026-09-18T05:32:15+00:00 \
  --ubuntu-release noble \
  glance

# Execute orchestrated commands as dry-run (safe in local/dev environments)
packaging build \
  --dry-run \
  --openstack-target 2027.1-b1 \
  --ubuntu-release noble \
  glance
```

A run creates a generation directory under `artifacts/` containing:

- `logs/commands.jsonl`
- `generation-manifest.json`
- `failures/<source>/...` (if any command fails)

## CI workflow model

`.github/workflows/vertical-slice.yml` keeps planning/scheduling in Python and uses staged jobs to reflect package dependencies:

1. plan
2. `pbr`
3. `python-oslo.*` in parallel
4. `glance`

The workflow demonstrates runner fan-out while preserving dependency constraints.

## Documentation

- `DESIGN.md` — architecture, lifecycle, DAG model, provenance, failure handling, AI boundary, security.
- `ROADMAP.md` — incremental milestones from planning to full packaging automation.
