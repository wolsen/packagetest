#!/usr/bin/env python3
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
    return json.loads(path.read_text())


def safe_id(value: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]+', '-', value).strip('-') or 'package'


def status_class(value: str) -> str:
    value = (value or 'UNKNOWN').upper()
    if value in {'SUCCEEDED', 'PASS', 'VALIDATED'}:
        return 'good'
    if value in {'SUPERFICIAL', 'NO_TESTS', 'SKIP', 'CANDIDATE'}:
        return 'warn'
    if value in {'MISSING', 'BLOCKED', 'NOT_RUN', 'UNKNOWN'}:
        return 'muted'
    return 'bad'


def status_badge(value: str) -> str:
    value = value or 'UNKNOWN'
    return f'<span class="badge {status_class(value)}">{html.escape(value)}</span>'


def proposal_files(root: Path | None):
    return sorted(root.rglob('packaging-proposal.json')) if root and root.exists() else []


def load_proposals(root: Path | None, known_sources: set[str], output: Path):
    proposals, warnings = {}, []
    patch_output = output / 'patches'
    if patch_output.exists():
        shutil.rmtree(patch_output)
    for metadata_path in proposal_files(root):
        try:
            proposal = read_json(metadata_path)
            if not isinstance(proposal, dict):
                raise ValueError('proposal metadata is not an object')
            source = proposal.get('source')
            if not isinstance(source, str) or not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', source):
                raise ValueError(f'invalid Debian source name {source!r}')
            if source not in known_sources:
                raise ValueError(f'proposal names unknown source {source!r}')
            if source in proposals:
                raise ValueError(f'duplicate proposal for {source}')
            if (not isinstance(proposal.get('actions', []), list)
                    or not all(isinstance(action, dict) for action in proposal.get('actions', []))):
                raise ValueError(f'invalid action list for {source}')
            patch_spec = proposal.get('patch') or {}
            if not isinstance(patch_spec, dict):
                raise ValueError(f'invalid patch metadata for {source}')
            patch_name = patch_spec.get('file', '')
            if not patch_name or Path(patch_name).name != patch_name:
                raise ValueError(f'unsafe patch path for {source}')
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
            proposals[source] = proposal
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            warnings.append({'file': str(metadata_path), 'error': str(exc)})
    return proposals, warnings


def build_report(catalog: dict, plan: dict, results: list[dict], proposals_root: Path | None,
                 output: Path, target: dict, functional: dict | None = None) -> dict:
    entries = {entry['source']: entry for entry in catalog['packages']}
    results_by_source = {row['source']: row for row in results}
    proposals, warnings = load_proposals(proposals_root, set(entries), output)
    levels = {source: index for index, level in enumerate(plan.get('waves', []), 1)
              for source in level}
    missing_results = sorted(set(entries) - set(results_by_source))
    extra_results = sorted(set(results_by_source) - set(entries))
    if missing_results:
        warnings.append({'error': 'Missing result rows: ' + ', '.join(missing_results)})
    if extra_results:
        warnings.append({'error': 'Result rows for unknown sources: ' + ', '.join(extra_results)})
    for failure in plan.get('resolution_failures', []):
        warnings.append({'error': f"Source resolution failed for {failure.get('source', 'unknown')}: "
                                  f"{failure.get('error', 'unknown error')}"})

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
            'upstream_ref': entry.get('upstream_ref'),
            'upstream_sha': entry.get('upstream_sha'),
            'build': result.get('build', missing),
            'autopkgtest': result.get('autopkgtest', missing),
            'proposal': proposals.get(source),
        })
    counts = {
        phase: dict(sorted(Counter(package[phase].get('result', 'UNKNOWN')
                                   for package in packages).items()))
        for phase in ('build', 'autopkgtest')
    }
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
            'requested_sources': plan.get('requested_sources', plan.get('sources', [])),
            'resolution_failures': plan.get('resolution_failures', []),
        },
        'warnings': warnings,
        'packages': packages,
    }


def render_diff(diff: str) -> str:
    lines = []
    for line in diff.splitlines():
        kind = ('file' if line.startswith(('diff --git ', 'index ', '--- ', '+++ ')) else
                'hunk' if line.startswith('@@') else
                'add' if line.startswith('+') else
                'del' if line.startswith('-') else 'context')
        lines.append(f'<span class="diff-{kind}">{html.escape(line)}</span>')
    return '\n'.join(lines)


def render_key_values(record: dict, keys: tuple[str, ...]) -> str:
    rows = []
    for key in keys:
        value = record.get(key)
        if value not in (None, '', [], {}):
            rows.append(f'<dt>{html.escape(key.replace("_", " ").title())}</dt>'
                        f'<dd>{html.escape(str(value))}</dd>')
    return ''.join(rows)


