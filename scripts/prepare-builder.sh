#!/usr/bin/env bash
# Run inside a disposable Ubuntu 24.04 runner. Never changes host sbuild defaults.
set -euo pipefail
if [[ $(. /etc/os-release; echo "$ID:$VERSION_ID") != ubuntu:24.04 ]]; then
    echo 'This builder profile requires Ubuntu 24.04.' >&2
    exit 1
fi
suite=${1:-noble}
case "$suite" in noble|stonking) ;; *) echo 'Supported targets: noble, stonking' >&2; exit 2 ;; esac
chroot_name="$suite-amd64-sbuild"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    git git-buildpackage pristine-tar devscripts dpkg-dev debhelper dh-python \
    openstack-pkg-tools sbuild schroot debootstrap ubuntu-keyring lintian \
    python3-venv python3-pip python3-pytest python3-setuptools python3-wheel
sudo sbuild-adduser "$USER"
# File-based schroot creates a new extracted build environment for every session.
# Refuse stale partial bootstrap state rather than silently reusing it.
if ! schroot --list | grep -qx "chroot:$chroot_name"; then
    sudo mkdir -p /srv/chroot
    repositories=()
    if [[ "$suite" == noble ]]; then
        repositories+=(--extra-repository='deb http://archive.ubuntu.com/ubuntu noble-updates main universe')
        repositories+=(--extra-repository='deb http://security.ubuntu.com/ubuntu noble-security main universe')
    fi
    # Ubuntu suites share this debootstrap implementation; explicitly select it
    # when the Noble host predates a development suite's alias.
    sudo sbuild-createchroot --arch=amd64 --components=main,universe \
        --make-sbuild-tarball="/srv/chroot/$chroot_name.tar.gz" \
        "${repositories[@]}" "$suite" "/srv/chroot/$suite-amd64-bootstrap" \
        http://archive.ubuntu.com/ubuntu /usr/share/debootstrap/scripts/gutsy
fi
sg sbuild -c "schroot -c $chroot_name --directory / -- true"
