from __future__ import annotations

from shlex import quote
from pathlib import Path


def apt_repository_commands(repo_dir: Path, distribution: str, key_name: str = "Packagetest Ephemeral") -> list[list[str]]:
    pool_dir = quote(str(repo_dir / "pool"))
    packages_file = quote(str(repo_dir / "Packages"))
    release_file = quote(str(repo_dir / "Release"))
    repo_dir_q = quote(str(repo_dir))
    return [
        ["mkdir", "-p", str(repo_dir / "pool")],
        ["bash", "-lc", f"apt-ftparchive packages {pool_dir} > {packages_file}"],
        ["gzip", "-kf", str(repo_dir / "Packages")],
        [
            "bash",
            "-lc",
            (
                "apt-ftparchive "
                f"-o APT::FTPArchive::Release::Suite={distribution} "
                f"-o APT::FTPArchive::Release::Codename={distribution} "
                f"release {repo_dir_q} > {release_file}"
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
            "-o",
            str(repo_dir / "Release.gpg"),
            str(repo_dir / "Release"),
        ],
        [
            "gpg",
            "--batch",
            "--yes",
            "--clearsign",
            "-o",
            str(repo_dir / "InRelease"),
            str(repo_dir / "Release"),
        ],
        ["bash", "-lc", "gpgconf --kill gpg-agent"],
    ]
