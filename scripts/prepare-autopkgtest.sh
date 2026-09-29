#!/usr/bin/env bash
# Run on a disposable GitHub runner. Outputs the image path on stdout.
set -euo pipefail
suite=${1:-resolute}
output=${2:-artifacts/autopkgtest-image}
cache=${AUTOPKGTEST_IMAGE_CACHE:-}
case "$suite" in resolute|noble|stonking) ;; *) echo "Unsupported suite: $suite" >&2; exit 2;; esac
mkdir -p "$output"
output=$(realpath "$output")
exec 2> >(tee "$output/prepare.log" >&2)
sudo apt-get update >&2
sudo apt-get install -y autopkgtest autodep8 qemu-system-x86 qemu-utils cloud-image-utils dpkg-dev >&2
if [[ ! -e /dev/kvm ]]; then
  echo 'KVM is required for the remote autopkgtest VM; use a KVM-capable runner.' >&2
  exit 1
fi
sudo chmod 666 /dev/kvm

if [[ -n "$cache" ]]; then
  mkdir -p "$cache"
  cache=$(realpath "$cache")
  cached_image="$cache/autopkgtest-$suite-amd64.img"
  cached_checksum="$cache/autopkgtest-$suite-amd64.img.sha256"
  if [[ -f "$cached_image" || -f "$cached_checksum" ]]; then
    test -f "$cached_image" && test -f "$cached_checksum"
    (cd "$cache" && sha256sum --check "$(basename "$cached_checksum")") >&2
    qemu-img check "$cached_image" >&2
    printf '%s\n' "$cached_image"
    exit 0
  fi
fi

autopkgtest-buildvm-ubuntu-cloud --release="$suite" --arch=amd64 --ram-size=2048 --cpus=2 \
  --cloud-image-url=https://cloud-images.ubuntu.com --output-dir="$output" --timeout=1800 >&2
image="$output/autopkgtest-$suite-amd64.img"
test -f "$image"
if [[ -n "$cache" ]]; then
  cached_image="$cache/$(basename "$image")"
  cp --reflink=auto "$image" "$cached_image"
  (cd "$cache" && sha256sum "$(basename "$cached_image")" > "$(basename "$cached_image").sha256")
  qemu-img check "$cached_image" >&2
  image="$cached_image"
fi
printf '%s\n' "$image"
