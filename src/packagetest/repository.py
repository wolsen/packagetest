from __future__ import annotations

from shlex import quote
from pathlib import Path


def apt_repository_commands(repo_dir: Path, distribution: str, key_name: str = "Packagetest Ephemeral") -> list[list[str]]:
    pool_dir = quote(str(repo_dir / "pool"))
    dists_dir = repo_dir / "dists" / distribution
    binary_dir = dists_dir / "main" / "binary-amd64"
    packages_file = quote(str(binary_dir / "Packages"))
    release_file = quote(str(dists_dir / "Release"))
    dists_dir_q = quote(str(dists_dir))
    distribution_q = quote(distribution)
    return [
        ["mkdir", "-p", str(repo_dir / "pool")],
        ["mkdir", "-p", str(binary_dir)],
        ["bash", "-lc", f"apt-ftparchive packages {pool_dir} > {packages_file}"],
        ["gzip", "-kf", str(binary_dir / "Packages")],
        [
            "bash",
            "-lc",
            (
                "apt-ftparchive "
                f"-o APT::FTPArchive::Release::Suite={distribution_q} "
                f"-o APT::FTPArchive::Release::Codename={distribution_q} "
                f"-o APT::FTPArchive::Release::Components=main "
                f"-o APT::FTPArchive::Release::Architectures=amd64 "
                f"release {dists_dir_q} > {release_file}"
            ),
        ],
        [
            "gpg",
            "--batch",
            "--yes",
            "--quick-gen-key",
            key_name,
            "rsa2048",
            "sign",
            "1d",
        ],
        [
            "gpg",
            "--batch",
            "--yes",
            "--detach-sign",
            "--local-user",
            key_name,
            "-o",
            str(dists_dir / "Release.gpg"),
            str(dists_dir / "Release"),
        ],
        [
            "gpg",
            "--batch",
            "--yes",
            "--clearsign",
            "--local-user",
            key_name,
            "-o",
            str(dists_dir / "InRelease"),
            str(dists_dir / "Release"),
        ],
        ["bash", "-lc", "gpgconf --kill gpg-agent"],
    ]
