from __future__ import annotations

from pathlib import Path


def apt_repository_commands(repo_dir: Path, distribution: str, key_name: str = "Packagetest Ephemeral") -> list[list[str]]:
    return [
        ["mkdir", "-p", str(repo_dir / "pool")],
        ["bash", "-lc", f"apt-ftparchive packages {repo_dir / 'pool'} > {repo_dir / 'Packages'}"],
        ["gzip", "-kf", str(repo_dir / "Packages")],
        [
            "bash",
            "-lc",
            (
                "apt-ftparchive "
                f"-o APT::FTPArchive::Release::Suite={distribution} "
                f"-o APT::FTPArchive::Release::Codename={distribution} "
                f"release {repo_dir} > {repo_dir / 'Release'}"
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
