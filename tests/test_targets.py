import json
from pathlib import Path
import pytest
from packagetest.locked import load_lock

LOCKS = Path(__file__).resolve().parents[1] / 'config/locks'


@pytest.mark.parametrize('name', ['oslo-i18n-noble-uca-epoxy', 'oslo-i18n-noble-uca-epoxy-baseline', 'glance-noble-uca-epoxy'])
def test_uca_locks_valid(name):
    assert load_lock(LOCKS / (name + '.json'))['target']['suite'] == 'noble'


@pytest.mark.parametrize('field,value', [('chroot', 'noble-amd64-sbuild'), ('pockets', ['noble']), ('openstack_series', '2026.1')])
def test_wrong_uca_target_rejected(tmp_path, field, value):
    lock = json.loads((LOCKS / 'oslo-i18n-noble-uca-epoxy.json').read_text())
    lock['target'][field] = value
    path = tmp_path / 'lock.json'
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match='UCA Epoxy target requires'):
        load_lock(path)


@pytest.mark.parametrize('change', ['branch', 'version', 'downgrade'])
def test_uca_revision_policy(tmp_path, change):
    lock = json.loads((LOCKS / 'oslo-i18n-noble-uca-epoxy.json').read_text())
    package = lock['packages'][0]
    if change == 'branch':
        package['input']['packaging_branch'] = 'master'
    elif change == 'version':
        package['version'] = '6.5.1-0ubuntu2'
    else:
        package['previous_target_version'] = '6.5.1-0ubuntu2~cloud0'
    path = tmp_path / 'lock.json'
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match='UCA'):
        load_lock(path)
