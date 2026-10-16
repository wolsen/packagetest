from pathlib import Path

import pytest

from packagetest.artifacts import fields


def test_deb822_comments_are_ignored_without_ending_a_continuation(tmp_path: Path):
    control = tmp_path / "control"
    control.write_text(
        "Source: python-ldappool\n"
        "Build-Depends: debhelper,\n"
        "               python-all,\n"
        "#               python3-all,\n"
        "               python-setuptools\n"
        "Standards-Version: 3.9.5\n\n"
        "Package: python-ldappool\n",
        encoding="utf-8",
    )

    assert fields(control) == {
        "Source": "python-ldappool",
        "Build-Depends": "debhelper,\npython-all,\npython-setuptools",
        "Standards-Version": "3.9.5",
    }


def test_deb822_rejects_non_comment_lines_without_a_field(tmp_path: Path):
    control = tmp_path / "control"
    control.write_text("Source: example\ninvalid metadata\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid Debian metadata"):
        fields(control)
