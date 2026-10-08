#!/usr/bin/env bash
# Run inside a disposable Ubuntu 24.04 runner. Never changes host sbuild defaults.
set -euo pipefail
if [[ $(. /etc/os-release; echo "$ID:$VERSION_ID") != ubuntu:24.04 ]]; then
    echo 'This builder profile requires Ubuntu 24.04.' >&2
    exit 1
fi
profile=${1:-noble}
suite=$profile
case "$profile" in noble|resolute|stonking|noble-uca-epoxy) ;; *) echo 'Supported targets: noble, resolute, stonking, noble-uca-epoxy' >&2; exit 2 ;; esac
if [[ "$profile" == noble-uca-epoxy ]]; then suite=noble; fi
chroot_name="$profile-amd64-sbuild"
cache=${SBUILD_ROOTFS_CACHE:-}
host_cache=${SBUILD_HOST_APT_CACHE:-}
cache_tarball=""
cache_checksum=""
if [[ -n "$cache" ]]; then
    mkdir -p "$cache"
    cache=$(realpath "$cache")
    cache_tarball="$cache/$chroot_name.tar.gz"
    cache_checksum="$cache/$chroot_name.tar.gz.sha256"
fi
if [[ -n "$host_cache" ]]; then
    mkdir -p "$host_cache"
    host_cache=$(realpath "$host_cache")
fi
host_packages=(
    git git-buildpackage pristine-tar devscripts dpkg-dev debhelper dh-python
    openstack-pkg-tools sbuild schroot debootstrap ubuntu-keyring ubuntu-cloud-keyring lintian
    python3-venv python3-pip python3-pytest python3-setuptools python3-wheel
)
apt_options=(
    -o Acquire::Retries=3
    -o Acquire::http::Timeout=30
    -o Acquire::https::Timeout=30
    -o DPkg::Lock::Timeout=300
)
host_manifest="$host_cache/packages.sha256"
if [[ -n "$host_cache" && -f "$host_manifest" ]]; then
    (cd "$host_cache" && sha256sum --check packages.sha256)
    # Restore both halves of apt's offline state: the signed indexes used for
    # resolution and the archives directory where apt expects every selected
    # package. Passing all .debs as local arguments is insufficient: apt can
    # still schedule their dependencies as archive downloads and --no-download
    # then fails even though matching files exist elsewhere in the workspace.
    sudo rm -rf /var/lib/apt/lists/*
    sudo tar -xzf "$host_cache/apt-lists.tar.gz" -C /var/lib/apt/lists
    sudo rm -f /var/cache/apt/archives/*.deb
    sudo cp "$host_cache"/*.deb /var/cache/apt/archives/
    # The cache preparation job resolved this complete tool set against the
    # same GitHub runner image. Do not contact an archive from package jobs.
    sudo timeout --signal=TERM --kill-after=30s 15m \
        env DEBIAN_FRONTEND=noninteractive apt-get --no-download \
        -o DPkg::Lock::Timeout=300 install -y "${host_packages[@]}"
else
    # Only the cache preparation job reaches this branch. Hosted-runner mirrors
    # and package locks still receive their own bounds.
    sudo timeout --signal=TERM --kill-after=30s 10m \
        apt-get "${apt_options[@]}" update
    sudo rm -f /var/cache/apt/archives/*.deb
    sudo timeout --signal=TERM --kill-after=30s 15m \
        env DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" \
        --download-only install -y "${host_packages[@]}"
    if [[ -n "$host_cache" ]]; then
        rm -f "$host_cache"/*.deb "$host_cache/apt-lists.tar.gz" "$host_manifest"
        sudo cp /var/cache/apt/archives/*.deb "$host_cache/"
        sudo tar -czf "$host_cache/apt-lists.tar.gz" -C /var/lib/apt/lists .
        sudo chown -R "$USER:$USER" "$host_cache"
        (cd "$host_cache" && sha256sum ./*.deb apt-lists.tar.gz > packages.sha256)
        # The downloaded archives are already in apt's normal cache here.
        # Install by package name so this path exercises the same operation
        # that restored package jobs perform.
        sudo timeout --signal=TERM --kill-after=30s 15m \
            env DEBIAN_FRONTEND=noninteractive apt-get --no-download \
            -o DPkg::Lock::Timeout=300 install -y "${host_packages[@]}"
    else
        sudo timeout --signal=TERM --kill-after=30s 15m \
            env DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" \
            install -y "${host_packages[@]}"
    fi
fi
for package in "${host_packages[@]}"; do
    dpkg-query -W -f='${db:Status-Abbrev}\n' "$package" | grep -qx 'ii '
done
sudo sbuild-adduser "$USER"
# File-based schroot creates a new extracted build environment for every session.
# Refuse stale partial bootstrap state rather than silently reusing it.
if ! schroot --list | grep -qx "chroot:$chroot_name"; then
    sudo mkdir -p /srv/chroot
    if [[ -n "$cache" && ( -f "$cache_tarball" || -f "$cache_checksum" ) ]]; then
        test -f "$cache_tarball" && test -f "$cache_checksum"
        (cd "$cache" && sha256sum --check "$(basename "$cache_checksum")")
        sudo install -m 0644 "$cache_tarball" "/srv/chroot/$chroot_name.tar.gz"
        config=$(mktemp)
        cat > "$config" <<EOF
[$chroot_name]
description=Ubuntu $suite/amd64 packagetest autobuilder
groups=root,sbuild
root-groups=root,sbuild
profile=sbuild
type=file
file=/srv/chroot/$chroot_name.tar.gz
EOF
        sudo install -m 0644 "$config" "/etc/schroot/chroot.d/packagetest-$chroot_name"
        rm -f "$config"
    else
        repositories=()
        if [[ "$suite" == noble || "$suite" == resolute ]]; then
            repositories+=(--extra-repository="deb http://archive.ubuntu.com/ubuntu $suite-updates main universe")
            repositories+=(--extra-repository="deb http://security.ubuntu.com/ubuntu $suite-security main universe")
        fi
        includes=()
        if [[ "$profile" == noble-uca-epoxy ]]; then
            includes+=(--include=ubuntu-cloud-keyring)
            repositories+=(--extra-repository='deb [signed-by=/usr/share/keyrings/ubuntu-cloud-keyring.gpg] http://ubuntu-cloud.archive.canonical.com/ubuntu noble-updates/epoxy main')
        fi
        # Ubuntu suites share this debootstrap implementation; explicitly select it
        # when the Noble host predates a development suite's alias.
        sudo sbuild-createchroot --arch=amd64 --components=main,universe --chroot-prefix="$profile" \
            --make-sbuild-tarball="/srv/chroot/$chroot_name.tar.gz" \
            "${includes[@]}" "${repositories[@]}" "$suite" "/srv/chroot/$profile-amd64-bootstrap" \
            http://archive.ubuntu.com/ubuntu /usr/share/debootstrap/scripts/gutsy
        # Persist the upgraded source chroot, rather than the initial debootstrap
        # result, so package jobs do not repeat the base upgrade work.
        sudo sbuild-update -udcar "$chroot_name"
        if [[ -n "$cache" ]]; then
            cp --reflink=auto "/srv/chroot/$chroot_name.tar.gz" "$cache_tarball"
            (cd "$cache" && sha256sum "$(basename "$cache_tarball")" > "$(basename "$cache_checksum")")
        fi
    fi
fi
sg sbuild -c "schroot -c $chroot_name --directory / -- true"
