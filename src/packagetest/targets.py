"""Explicit target policy for the first supported Ubuntu Cloud Archive profile."""
import re
import subprocess

UCA_POCKETS = ['noble', 'noble-updates', 'noble-security', 'noble-updates/epoxy']


def validate_target(lock):
    target = lock['target']
    profile = target.get('profile')
    if not profile:
        return
    if profile != 'noble-uca-epoxy':
        raise ValueError('Unsupported target profile')
    expected = {'suite': 'noble', 'architecture': 'amd64', 'chroot': 'noble-uca-epoxy-amd64-sbuild',
                'openstack_series': '2025.1', 'pockets': UCA_POCKETS,
                'cloud_mirror': 'http://ubuntu-cloud.archive.canonical.com/ubuntu',
                'cloud_keyring': '/usr/share/keyrings/ubuntu-cloud-keyring.gpg'}
    for key, value in expected.items():
        if target.get(key) != value:
            raise ValueError(f'UCA Epoxy target requires {key}={value}')
    for package in lock['packages']:
        if '~cloud' not in package['version']:
            raise ValueError('UCA package requires an explicit ~cloud revision')
        spec = package['input']
        if spec['kind'] == 'gbp':
            if (spec['packaging_branch'], spec['upstream_branch']) != ('stable/2025.1', 'upstream-epoxy'):
                raise ValueError('UCA Epoxy requires its stable packaging branches')
            base = package.get('backport_of', '')
            if not base or not re.fullmatch(re.escape(base) + r'~cloud[0-9]+(?:\+packagetest[0-9]+)?', package['version']):
                raise ValueError('UCA backport version must extend its pinned base with ~cloudN')
            previous = package.get('previous_target_version', '')
            if not previous or subprocess.run(['dpkg', '--compare-versions', package['version'], 'gt', previous], capture_output=True).returncode:
                raise ValueError('UCA backport must advance the pinned target version')
