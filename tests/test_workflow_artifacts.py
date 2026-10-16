"""Exercise complete artifact retrieval without network access or workflow credentials."""
import hashlib
import io
import json
import runpy
import stat
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import pytest

module = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/download-workflow-artifacts.py'))
WorkflowArtifacts = module['WorkflowArtifacts']


def zip_bytes(name: str = 'result.json', content: bytes = b'{"result":"SUCCEEDED"}') -> bytes:
    """Create one in-memory artifact without depending on a live Actions service."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr(name, content)

    return output.getvalue()


class FakeAPI:
    """Serve paginated metadata and ZIP bytes through urllib's response interface."""

    def __init__(self, artifacts: list[dict], archive: bytes) -> None:
        """Retain request evidence and the simulated run inventory."""
        self.artifacts = artifacts
        self.archive = archive
        self.requests = []

    def open(self, request: Request, timeout: int) -> io.BytesIO:
        """Return one REST page or a single artifact's ZIP payload."""
        self.requests.append(request)
        parsed = urlparse(request.full_url)
        if parsed.path.endswith('/artifacts'):
            page = int(parse_qs(parsed.query)['page'][0])
            return io.BytesIO(json.dumps({'artifacts': self.artifacts[(page - 1) * 100:page * 100]}).encode())

        return io.BytesIO(self.archive)


def test_status_beyond_first_thousand_artifacts_is_downloaded(tmp_path: Path) -> None:
    """The summary must retrieve statuses even when unrelated artifacts exceed 1,000."""
    archive = zip_bytes()
    artifacts = [{'id': index, 'name': f'unrelated-{index}', 'expired': False}
                 for index in range(1205)]
    artifacts[1100] = {'id': 1100, 'name': 'status-target-build-demo', 'expired': False,
                       'digest': 'sha256:' + hashlib.sha256(archive).hexdigest()}
    api = FakeAPI(artifacts, archive)
    client = WorkflowArtifacts('owner/repo', '123', 'test-token', opener=api)

    assert client.download('status-target-*', tmp_path) == 1
    assert json.loads((tmp_path / 'status-target-build-demo/result.json').read_text())['result'] == 'SUCCEEDED'
    assert sum('/artifacts?' in request.full_url for request in api.requests) == 13
    assert all(request.get_header('Authorization') == 'Bearer test-token' for request in api.requests)


def test_blob_redirect_drops_github_credentials() -> None:
    """The initial API token must not accompany a signed storage redirect."""
    handler = module['ArtifactRedirectHandler']()
    request = Request('https://api.github.com/repos/owner/repo/actions/artifacts/1/zip',
                      headers={'Authorization': 'Bearer test-token'})
    redirected = handler.redirect_request(request, None, 302, 'Found', {},
                                           'https://storage.example/artifact.zip?signature=value')
    assert redirected.get_header('Authorization') is None
    assert request.get_header('Authorization') == 'Bearer test-token'


@pytest.mark.parametrize('name', ['../outside', '/outside', r'directory\outside'])
def test_unsafe_zip_paths_are_rejected_before_extraction(tmp_path: Path, name: str) -> None:
    """An artifact must not write outside its staging directory."""
    archive = tmp_path / 'artifact.zip'
    archive.write_bytes(zip_bytes(name))
    with pytest.raises(ValueError, match='Unsafe artifact member'):
        module['extract_archive'](archive, tmp_path / 'staging')

    assert not (tmp_path / 'staging').exists()


def test_zip_links_and_conflicting_producer_files_are_rejected(tmp_path: Path) -> None:
    """Neither links nor merged collisions may redirect or replace a producer bundle."""
    member = zipfile.ZipInfo('link')
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError, match='Symlink'):
        module['validate_zip_member'](member)

    staging = tmp_path / 'staging'
    destination = tmp_path / 'downloads'
    staging.mkdir()
    destination.mkdir()
    (staging / 'demo.tar.gz').write_bytes(b'new')
    (destination / 'demo.tar.gz').write_bytes(b'original')
    with pytest.raises(ValueError, match='Conflicting artifact member'):
        module['publish_archive'](staging, destination, 'build-demo', True)

    assert (destination / 'demo.tar.gz').read_bytes() == b'original'