def render_proposal(proposal: dict | None) -> str:
    if not proposal:
        return '<p class="empty">No packaging change was proposed.</p>'
    actions = []
    for action in proposal.get('actions', []):
        label = action.get('action', 'packaging change').replace('-', ' ')
        reason = action.get('reason') or action.get('explanation') or action.get('name')
        details = {key: value for key, value in action.items()
                   if key not in {'action', 'reason', 'explanation'} and value not in (None, '', [], {})}
        body = f'<strong>{html.escape(label)}</strong>'
        if reason:
            body += f'<p>{html.escape(str(reason))}</p>'
        if details:
            body += '<dl class="compact">' + ''.join(
                f'<dt>{html.escape(str(key).replace("_", " "))}</dt><dd>{html.escape(str(value))}</dd>'
                for key, value in details.items()) + '</dl>'
        actions.append(f'<li>{body}</li>')
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


def render_html(report: dict) -> str:
    target = report['target']
    run = report.get('run', {})
    cards = []
    levels = sorted({package['dependency_level'] for package in report['packages']})
    for package in report['packages']:
        source = package['source']
        build = package['build']
        test = package['autopkgtest']
        proposal = package['proposal']
        dependencies = ', '.join(package['dependencies']) or 'None'
        bootstrap = ', '.join(package['archive_bootstrap_dependencies']) or 'None'
        reasons = ', '.join(package['selection_reasons']) or 'Catalog selection'
        search = ' '.join((source, package.get('deliverable') or '', dependencies, reasons)).lower()
        errors = []
        for label, value in [('Build', build), ('Autopkgtest', test)]:
            if value.get('error'):
                errors.append(f'<h4>{label} detail</h4><pre class="error">{html.escape(str(value["error"]))}</pre>')
        cards.append(f'''
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
                              'upstream ref': package.get('upstream_ref'),
                              'upstream sha': package.get('upstream_sha'),
                              'candidate dependencies': dependencies,
                              'archive bootstrap dependencies': bootstrap,
                              'selection reason': reasons},
                             ('archive version', 'upstream ref', 'upstream sha',
                              'candidate dependencies', 'archive bootstrap dependencies', 'selection reason'))}</dl>
      {''.join(errors)}
    </section>
    <section><h3>Packaging proposal</h3>{render_proposal(proposal)}</section>
  </div>
</details>''')
    count_cards = []
    plan = report['plan']
    count_cards.append(
        f'<section class="metric"><h3>Build plan</h3>'
        f'<span>Packages <b>{plan["package_count"]}</b></span>'
        f'<span>Dependency levels <b>{plan["dependency_levels"]}</b></span>'
        f'<span>Proposed patches <b>{plan["proposal_count"]}</b></span></section>')
    for phase, outcomes in report['counts'].items():
        count_cards.append('<section class="metric"><h3>' + html.escape(phase.title()) + '</h3>' +
                           ''.join(f'<span>{status_badge(status)} <b>{count}</b></span>'
                                   for status, count in outcomes.items()) + '</section>')
    functional = report['functional_tests'][0]
    warnings = ''.join(f'<li>{html.escape(item.get("error", str(item)))}</li>' for item in report['warnings'])
    options = lambda values: ''.join(f'<option value="{html.escape(str(v), quote=True)}">{html.escape(str(v))}</option>' for v in values)
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
 <label>Build<select id="build"><option value="">All</option>{options(build_states)}</select></label>
 <label>Autopkgtest<select id="test"><option value="">All</option>{options(test_states)}</select></label>
 <label>Proposal<select id="proposal"><option value="">All</option><option value="yes">Proposed</option><option value="no">None</option></select></label>
 <label>Dependency level<select id="level"><option value="">All</option>{options(levels)}</select></label>
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--proposals', type=Path)
    parser.add_argument('--functional-results', type=Path)
    parser.add_argument('--output', type=Path, default=Path('summary'))
    parser.add_argument('--target-id', default='ubuntu-development')
    parser.add_argument('--target-kind', choices=('development', 'uca'), default='development')
    parser.add_argument('--base-series')
    parser.add_argument('--suite')
    parser.add_argument('--architecture', default='amd64')
    args = parser.parse_args()
    catalog, plan, results = read_json(args.catalog), read_json(args.plan), read_json(args.results)
    suite = args.suite or catalog.get('suite', 'unknown')
    target = {
        'id': args.target_id,
        'kind': args.target_kind,
        'label': ('Ubuntu development release' if args.target_kind == 'development'
                  else 'Ubuntu Cloud Archive'),
        'base_series': args.base_series or suite,
        'suite': suite,
        'architecture': args.architecture,
        'openstack_series': catalog.get('series', 'unknown'),
    }
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
