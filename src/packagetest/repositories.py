"""Share target archive policy between dependency planning, sbuild, and testbeds."""
from __future__ import annotations

import argparse
import shlex
from urllib.parse import urlparse


PPA_FINGERPRINT = '992D4F030DBED72FEE21747D540315018F3594DF'
PPA_KEYRING = '/usr/share/keyrings/packagetest-uca-ppa.gpg'


def is_uca_ppa_source(entry: dict) -> bool:
    """Return whether the frozen source descriptor comes from a UCA PPA."""
    url = entry.get('archive_source', {}).get('url', '')
    parsed = urlparse(url)
    return (parsed.hostname in {'ppa.launchpad.net', 'ppa.launchpadcontent.net'}
            and parsed.path.startswith('/ubuntu-cloud-archive/'))


def distro_repositories(suite: str) -> list[dict]:
    """Enable release, updates, security, and proposed with the same components."""
    return [dict(repository=host, distribution=suite + pocket, component='main universe',
                 keyring='/usr/share/keyrings/ubuntu-archive-keyring.gpg')
            for pocket, host in [('', 'http://archive.ubuntu.com/ubuntu'),
                                 ('-updates', 'http://archive.ubuntu.com/ubuntu'),
                                 ('-security', 'http://security.ubuntu.com/ubuntu'),
                                 ('-proposed', 'http://archive.ubuntu.com/ubuntu')]]


def target_repositories(target: dict) -> list[dict]:
    """Add the release-specific UCA archives only to targets declaring a Cloud Archive."""
    repositories = distro_repositories(target['suite'])
    cloud = target.get('cloud_archive')
    if cloud:
        repositories.append(dict(cloud))
        codename = cloud['distribution'].split('/')[-1]
        repositories.extend(dict(
            repository=f'https://ppa.launchpadcontent.net/ubuntu-cloud-archive/{codename}-{pocket}/ubuntu',
            distribution=target['suite'], component='main', keyring=PPA_KEYRING)
            for pocket in ('updates', 'staging', 'proposed'))

    return repositories


def repository_line(repository: dict) -> str:
    """Render an authenticated APT source without enabling unsigned repositories."""
    return (f"deb [signed-by={repository['keyring']}] {repository['repository']} "
            f"{repository['distribution']} {repository['component']}")


def binary_indexes(target: dict) -> list[dict]:
    """Describe architecture indexes for precisely the repositories enabled in the guest."""
    indexes = []
    for number, repository in enumerate(target_repositories(target)):
        for component in repository['component'].split():
            root = repository['repository'].replace('http://', 'https://')
            url = f"{root}/dists/{repository['distribution']}/{component}/binary-{target.get('architecture', 'amd64')}/Packages.gz"
            indexes.append({'filename': f'binary-{number}-{component}-Packages.gz', 'url': url})

    return indexes


def _ppa_key_setup() -> str:
    """Fetch the pinned UCA PPA key and verify its primary fingerprint before installation."""
    url = f'https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x{PPA_FINGERPRINT}'
    return (
        'DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg ubuntu-cloud-keyring\n'
        'keydir=$(mktemp -d)\n'
        'trap \'rm -rf "$keydir"\' EXIT\n'
        f'curl --fail --silent --show-error --retry 3 {shlex.quote(url)} -o "$keydir/key.asc"\n'
        'fingerprint=$(gpg --batch --with-colons --show-keys "$keydir/key.asc" '
        '| awk -F: \'$1 == "fpr" {print $10; exit}\')\n'
        f'test "$fingerprint" = {PPA_FINGERPRINT}\n'
        f'gpg --batch --yes --dearmor -o {PPA_KEYRING} "$keydir/key.asc"\n'
        f'chmod 0644 {PPA_KEYRING}\n'
        'rm -rf "$keydir"\ntrap - EXIT\n')


def repository_setup(target: dict) -> str:
    """Install matching APT sources and equal priorities while leaving local candidate pins intact."""
    repositories = target_repositories(target)
    setup = 'set -eu\n'
    if target.get('cloud_archive'):
        setup += _ppa_key_setup()

    lines = '\n'.join(map(repository_line, repositories)) + '\n'
    # Replace image defaults so inherited sources cannot leak into this target.
    setup += 'rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources\n'
    setup += f"printf '%s' {shlex.quote(lines)} > /etc/apt/sources.list.d/packagetest-target.list\n"
    pins = '\n'.join(f"Package: *\nPin: release n={r['distribution']}\nPin-Priority: 500\n" for r in repositories)
    setup += f"printf '%s' {shlex.quote(pins)} > /etc/apt/preferences.d/packagetest-target\n"
    return setup + 'apt-get update\n'


def profile_target(profile: str) -> dict:
    """Translate a builder profile into the same minimal target used by repository policy."""
    suite, separator, codename = profile.partition('-uca-')
    target = {'suite': suite}
    if separator:
        target['cloud_archive'] = dict(
            repository='http://ubuntu-cloud.archive.canonical.com/ubuntu',
            distribution=f'{suite}-updates/{codename}', component='main',
            keyring='/usr/share/keyrings/ubuntu-cloud-keyring.gpg')

    return target


def main() -> None:
    """Emit the shared setup script for an explicitly selected builder profile."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile')
    args = parser.parse_args()
    print(repository_setup(profile_target(args.profile)), end='')


if __name__ == '__main__':
    main()
