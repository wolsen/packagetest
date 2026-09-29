"""Read Python dependency metadata from an immutable upstream revision."""
from __future__ import annotations

import re
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version


DEPENDENCY_FILES = ('requirements.txt', 'test-requirements.txt', 'pyproject.toml')
TEST_GROUP_NAMES = {'test', 'tests', 'testing'}
ARCHIVE_DISTRIBUTION_ALIASES = {
    'pyyaml': 'yaml',
    'python-dateutil': 'dateutil',
}


class MetadataFetchError(RuntimeError):
    """An upstream dependency file could not be read reliably."""


def normalize_distribution(name: str) -> str:
    """Use the same spelling equivalence as Python package indexes."""
    return re.sub(r'[-_.]+', '-', name).lower()


def debian_upstream_version(version: str) -> str:
    """Extract and normalize the upstream portion of a Debian version."""
    value = version.split(':', 1)[-1]
    if '-' in value:
        value = value.rsplit('-', 1)[0]
    # Debian sorts '~' before the empty string; Python expresses the common
    # OpenStack prereleases without that separator (1.0.0rc1).
    return value.replace('~', '')


def archive_requirement_decision(requirement: str, archive_version: str) -> tuple[bool | None, str]:
    """Prove whether an Ubuntu source version satisfies one Python requirement.

    ``None`` is intentionally conservative: an unrepresentable Debian or PEP
    440 version causes a same-run candidate build instead of an archive guess.
    Environment markers are preserved as evidence but are not evaluated using
    the planner host, whose Python version can differ from Ubuntu 26.04.
    """
    value = re.split(r'\s+#', requirement.strip(), maxsplit=1)[0].strip()
    try:
        parsed = Requirement(value)
    except InvalidRequirement as exc:
        return None, f'requirement is not valid PEP 508: {exc}'
    if parsed.url:
        return None, 'direct URL requirements cannot be satisfied from an Ubuntu version'
    if not parsed.specifier:
        return True, 'Ubuntu provides the mapped distribution; no version constraint was declared'
    upstream = debian_upstream_version(archive_version)
    try:
        satisfied = parsed.specifier.contains(Version(upstream), prereleases=True)
    except InvalidVersion as exc:
        return None, f'Ubuntu upstream version {upstream!r} is not valid PEP 440: {exc}'
    relation = 'satisfies' if satisfied else 'does not satisfy'
    return satisfied, f'Ubuntu {archive_version} ({upstream}) {relation} {parsed.specifier}'


def requirement_name(requirement: str) -> str | None:
    """Return the distribution name from a PEP 508-style requirement.

    OpenStack requirements use ordinary distribution names followed by a
    version, extra, marker, or URL. Options and editable VCS requirements do
    not name a distribution unambiguously and are retained as unresolved
    evidence instead of guessed.
    """
    value = requirement.strip()
    if not value or value.startswith(('#', '-')):
        return None
    value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
    match = re.match(r'^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[|[<>=!~;@\s]|$)', value)
    return normalize_distribution(match.group(1)) if match else None


def parse_requirement_file(text: str, *, filename: str, kind: str) -> list[dict]:
    records = []
    for line_number, line in enumerate(text.splitlines(), 1):
        value = line.strip()
        if not value or value.startswith('#'):
            continue
        name = requirement_name(value)
        records.append({'distribution': name, 'requirement': value, 'kind': kind,
                        'file': filename, 'line': line_number})
    return records


def _string_requirements(values, *, filename: str, kind: str, group: str | None = None) -> list[dict]:
    records = []
    for index, value in enumerate(values or [], 1):
        if not isinstance(value, str):
            continue
        record = {'distribution': requirement_name(value), 'requirement': value,
                  'kind': kind, 'file': filename, 'line': index}
        if group:
            record['group'] = group
        records.append(record)
    return records


def parse_pyproject(text: str) -> list[dict]:
    data = tomllib.loads(text)
    records = []
    records.extend(_string_requirements(data.get('project', {}).get('dependencies', []),
                                        filename='pyproject.toml', kind='runtime'))
    records.extend(_string_requirements(data.get('build-system', {}).get('requires', []),
                                        filename='pyproject.toml', kind='build-system'))
    optional = data.get('project', {}).get('optional-dependencies', {})
    for group, values in optional.items():
        if normalize_distribution(group) in TEST_GROUP_NAMES:
            records.extend(_string_requirements(values, filename='pyproject.toml',
                                                kind='test', group=group))
    for group, values in data.get('dependency-groups', {}).items():
        if normalize_distribution(group) in TEST_GROUP_NAMES:
            records.extend(_string_requirements(values, filename='pyproject.toml',
                                                kind='test', group=group))
    return records


def raw_url(repository: str, sha: str, filename: str) -> str:
    parsed = urlparse(repository.rstrip('/'))
    path = parsed.path.strip('/').removesuffix('.git')
    if parsed.hostname == 'opendev.org':
        return f'{parsed.scheme}://{parsed.netloc}/{path}/raw/commit/{sha}/{filename}'
    if parsed.hostname in {'github.com', 'www.github.com'}:
        return f'https://raw.githubusercontent.com/{path}/{sha}/{filename}'
    raise MetadataFetchError(f'Unsupported upstream source host for dependency metadata: {repository}')