def test_optional_empty_patterns_and_exact_producer_lists(tmp_path: Path) -> None:
    """Optional proposals can be absent while required statuses cannot disappear silently."""
    client = WorkflowArtifacts('owner/repo', '123', 'token', opener=FakeAPI([], b''))
    assert client.download('packaging-proposal-*', tmp_path, allow_empty=True) == 0
    with pytest.raises(ValueError, match='No workflow artifacts'):
        client.download('status-*', tmp_path)

    assert module['safe_artifact_name']('build-demo+plugin') == 'build-demo+plugin'
    assert module['matches']('build-a', '{build-a,build-b}')
    assert not module['matches']('build-c', '{build-a,build-b}')


@pytest.mark.parametrize('records, message', [
    ([{'name': 'status-a', 'expired': True}], 'expired'),
    ([{'name': 'status-a', 'expired': False}] * 2, 'Duplicate'),
    ([{'name': '../unsafe', 'expired': False}], 'Unsafe artifact name'),
])
def test_inventory_validation_rejects_unusable_matches(records: list[dict], message: str) -> None:
    """Ambiguous, expired, or unsafe matches must cause an explicit retrieval failure."""
    with pytest.raises(ValueError, match=message):
        module['validate_selection'](records)


def test_corrupted_artifact_does_not_publish_partial_output(tmp_path: Path) -> None:
    """A failed service digest must leave no seemingly valid result directory."""
    api = FakeAPI([{'id': 1, 'name': 'status-a', 'expired': False, 'digest': 'sha256:' + '0' * 64}], zip_bytes())
    client = WorkflowArtifacts('owner/repo', '123', 'token', opener=api)
    with pytest.raises(ValueError, match='checksum mismatch'):
        client.download('status-*', tmp_path)

    assert list(tmp_path.iterdir()) == []


class GrowingAPI(FakeAPI):
    """Prepend an upload between pages to reproduce overlapping live API results."""

    def open(self, request: Request, timeout: int) -> io.BytesIO:
        """Shift the second page by one record before serving its offset."""
        parsed = urlparse(request.full_url)
        if parsed.path.endswith('/artifacts') and parse_qs(parsed.query)['page'] == ['2']:
            self.artifacts.insert(0, {'id': 10000, 'name': 'new-upload', 'expired': False})

        return super().open(request, timeout)


def test_concurrent_upload_does_not_make_one_artifact_ambiguous(tmp_path):
    """Repeated IDs across live pages must download once without losing older producers."""
    artifacts = [{'id': i, 'name': f'build-{i}', 'expired': False} for i in range(250)]
    api = GrowingAPI(artifacts, zip_bytes('producer.tar.gz'))
    client = WorkflowArtifacts('owner/repo', '123', 'token', opener=api)
    assert client.download('{build-99,build-249}', tmp_path) == 2
    assert (tmp_path / 'build-99/producer.tar.gz').is_file()
    assert (tmp_path / 'build-249/producer.tar.gz').is_file()


def test_distinct_artifacts_with_one_name_remain_ambiguous(tmp_path):
    """Coalescing pagination overlap must not choose between separate uploaded objects."""
    api = FakeAPI([{'id': i, 'name': 'build-demo', 'expired': False} for i in (1, 2)], zip_bytes())
    client = WorkflowArtifacts('owner/repo', '123', 'token', opener=api)
    with pytest.raises(ValueError, match='Duplicate matching artifact names'):
        client.download('build-*', tmp_path)


def test_repeated_id_with_changed_identity_is_rejected():
    """Repeated IDs cannot silently substitute a different name or checksum."""
    client = WorkflowArtifacts('owner/repo', '123', 'token', opener=FakeAPI([], b''))
    inventory = {1: {'id': 1, 'name': 'build-demo', 'digest': 'sha256:original'}}
    with pytest.raises(ValueError, match='identity changed'):
        client._record_page(inventory, [{'id': 1, 'name': 'build-demo', 'digest': 'sha256:changed'}])
