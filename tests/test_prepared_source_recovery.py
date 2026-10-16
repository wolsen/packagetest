"""Recover a repair baseline from actual checksum-pinned Debian source bundles."""
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

from packagetest.prepared_source import export_prepared
from packagetest.remediation import prepared_tree


pytestmark = pytest.mark.packaging_component


@pytest.fixture
def frozen_bundle(tmp_path: Path) -> tuple[Path, Path, dict]:
    """Export a quilt source bundle with one real upstream change for recovery tests."""
    tree = tmp_path / 'demo-1.0'
    tree.mkdir()
    (tree / 'module.py').write_text('original upstream\n')
    with tarfile.open(tmp_path / 'demo_1.0.orig.tar.gz', 'w:gz') as archive:
        archive.add(tree, arcname=tree.name)

    debian = tree / 'debian'
    (debian / 'source').mkdir(parents=True)
    (debian / 'source/format').write_text('3.0 (quilt)\n')
    (debian / 'control').write_text(
        'Source: demo\nMaintainer: Test <test@example.invalid>\nStandards-Version: 4.6.2\n\n'
        'Package: python3-demo\nArchitecture: all\nDescription: test fixture\n')
    (debian / 'changelog').write_text(
        'demo (1.0-1) unstable; urgency=medium\n\n  * Fixture.\n\n'
        ' -- Test <test@example.invalid>  Thu, 01 Jan 2026 00:00:00 +0000\n')
    (debian / 'patches').mkdir()
    (debian / 'patches/series').write_text('upstream.patch\n')
    (debian / 'patches/upstream.patch').write_text(
        '--- a/module.py\n+++ b/module.py\n@@ -1 +1 @@\n-original upstream\n+packaged upstream\n')
    subprocess.run(['dpkg-source', '--before-build', str(tree)], check=True, capture_output=True)
    subprocess.run(['dpkg-source', '-b', str(tree)], cwd=tmp_path, check=True, capture_output=True)
    resolution = tmp_path / 'resolution.json'
    resolution.write_text('{}')
    ci = {'run_id': '123', 'run_attempt': '1'}
    entry = {'source': 'demo', 'upstream_sha': 'a' * 40}
    outputs = tmp_path / 'outputs'
    bundle = outputs / 'source-preparation'
    manifest = export_prepared(
        {'dsc': str(tmp_path / 'demo_1.0-1.dsc'), 'tree': str(tree),
         'lock': str(resolution), 'metadata': {}}, entry, {'ci': ci}, bundle)
    entry['prepared_source'] = {key: manifest[key] for key in ('dsc_sha256', 'catalog_entry_sha256')}
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps({'ci': ci, 'packages': [entry]}))
    return outputs, catalog, ci


def test_frozen_source_is_recovered_without_applying_quilt(frozen_bundle: tuple[Path, Path, dict]) -> None:
    """Bundle-only jobs must supply the original source and Debian patches to remediation."""
    outputs, catalog, ci = frozen_bundle
    tree = prepared_tree(outputs, catalog=catalog, source='demo', ci=ci)
    assert (tree / 'debian/control').is_file()
    assert (tree / 'module.py').read_text() == 'original upstream\n'
    assert (tree / 'debian/patches/series').read_text() == 'upstream.patch\n'
    assert prepared_tree(outputs, catalog=catalog, source='demo', ci=ci) == tree
    assert not list((outputs / 'source-preparation').glob('.remediation-*'))


@pytest.mark.parametrize('change', ['run', 'plan', 'artifact'])
def test_recovery_rejects_changed_frozen_inputs(frozen_bundle: tuple[Path, Path, dict], change: str) -> None:
    """Run, finalized-plan, and source-byte mismatches must fail before a repair tree appears."""
    outputs, catalog, ci = frozen_bundle
    bundle = outputs / 'source-preparation'
    if change == 'run':
        ci = {**ci, 'run_attempt': '2'}
    elif change == 'plan':
        data = json.loads(catalog.read_text())
        data['packages'][0]['prepared_source']['dsc_sha256'] = '0' * 64
        catalog.write_text(json.dumps(data))
    else:
        archive = next((bundle / 'source').glob('*.orig.tar.gz'))
        archive.write_bytes(b'changed')

    with pytest.raises(ValueError):
        prepared_tree(outputs, catalog=catalog, source='demo', ci=ci)

    assert not (bundle / 'remediation-source').exists()