def fetch_optional(url: str, *, attempts: int = 3, timeout: int = 30) -> str | None:
    request = Request(url, headers={'User-Agent': 'packagetest-dependency-planner/1'})
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode()
        except HTTPError as exc:
            if exc.code == 404:
                return None
            error = exc
        except (URLError, TimeoutError, UnicodeDecodeError) as exc:
            error = exc
        if attempt + 1 < attempts:
            time.sleep(1 << attempt)
    raise MetadataFetchError(f'Failed to fetch {url}: {error}')


def inspect_revision(entry: dict, *, fetch=fetch_optional) -> tuple[list[dict], list[str]]:
    if not entry.get('upstream_sha'):
        raise MetadataFetchError(f'{entry["source"]} has no immutable upstream SHA')
    records = []
    present = []
    for filename in DEPENDENCY_FILES:
        url = raw_url(entry['upstream_repository'], entry['upstream_sha'], filename)
        text = fetch(url)
        if text is None:
            continue
        present.append(filename)
        if filename == 'requirements.txt':
            records.extend(parse_requirement_file(text, filename=filename, kind='runtime'))
        elif filename == 'test-requirements.txt':
            records.extend(parse_requirement_file(text, filename=filename, kind='test'))
        else:
            records.extend(parse_pyproject(text))
    return records, present


def distribution_source_index(packages: list[dict]) -> dict[str, str]:
    """Map unambiguous Python distribution spellings to Ubuntu sources."""
    candidates: dict[str, set[str]] = {}
    for entry in packages:
        source = entry['source']
        names = {source, entry.get('deliverable', '')}
        for prefix in ('python-', 'python3-'):
            if source.startswith(prefix):
                names.add(source[len(prefix):])
        for binary in entry.get('binaries', []):
            names.add(binary)
            if binary.startswith('python3-'):
                names.add(binary[len('python3-'):])
        for name in names - {''}:
            candidates.setdefault(normalize_distribution(name), set()).add(source)
    # Common Python import/distribution name differences not represented by
    # Ubuntu's binary package spelling.
    aliases = {'pyyaml': 'python3-yaml', 'python-dateutil': 'python3-dateutil'}
    binary_sources = {binary: entry['source'] for entry in packages for binary in entry.get('binaries', [])}
    for distribution, binary in aliases.items():
        if binary in binary_sources:
            candidates.setdefault(distribution, set()).add(binary_sources[binary])
    return {name: next(iter(sources)) for name, sources in candidates.items() if len(sources) == 1}


def distribution_archive_index(records: list[dict]) -> dict[str, dict]:
    """Return unambiguous Python distribution providers from Ubuntu indexes."""
    candidates: dict[str, list[dict]] = {}
    for original in records:
        record = dict(original)
        distribution = normalize_distribution(record['distribution'])
        candidates.setdefault(distribution, []).append(record)
    for alias, distribution in ARCHIVE_DISTRIBUTION_ALIASES.items():
        if distribution in candidates:
            candidates.setdefault(alias, []).extend(candidates[distribution])
    return {name: values[0] for name, values in candidates.items()
            if len({(value['binary'], value['source']) for value in values}) == 1}


def map_requirements(records: list[dict], index: dict[str, str], consumer: str,
                     packages: dict[str, dict] | None = None,
                     archive_index: dict[str, dict] | None = None) -> tuple[list[dict], list[str]]:
    mapped = []
    dependencies = set()
    packages = packages or {}
    archive_index = archive_index or {}
    for original in records:
        record = dict(original)
        source = index.get(record['distribution']) if record.get('distribution') else None
        if source and source != consumer:
            record['source'] = source
            archive_version = packages.get(source, {}).get('archive_version')
            record['archive_version'] = archive_version
            if archive_version:
                satisfied, reason = archive_requirement_decision(record['requirement'], archive_version)
            else:
                satisfied, reason = None, 'mapped Ubuntu source has no archive version evidence'
            record['archive_satisfies'] = satisfied
            record['archive_decision'] = ('satisfied' if satisfied is True else
                                          'insufficient' if satisfied is False else 'unknown')
            record['archive_decision_reason'] = reason
            if not satisfied:
                dependencies.add(source)
        elif source == consumer:
            record['source'] = source
            record['archive_decision_reason'] = 'Requirement is provided by the source being built'
        elif record.get('distribution') in archive_index:
            provider = archive_index[record['distribution']]
            record.update(archive_binary=provider['binary'],
                          archive_source=provider['source'],
                          archive_version=provider['version'])
            satisfied, reason = archive_requirement_decision(
                record['requirement'], provider['version'])
            record['archive_satisfies'] = satisfied
            record['archive_decision'] = ('satisfied' if satisfied is True else
                                          'insufficient' if satisfied is False else 'unknown')
            record['archive_decision_reason'] = reason
        elif record.get('distribution'):
            record['archive_decision'] = 'unmapped'
            record['archive_decision_reason'] = (
                'No unambiguous Ubuntu python3 binary provider was found')
        mapped.append(record)
    return mapped, sorted(dependencies)
