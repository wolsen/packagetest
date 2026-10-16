"""Render a self-contained, browsable snapshot pipeline report."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import shutil


def read_json(path: Path):
    """Read report JSON, allowing missing files and malformed content to fail visibly."""
    return json.loads(path.read_text())


def safe_id(value: str) -> str:
    """Normalize a report label into an HTML anchor identifier."""
    return re.sub(r'[^a-zA-Z0-9_-]+', '-', value).strip('-') or 'package'


def status_class(value: str) -> str:
    """Map pipeline verdicts to presentation classes used by report badges."""
    value = (value or 'UNKNOWN').upper()
    if value in {'SUCCEEDED', 'PASS', 'VALIDATED'}:
        return 'good'
    if value in {'SUPERFICIAL', 'NO_TESTS', 'SKIP', 'CANDIDATE'}:
        return 'warn'
    if value in {'MISSING', 'BLOCKED', 'NOT_RUN', 'UNKNOWN'}:
        return 'muted'
    return 'bad'


def status_badge(value: str) -> str:
    """Render an escaped status label with its corresponding verdict style."""
    value = value or 'UNKNOWN'
    return f'<span class="badge {status_class(value)}">{html.escape(value)}</span>'


def proposal_files(root: Path | None):
    """List proposal files beneath an available downloaded report root in stable order."""
    return sorted(root.rglob('packaging-proposal.json')) if root and root.exists() else []


def _validate_evolution(value: dict, known_sources: set[str], evolutions: dict) -> str:
    """Validate evolution identity and record shape before accepting evidence."""
    source = value.get('source')
    if source not in known_sources:
        raise ValueError(f'evolution record names unknown source {source!r}')

    if source in evolutions:
        raise ValueError(f'duplicate evolution record for {source}')

    if not isinstance(value.get('commit_delta'), dict):
        raise ValueError(f'invalid commit delta for {source}')

    if not isinstance(value.get('introduced_entry_points', []), list):
        raise ValueError(f'invalid entry point list for {source}')

    return source


def _evolution_files(root: Path | None) -> list[Path]:
    """Find evolution records in deterministic order when an artifact root exists."""
    return sorted(root.rglob('source-evolution.json')) if root and root.exists() else []


def load_evolutions(root: Path | None, known_sources: set[str]) -> tuple[dict, list[dict]]:
    """Load valid source evolution evidence and retain per-file warnings."""
    evolutions, warnings = {}, []
    paths = _evolution_files(root)
    for path in paths:
        try:
            value = read_json(path)
            source = _validate_evolution(value, known_sources, evolutions)
            evolutions[source] = value
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            warnings.append({'file': str(path), 'error': str(exc)})

    return evolutions, warnings


def _validate_proposal_actions(proposal: dict, source: str) -> None:
    """Require action evidence to be a list of structured objects."""
    if (not isinstance(proposal.get('actions', []), list)
            or not all(isinstance(action, dict) for action in proposal.get('actions', []))):
        raise ValueError(f'invalid action list for {source}')


def _validate_proposal_membership(proposal: dict, source: str,
                                  known_sources: set[str], proposals: dict) -> None:
    """Reject unknown or duplicate sources and malformed action lists."""
    if source not in known_sources:
        raise ValueError(f'proposal names unknown source {source!r}')

    if source in proposals:
        raise ValueError(f'duplicate proposal for {source}')

    _validate_proposal_actions(proposal, source)


def _validate_proposal(proposal: object, known_sources: set[str], proposals: dict) -> str:
    """Require an identified, unique proposal with structured repair actions."""
    if not isinstance(proposal, dict):
        raise ValueError('proposal metadata is not an object')

    source = proposal.get('source')
    if not isinstance(source, str) or not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', source):
        raise ValueError(f'invalid Debian source name {source!r}')

    _validate_proposal_membership(proposal, source, known_sources, proposals)
    return source


def _proposal_patch_spec(proposal: dict, source: str) -> dict:
    """Require structured patch metadata with a single safe filename."""
    patch_spec = proposal.get('patch') or {}
    if not isinstance(patch_spec, dict):
        raise ValueError(f'invalid patch metadata for {source}')

    patch_name = patch_spec.get('file', '')
    if not patch_name or Path(patch_name).name != patch_name:
        raise ValueError(f'unsafe patch path for {source}')

    return patch_spec


def _export_proposal_patch(proposal: dict, source: str, metadata_path: Path, patch_output: Path) -> None:
    """Verify patch content before exporting a source-named download artifact."""
    patch_spec = _proposal_patch_spec(proposal, source)
    patch_name = patch_spec.get('file', '')
    patch_path = metadata_path.parent / patch_name
    patch = patch_path.read_bytes()
    digest = hashlib.sha256(patch).hexdigest()
    if digest != patch_spec.get('sha256'):
        raise ValueError(f'patch checksum mismatch for {source}')

    patch_output.mkdir(parents=True, exist_ok=True)
    exported = patch_output / f'{source}.patch'
    shutil.copyfile(patch_path, exported)
    proposal['patch'] = {
        **patch_spec,
        'download': f'patches/{source}.patch',
        'content': patch.decode('utf-8', errors='replace'),
    }


def load_proposals(root: Path | None, known_sources: set[str], output: Path) -> tuple[dict, list[dict]]:
    """Export verified patch proposals and retain rejected artifact diagnostics."""
    proposals, warnings = {}, []
    patch_output = output / 'patches'
    if patch_output.exists():
        shutil.rmtree(patch_output)

    for metadata_path in proposal_files(root):
        try:
            proposal = read_json(metadata_path)
            source = _validate_proposal(proposal, known_sources, proposals)
            _export_proposal_patch(proposal, source, metadata_path, patch_output)
            proposals[source] = proposal
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            warnings.append({'file': str(metadata_path), 'error': str(exc)})

    return proposals, warnings


def _fallback_warnings(entries: dict) -> list[dict]:
    """Keep explicit warnings for packages using fallback Debian packaging."""
    warnings = []
    for source, entry in entries.items():
        if entry.get('packaging_fallback_warning'):
            warnings.append({
                'source': source,
                'kind': 'debian-packaging-fallback',
                'error': f"{source}: {entry['packaging_fallback_warning']}",
            })

    return warnings


def _result_warnings(entries: dict, results_by_source: dict, plan: dict) -> list[dict]:
    """Explain missing outcomes, discovery failures, and packaging fallbacks."""
    warnings = []
    missing_results = sorted(set(entries) - set(results_by_source))
    extra_results = sorted(set(results_by_source) - set(entries))
    if missing_results:
        warnings.append({'error': 'Missing result rows: ' + ', '.join(missing_results)})

    if extra_results:
        warnings.append({'error': 'Result rows for unknown sources: ' + ', '.join(extra_results)})

    for failure in plan.get('resolution_failures', []):
        warnings.append({'error': f"Source resolution failed for {failure.get('source', 'unknown')}: "
                                  f"{failure.get('error', 'unknown error')}"})

    warnings.extend(_fallback_warnings(entries))
    return warnings


def _package_records(entries: dict, results_by_source: dict, levels: dict,
                     proposals: dict, evolutions: dict) -> list[dict]:
    """Merge per-source evidence in dependency-level order with explicit missing outcomes."""
    packages = []
    missing = {'result': 'MISSING', 'error': 'No result was aggregated for this package'}
    ordered_sources = sorted(entries, key=lambda source: (levels.get(source, 10**9), source))
    for source in ordered_sources:
        entry = entries[source]
        result = results_by_source.get(source, {})
        packages.append({
            'source': source,
            'deliverable': entry.get('deliverable'),
            'dependency_level': levels.get(source, entry.get('wave', 0) + 1),
            'dependencies': entry.get('run_dependencies', []),
            'archive_bootstrap_dependencies': entry.get('archive_bootstrap_dependencies', []),
            'selection_reasons': entry.get('selection_reasons', []),
            'archive_version': entry.get('archive_version'),
            'comparison_archive_version': entry.get('comparison_archive_version'),
            'comparison_archive_pocket': entry.get('comparison_archive_pocket'),
            'packaging_source_role': entry.get('packaging_source_role'),
            'packaging_fallback_warning': entry.get('packaging_fallback_warning'),
            'upstream_ref': entry.get('upstream_ref'),
            'upstream_sha': entry.get('upstream_sha'),
            'build': result.get('build', missing),
            'autopkgtest': result.get('autopkgtest', missing),
            'proposal': proposals.get(source),
            'source_evolution': evolutions.get(source),
        })

    return packages


def _phase_counts(packages: list[dict]) -> dict:
    """Count build and autopkgtest verdicts in deterministic status order."""
    return {
        phase: dict(sorted(Counter(package[phase].get('result', 'UNKNOWN')
                                   for package in packages).items()))
        for phase in ('build', 'autopkgtest')
    }


def _dependency_levels(plan: dict) -> dict[str, int]:
    """Map scheduled sources to their one-based display level."""
    return {source: index for index, level in enumerate(plan.get('waves', []), 1)
              for source in level}


def build_report(catalog: dict, plan: dict, results: list[dict], proposals_root: Path | None,
                 output: Path, target: dict, functional: dict | None = None) -> dict:
    """Combine frozen planning metadata and results into a complete report."""
    entries = {entry['source']: entry for entry in catalog['packages']}
    results_by_source = {row['source']: row for row in results}
    proposals, warnings = load_proposals(proposals_root, set(entries), output)
    evolutions, evolution_warnings = load_evolutions(proposals_root, set(entries))
    warnings.extend(evolution_warnings)
    levels = _dependency_levels(plan)
    warnings.extend(_result_warnings(entries, results_by_source, plan))
    packages = _package_records(entries, results_by_source, levels, proposals, evolutions)
    counts = _phase_counts(packages)
    functional = functional or {
        'name': 'regress-stack',
        'result': 'NOT_RUN',
        'summary': 'The functional validation gate is reserved for a future pipeline phase.',
    }
    return {
        'schema_version': 1,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'run': catalog.get('ci', {}),
        'target': target,
        'counts': counts,
        'functional_tests': [functional],
        'plan': {
            'package_count': len(packages),
            'dependency_levels': len(plan.get('waves', [])),
            'proposal_count': len(proposals),
            'binary_review_count': sum(
                bool(value.get('human_binary_package_review_required'))
                for value in evolutions.values()),
            'requested_sources': plan.get('requested_sources', plan.get('sources', [])),
            'resolution_failures': plan.get('resolution_failures', []),
        },
        'warnings': warnings,
        'packages': packages,
    }


def _diff_line_kind(line: str) -> str:
    """Classify diff headers before additions and deletions sharing their prefixes."""
    return ('file' if line.startswith(('diff --git ', 'index ', '--- ', '+++ ')) else
            'hunk' if line.startswith('@@') else
            'add' if line.startswith('+') else
            'del' if line.startswith('-') else 'context')


def render_diff(diff: str) -> str:
    """Render escaped diff lines with semantic highlighting."""
    lines = []
    for line in diff.splitlines():
        kind = _diff_line_kind(line)
        lines.append(f'<span class="diff-{kind}">{html.escape(line)}</span>')

    return '\n'.join(lines)


def render_key_values(record: dict, keys: tuple[str, ...]) -> str:
    """Render nonempty selected record fields as escaped HTML definition entries."""
    rows = []
    for key in keys:
        value = record.get(key)
        if value not in (None, '', [], {}):
            rows.append(f'<dt>{html.escape(key.replace("_", " ").title())}</dt>'
                        f'<dd>{html.escape(str(value))}</dd>')
    return ''.join(rows)


def _action_details(action: dict) -> dict:
    """Retain nonempty action fields not already rendered as label or rationale."""
    return {key: value for key, value in action.items()
               if key not in {'action', 'reason', 'explanation'} and value not in (None, '', [], {})}


def _action_reason(action: dict) -> object:
    """Choose the first explicit rationale, explanation, or named subject."""
    return action.get('reason') or action.get('explanation') or action.get('name')


def _render_action(action: dict) -> str:
    """Render escaped repair rationale and remaining structured details."""
    label = action.get('action', 'packaging change').replace('-', ' ')
    reason = _action_reason(action)
    details = _action_details(action)
    body = f'<strong>{html.escape(label)}</strong>'
    if reason:
        body += f'<p>{html.escape(str(reason))}</p>'

    if details:
        body += '<dl class="compact">' + ''.join(
            f'<dt>{html.escape(str(key).replace("_", " "))}</dt><dd>{html.escape(str(value))}</dd>'
            for key, value in details.items()) + '</dl>'

    return f'<li>{body}</li>'


def render_proposal(proposal: dict | None) -> str:
    """Render a packaging proposal with its rationale, validation, and downloadable patch."""
    if not proposal:
        return '<p class="empty">No packaging change was proposed.</p>'

    actions = [_render_action(action) for action in proposal.get('actions', [])]
    destinations = ', '.join(item.get('repository', '') for item in proposal.get('destination_candidates', []))
    branches = ', '.join(proposal.get('branch_candidates', []))
    validation = proposal.get('validation', {})
    patch = proposal['patch']
    return f'''
      <div class="proposal-heading">
        {status_badge(proposal.get('status', 'candidate'))}
        <span class="review">Human review required</span>
        <a class="download" href="{html.escape(patch['download'], quote=True)}" download>Download patch</a>
      </div>
      <h4>Why this patch is proposed</h4>
      <ol class="actions">{''.join(actions) or '<li>No structured rationale was recorded.</li>'}</ol>
      <dl>{render_key_values({'candidate repositories': destinations, 'candidate branches': branches,
                              'removal condition': proposal.get('removal_condition')},
                             ('candidate repositories', 'candidate branches', 'removal condition'))}</dl>
      <h4>Proposal validation</h4>
      <p>Build {status_badge(validation.get('build', {}).get('result', 'NOT_RUN'))}
         Autopkgtest {status_badge(validation.get('autopkgtest', {}).get('result', 'NOT_RUN'))}</p>
      <details class="diff"><summary>View proposed packaging diff</summary>
        <pre>{render_diff(patch['content'])}</pre>
      </details>'''


def _baseline_warning(delta: dict) -> str:
    """Explain missing or mismatched official archive baselines."""
    mismatch = ''
    if delta.get('basis') == 'no-target-archive-baseline':
        mismatch = ('<p class="review">No official package exists in the target archive; '
                    'an official commit delta is not available yet.</p>')
    elif not delta.get('comparison_tag_matches_archive_version', False):
        mismatch = ('<p class="review">The archive version did not match this tag; '
                    'verify the official-package baseline.</p>')

    return mismatch


def _commit_list(commits: list[dict], rows: str, delta: dict) -> str:
    """Distinguish an empty delta from an unavailable official comparison."""
    if commits:
        return '<ol>' + rows + '</ol>'

    if delta.get('basis') == 'no-target-archive-baseline':
        return '<p>No official comparison is available.</p>'

    return '<p>Snapshot matches the comparison tag.</p>'


def _entry_point_rows(introduced: list[dict]) -> str:
    """Render new command names, groups, and packaging review guidance."""
    return ''.join(
        '<li><strong>' + html.escape(item.get('name', 'unknown')) + '</strong> '
        f'(<code>{html.escape(item.get("group", "unknown"))}</code>): '
        + html.escape(item.get('packaging', 'review required')) + '</li>'
        for item in introduced)


def render_evolution(evolution: dict | None) -> str:
    """Render upstream changes and baseline limitations for human review."""
    if not evolution:
        return '<p class="empty">No upstream evolution record was available.</p>'

    delta = evolution.get('commit_delta', {})
    commits = delta.get('commits', [])
    commit_rows = ''.join(
        f'<li><code>{html.escape(item.get("sha", "")[:12])}</code> '
        f'{html.escape(item.get("subject", ""))}</li>' for item in commits)
    introduced = evolution.get('introduced_entry_points', [])
    entry_rows = _entry_point_rows(introduced)
    mismatch = _baseline_warning(delta)
    review = ('<p class="review">Human review must decide whether a new binary package is needed.</p>'
              if evolution.get('human_binary_package_review_required') else '')
    return f'''<dl>{render_key_values({
        'archive upstream version': delta.get('archive_upstream_version'),
        'comparison archive tier': delta.get('comparison_archive_pocket'),
        'comparison tag': delta.get('comparison_tag'),
        'commits after baseline': delta.get('count'),
        'comparison basis': delta.get('basis'),
    }, ('archive upstream version', 'comparison archive tier', 'comparison tag',
        'commits after baseline', 'comparison basis'))}</dl>
      {mismatch}
      <details><summary>Commits in snapshot ({len(commits)})</summary>
        {_commit_list(commits, commit_rows, delta)}
      </details>
      <h4>New commands, services, and entry points</h4>
      {review}{'<ul>' + entry_rows + '</ul>' if introduced else '<p>None detected.</p>'}'''


def _package_errors(package: dict) -> list[str]:
    """Render escaped phase errors and put packaging-source warnings first."""
    build, test = package['build'], package['autopkgtest']
    errors = []
    for label, value in [('Build', build), ('Autopkgtest', test)]:
        if value.get('error'):
            errors.append(f'<h4>{label} detail</h4><pre class="error">{html.escape(str(value["error"]))}</pre>')

    packaging_warning = package.get('packaging_fallback_warning')
    if packaging_warning:
        errors.insert(0, '<p class="review">Packaging source warning: ' +
                      html.escape(packaging_warning) + '</p>')

    return errors


def _package_search(package: dict, dependencies: str, reasons: str) -> str:
    """Build normalized search text from source identity and evolution evidence."""
    source, evolution = package['source'], package.get('source_evolution')
    evolution_search = ' '.join(item.get('name', '')
                                for item in (evolution or {}).get('introduced_entry_points', []))
    return ' '.join((source, package.get('deliverable') or '', dependencies, reasons,
                       evolution_search)).lower()


def _display_names(values: list[str], fallback: str) -> str:
    """Join display names while retaining the established empty-list label."""
    return ', '.join(values) or fallback


def _package_card(package: dict) -> str:
    """Render one searchable package with build, test, and packaging evidence."""
    source = package['source']
    build = package['build']
    test = package['autopkgtest']
    proposal = package['proposal']
    evolution = package.get('source_evolution')
    dependencies = _display_names(package['dependencies'], 'None')
    bootstrap = _display_names(package['archive_bootstrap_dependencies'], 'None')
    reasons = _display_names(package['selection_reasons'], 'Catalog selection')
    search = _package_search(package, dependencies, reasons)
    errors = _package_errors(package)
    return f'''
<details class="package" id="package-{safe_id(source)}" data-search="{html.escape(search, quote=True)}"
         data-build="{html.escape(build.get('result', 'UNKNOWN'), quote=True)}"
         data-test="{html.escape(test.get('result', 'UNKNOWN'), quote=True)}"
         data-proposal="{'yes' if proposal else 'no'}" data-level="{package['dependency_level']}">
  <summary>
    <span class="package-name">{html.escape(source)}</span>
    <span class="level">Level {package['dependency_level']}</span>
    <span>Build {status_badge(build.get('result', 'UNKNOWN'))}</span>
    <span>Test {status_badge(test.get('result', 'UNKNOWN'))}</span>
    <span>{'Patch proposed' if proposal else 'No patch'}</span>
  </summary>
  <div class="package-body">
    <section><h3>Package result</h3>
      <dl>{render_key_values({'archive version': package.get('archive_version'),
                              'comparison archive version': package.get('comparison_archive_version'),
                              'comparison archive tier': package.get('comparison_archive_pocket'),
                              'packaging source': package.get('packaging_source_role'),
                              'upstream ref': package.get('upstream_ref'),
                              'upstream sha': package.get('upstream_sha'),
                              'candidate dependencies': dependencies,
                              'archive bootstrap dependencies': bootstrap,
                              'selection reason': reasons},
                             ('archive version', 'comparison archive version', 'comparison archive tier',
                              'packaging source',
                              'upstream ref', 'upstream sha',
                              'candidate dependencies', 'archive bootstrap dependencies', 'selection reason'))}</dl>
      {''.join(errors)}
    </section>
    <section><h3>Upstream evolution</h3>{render_evolution(evolution)}
      <h3>Packaging proposal</h3>{render_proposal(proposal)}</section>
  </div>
</details>'''


def _count_cards(report: dict) -> list[str]:
    """Render plan totals and per-phase status counters."""
    count_cards = []
    plan = report['plan']
    count_cards.append(
        f'<section class="metric"><h3>Build plan</h3>'
        f'<span>Packages <b>{plan["package_count"]}</b></span>'
        f'<span>Dependency levels <b>{plan["dependency_levels"]}</b></span>'
        f'<span>Proposed patches <b>{plan["proposal_count"]}</b></span></section>')
    count_cards[-1] = count_cards[-1].replace(
        '</section>', f'<span>Binary package reviews <b>{plan.get("binary_review_count", 0)}</b></span></section>')
    for phase, outcomes in report['counts'].items():
        count_cards.append('<section class="metric"><h3>' + html.escape(phase.title()) + '</h3>' +
                           ''.join(f'<span>{status_badge(status)} <b>{count}</b></span>'
                                   for status, count in outcomes.items()) + '</section>')

    return count_cards


def _filter_options(values: list) -> str:
    """Escape both labels and attribute values for report filter choices."""
    return ''.join(f'<option value="{html.escape(str(value), quote=True)}">{html.escape(str(value))}</option>'
                   for value in values)


def render_html(report: dict) -> str:
    """Render the complete self-contained report with package filters."""
    target = report['target']
    run = report.get('run', {})
    levels = sorted({package['dependency_level'] for package in report['packages']})
    cards = [_package_card(package) for package in report['packages']]
    count_cards = _count_cards(report)
    functional = report['functional_tests'][0]
    warnings = ''.join(f'<li>{html.escape(item.get("error", str(item)))}</li>' for item in report['warnings'])
    build_states = sorted(report['counts']['build'])
    test_states = sorted(report['counts']['autopkgtest'])
    title = f"{target.get('openstack_series', 'OpenStack')} snapshot report"
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{--ink:#17202a;--sub:#5c6672;--line:#d8dee6;--paper:#fff;--wash:#f4f6f8;--navy:#123a58;--blue:#276a9b;--good:#176b4d;--good-bg:#e2f4eb;--warn:#8a5700;--warn-bg:#fff1cf;--bad:#a52b2b;--bad-bg:#fde5e5;--muted:#59636e;--muted-bg:#e9edf1}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--wash);color:var(--ink);font:15px/1.5 system-ui,-apple-system,sans-serif}}
header{{background:var(--navy);color:#fff;padding:2.25rem max(1.25rem,calc((100% - 1180px)/2))}} header h1{{margin:0 0 .35rem;font-size:2rem}} header p{{margin:.2rem 0;color:#d9e8f2}}
main{{max-width:1180px;margin:auto;padding:1.5rem}} .run-meta,.metrics,.filters,.package-body{{display:grid;gap:1rem}} .run-meta{{grid-template-columns:repeat(auto-fit,minmax(155px,1fr));margin-top:1.4rem}} .run-meta div{{border-left:3px solid #83b7d8;padding-left:.7rem}} .run-meta b{{display:block;color:#fff}}
.metrics{{grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-bottom:1rem}} .metric,.gate,.notice,.filters{{background:var(--paper);border:1px solid var(--line);border-radius:8px;padding:1rem}} .metric h3,.gate h2{{margin:0 0 .6rem}} .metric>span{{display:flex;justify-content:space-between;margin:.35rem 0}}
.badge{{display:inline-block;border-radius:999px;padding:.1rem .5rem;font-size:.78rem;font-weight:750;letter-spacing:.02em}} .good{{color:var(--good);background:var(--good-bg)}} .warn{{color:var(--warn);background:var(--warn-bg)}} .bad{{color:var(--bad);background:var(--bad-bg)}} .muted{{color:var(--muted);background:var(--muted-bg)}}
.gate{{margin-bottom:1rem;border-left:5px solid var(--blue)}} .gate h2{{display:flex;gap:.7rem;align-items:center}} .notice{{margin-bottom:1rem;border-left:5px solid var(--warn)}} .notice h2{{margin-top:0}}
.filters{{grid-template-columns:2fr repeat(4,1fr);position:sticky;top:0;z-index:2;box-shadow:0 3px 12px #18232d18;margin-bottom:1rem}} label{{font-size:.8rem;color:var(--sub);font-weight:700}} input,select{{display:block;width:100%;margin-top:.25rem;padding:.55rem;border:1px solid #aeb7c2;border-radius:5px;background:#fff;color:var(--ink)}}
.result-line{{display:flex;justify-content:space-between;align-items:center;margin:1.2rem 0 .65rem}} .package{{background:#fff;border:1px solid var(--line);border-radius:7px;margin:.55rem 0;overflow:hidden}} .package[open]{{border-color:#9aabba}} .package>summary{{cursor:pointer;display:grid;grid-template-columns:minmax(190px,1.4fr) .55fr 1fr 1fr .7fr;gap:.8rem;align-items:center;padding:.85rem 1rem}} .package>summary:hover{{background:#f7fafc}} .package-name{{font-weight:800;color:var(--navy)}} .level{{color:var(--sub)}}
.package-body{{grid-template-columns:1fr 1.5fr;border-top:1px solid var(--line);padding:1rem;background:#fbfcfd}} h3,h4{{color:var(--navy)}} dl{{display:grid;grid-template-columns:minmax(120px,.65fr) 1.5fr;gap:.3rem .8rem}} dt{{font-weight:700;color:var(--sub)}} dd{{margin:0;overflow-wrap:anywhere}} .compact{{font-size:.88rem}} .actions{{padding-left:1.25rem}} .actions li{{margin:.65rem 0}} .actions p{{margin:.15rem 0}} .proposal-heading{{display:flex;gap:.7rem;align-items:center;flex-wrap:wrap}} .review{{font-weight:700;color:var(--warn)}} .download{{margin-left:auto}} a{{color:var(--blue)}}
.diff summary{{cursor:pointer;font-weight:750;color:var(--blue)}} pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#18232d;color:#e9eef2;padding:1rem;border-radius:5px;max-height:36rem;overflow:auto;font:12px/1.45 ui-monospace,monospace}} pre span{{display:block}} .diff-add{{color:#9ce6b8;background:#17442c}} .diff-del{{color:#ffb1ac;background:#4b2224}} .diff-hunk{{color:#a9cff2}} .diff-file{{color:#f5d77a;font-weight:700}} .error{{border-left:4px solid #d55}} .empty{{color:var(--sub);font-style:italic}} footer{{color:var(--sub);margin:2rem 0}}
[hidden]{{display:none!important}} @media(max-width:800px){{.filters{{grid-template-columns:1fr 1fr;position:static}} .package>summary{{grid-template-columns:1fr 1fr}} .package-body{{grid-template-columns:1fr}}}} @media(max-width:500px){{.filters,.package>summary{{grid-template-columns:1fr}}}}
</style></head><body>
<header><h1>{html.escape(title)}</h1><p>Build, package test, and proposed packaging changes in one review artifact.</p>
<div class="run-meta">
 <div>Target<b>{html.escape(target.get('label', target.get('id', 'unknown')))}</b></div>
 <div>Ubuntu base<b>{html.escape(target.get('base_series', 'unknown'))}</b></div>
 <div>Suite<b>{html.escape(target.get('suite', 'unknown'))}</b></div>
 <div>Architecture<b>{html.escape(target.get('architecture', 'unknown'))}</b></div>
 <div>Run / attempt<b>{html.escape(str(run.get('run_id', 'local')))} / {html.escape(str(run.get('run_attempt', '1')))}</b></div>
</div></header><main>
<div class="metrics">{''.join(count_cards)}</div>
<section class="gate"><h2>Functional validation {status_badge(functional.get('result', 'NOT_RUN'))}</h2><p><strong><a href="https://github.com/canonical/regress-stack">{html.escape(functional.get('name', 'regress-stack'))}</a></strong>: {html.escape(functional.get('summary', ''))}</p></section>
{f'<section class="notice"><h2>Report warnings</h2><ul>{warnings}</ul></section>' if warnings else ''}
<section class="filters" aria-label="Package filters">
 <label>Search<input id="search" type="search" placeholder="Package, dependency, reason"></label>
 <label>Build<select id="build"><option value="">All</option>{_filter_options(build_states)}</select></label>
 <label>Autopkgtest<select id="test"><option value="">All</option>{_filter_options(test_states)}</select></label>
 <label>Proposal<select id="proposal"><option value="">All</option><option value="yes">Proposed</option><option value="no">None</option></select></label>
 <label>Dependency level<select id="level"><option value="">All</option>{_filter_options(levels)}</select></label>
</section>
<div class="result-line"><strong id="visible-count">{len(cards)} packages</strong><button id="reset" type="button">Reset filters</button></div>
<section id="packages">{''.join(cards)}</section>
<footer>Generated {html.escape(report['generated_at'])}. Extract the artifact before using patch download links.</footer>
</main><script>
const controls=['search','build','test','proposal','level'].map(id=>document.getElementById(id));
const packages=[...document.querySelectorAll('.package')];
function filter(){{const [q,b,t,p,l]=controls.map(x=>x.value.toLowerCase());let n=0;packages.forEach(card=>{{const show=(!q||card.dataset.search.includes(q))&&(!b||card.dataset.build.toLowerCase()===b)&&(!t||card.dataset.test.toLowerCase()===t)&&(!p||card.dataset.proposal===p)&&(!l||card.dataset.level===l);card.hidden=!show;if(show)n++;}});document.getElementById('visible-count').textContent=`${{n}} package${{n===1?'':'s'}}`;}}
controls.forEach(control=>control.addEventListener('input',filter));document.getElementById('reset').addEventListener('click',()=>{{controls.forEach(x=>x.value='');filter();}});
</script></body></html>'''


def _validate_report_target(frozen: dict, supplied: dict) -> None:
    """Require explicitly supplied fields to agree with a frozen catalog target."""
    for key, value in supplied.items():
        if frozen and value is not None and frozen.get(key) != value:
            raise ValueError(f'Report target {key} differs from frozen catalog target')


def _target_value(frozen: dict, supplied: dict, key: str, default: str) -> str:
    """Prefer frozen identity, then a supplied value, then the display fallback."""
    return frozen.get(key) or supplied.get(key) or default


def _report_target(catalog: dict, args: argparse.Namespace) -> dict:
    """Resolve display target fields while rejecting conflicts with the frozen target."""
    frozen = catalog.get('target') or {}
    supplied = {
        'id': args.target_id, 'kind': args.target_kind,
        'base_series': args.base_series, 'suite': args.suite,
        'architecture': args.architecture,
    }
    _validate_report_target(frozen, supplied)
    suite = _target_value(frozen, supplied, 'suite', catalog.get('suite', 'unknown'))
    kind = _target_value(frozen, supplied, 'kind', 'development')
    target = {
        'id': _target_value(frozen, supplied, 'id', 'ubuntu-development'),
        'kind': kind,
        'label': frozen.get('label') or ('Ubuntu development release'
                                        if kind == 'development'
                                        else 'Ubuntu Cloud Archive'),
        'base_series': _target_value(frozen, supplied, 'base_series', suite),
        'suite': suite,
        'architecture': _target_value(frozen, supplied, 'architecture', args.architecture),
        'openstack_series': catalog.get('series', 'unknown'),
    }
    return target


def main() -> int:
    """Validate report inputs and publish JSON plus an HTML review artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--proposals', type=Path)
    parser.add_argument('--functional-results', type=Path)
    parser.add_argument('--output', type=Path, default=Path('summary'))
    parser.add_argument('--target-id')
    parser.add_argument('--target-kind', choices=('development', 'uca'))
    parser.add_argument('--base-series')
    parser.add_argument('--suite')
    parser.add_argument('--architecture', default='amd64')
    args = parser.parse_args()
    catalog, plan, results = read_json(args.catalog), read_json(args.plan), read_json(args.results)
    target = _report_target(catalog, args)
    functional = read_json(args.functional_results) if args.functional_results else None
    args.output.mkdir(parents=True, exist_ok=True)
    report = build_report(catalog, plan, results, args.proposals, args.output, target, functional)
    (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    (args.output / 'index.html').write_text(render_html(report))
    print(json.dumps({'packages': len(report['packages']),
                      'proposals': sum(package['proposal'] is not None for package in report['packages']),
                      'warnings': len(report['warnings']), 'output': str(args.output / 'index.html')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
