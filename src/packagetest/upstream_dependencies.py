"""Read Python dependency metadata from an immutable upstream revision."""
from __future__ import annotations

import re
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEPENDENCY_FILES = ('requirements.txt', 'test-requirements.txt', 'pyproject.toml')
TEST_GROUP_NAMES = {'test', 'tests', 'testing'}


class MetadataFetchError(RuntimeError):
    """An upstream dependency file could not be read reliably."""


def normalize_distribution(name: str) -> str:
    """Use the same spelling equivalence as Python package indexes."""
    return re.sub(r'[-_.]+', '-', name).lower()


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


def map_requirements(records: list[dict], index: dict[str, str], consumer: str) -> tuple[list[dict], list[str]]:
    mapped = []
    dependencies = set()
    for original in records:
        record = dict(original)
        source = index.get(record['distribution']) if record.get('distribution') else None
        if source and source != consumer:
            record['source'] = source
            dependencies.add(source)
        mapped.append(record)
    return mapped, sorted(dependencies)
