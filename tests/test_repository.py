from pathlib import Path

from packagetest.repository import apt_repository_commands


def test_apt_repository_commands_use_standard_dists_layout(tmp_path: Path):
    commands = apt_repository_commands(tmp_path, "noble")
    command_texts = [" ".join(command) for command in commands]

    assert any("dists/noble/main/binary-amd64/Packages" in text for text in command_texts)
    assert any("dists/noble/Release" in text for text in command_texts)
    assert any("dists/noble/Release.gpg" in text for text in command_texts)
    assert any("dists/noble/InRelease" in text for text in command_texts)
