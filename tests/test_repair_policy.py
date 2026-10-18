"""Adversarial repair admissions and legitimate dependency/check preservation."""

import difflib

import pytest

from packagetest.failure_analysis import validate_source_patch
from packagetest.repair_policy import validate_file_edit


def control(dependency="python3-example (>= 2:1.4~rc1-1)", field="Build-Depends"):
    return f"Source: sample\n{field}: {dependency}\n\nPackage: sample\nArchitecture: all\nDepends: ${{python3:Depends}}, python3-example (>= 1)\n"


@pytest.mark.parametrize("dependency", [
    "", "python3-example", "python3-example (>= 1:9.0)",
    "python3-example (>= 2:1.4~rc1-1) | python3-unrelated",
    "python3-example (>= 2:1.4~rc1-1) [amd64]",
    "python3-example (>= 2:1.4~rc1-1) <!nocheck>",
    "python3-example:any (>= 2:1.4~rc1-1)",
])
def test_debian_dependency_weakening_is_rejected_for_all_target_scopes(dependency):
    with pytest.raises(ValueError, match="Dependency"):
        validate_file_edit("debian/control", control(), control(dependency))


@pytest.mark.parametrize("before, after", [
    ("python3-example (>= 2:1.4~rc1-1)", "python3-example (>= 2:1.4-1)"),
    ("python3-example (>= 2)", "python3-example (>> 2)"),
    ("python3-example (<< 3)", "python3-example (<= 2.9)"),
    ("python3-example (>= 2)", "python3-example (= 2)"),
    ("python3-example | python3-other", "python3-example"),
    ("python3-example (>= 2) [amd64] <!nocheck>", "python3-example (>= 3) [amd64] <!nocheck>"),
    ("python3-example", "python3-example, python3-added"),
])
def test_debian_additions_tighter_ranges_and_fewer_alternatives_remain_available(before, after):
    validate_file_edit("debian/control", control(before), control(after))


@pytest.mark.parametrize("before, after", [
    ("python3-example (= 2)", "python3-example (>= 2)"),
    ("python3-example (>> 2)", "python3-example (>= 2)"),
    ("python3-example (<< 3)", "python3-example (<= 3)"),
    ("python3-example (<= 3)", "python3-example (<= 4)"),
    ("python3-example (>= 2)", "python3-example (<= 3)"),
])
def test_pins_strictness_upper_bounds_and_inverted_ranges_are_not_relaxed(before, after):
    with pytest.raises(ValueError, match="Dependency"):
        validate_file_edit("debian/control", control(before), control(after))


def test_debian_dependency_cannot_move_from_build_to_indep_or_another_binary():
    with pytest.raises(ValueError, match="Dependency"):
        validate_file_edit("debian/control", control(), control(field="Build-Depends-Indep"))
    before = control() + "\nPackage: helper\nArchitecture: all\n"
    after = before.replace("Depends: ${python3:Depends}, python3-example (>= 1)", "Depends: ${python3:Depends}") + "Depends: python3-example (>= 1)\n"
    with pytest.raises(ValueError, match="Dependency"):
        validate_file_edit("debian/control", before, after)


def test_substitutions_and_negative_dependencies_cannot_disappear():
    before = control() + "Conflicts: obsolete-helper\n"
    for after in (before.replace("${python3:Depends}, ", ""), before.replace("Conflicts: obsolete-helper\n", "")):
        with pytest.raises(ValueError):
            validate_file_edit("debian/control", before, after)


def test_control_case_and_continuation_do_not_hide_a_constraint():
    before = control("python3-example (>= 2),\n python3-extra")
    after = before.replace("Build-Depends:", "build-depends:").replace(",\n python3-extra", ", python3-extra")
    validate_file_edit("debian/control", before, after)
    with pytest.raises(ValueError, match="Duplicate"):
        validate_file_edit("debian/control", before, after.replace("build-depends:", "Build-Depends: python3-unconstrained\nbuild-depends:"))


@pytest.mark.parametrize("before, after", [
    ("example>=2\n", "example\n"),
    ("example>=2\n", "example>=1\n"),
    ("example[secure]>=2\n", "example>=2\n"),
    ("example>=2; python_version >= '3.11'\n", "example>=2; python_version >= '3.12'\n"),
    ("example @ https://example.org/one.whl\n", "example @ https://example.org/two.whl\n"),
    ("-r base.txt\nexample>=2\n", "example>=2\n"),
    ("example>=2\n", "example>=2\n--no-deps\n"),
])
def test_python_requirements_preserve_versions_markers_extras_urls_and_includes(before, after):
    with pytest.raises(ValueError):
        validate_file_edit("test-requirements.txt", before, after)


def test_python_addition_and_additional_constraint_preserve_existing_requirement():
    validate_file_edit("requirements.txt", "Example>=2\n", "example>=2,<5\nother>=1\n")


@pytest.mark.parametrize("name, before, after", [
    ("setup.cfg", "[options]\ninstall_requires = example>=2\n", "[options]\ninstall_requires = example\n"),
    ("setup.cfg", "[options.extras_require]\ntest = example>=2\n", "[options.extras_require]\ntest = example\n"),
    ("pyproject.toml", '[project]\ndependencies=["example>=2"]\n', '[project]\ndependencies=[]\n'),
    ("pyproject.toml", '[project.optional-dependencies]\ntest=["example>=2"]\n', '[project.optional-dependencies]\ntest=[]\n'),
    ("pyproject.toml", '[build-system]\nrequires=["setuptools>=68"]\nbuild-backend="setuptools.build_meta"\n', '[build-system]\nrequires=[]\nbuild-backend="setuptools.build_meta"\n'),
])
def test_python_metadata_dependency_fields_are_validated(name, before, after):
    with pytest.raises(ValueError):
        validate_file_edit(name, before, after)


TEST = 'BASELINE = 2\n\ndef test_value():\n    observed = operation()\n    assert observed == BASELINE\n'


@pytest.mark.parametrize("after", [
    TEST.replace("assert observed == BASELINE", "assert True"),
    TEST.replace("assert observed == BASELINE", "assert observed"),
    TEST.replace("assert observed == BASELINE", "assert observed >= BASELINE"),
    TEST.replace("assert observed == BASELINE", "assert observed == observed"),
    TEST.replace("BASELINE = 2", "BASELINE = 1"),
    TEST.replace("observed = operation()", "observed = BASELINE"),
    TEST.replace("    observed = operation()", "    return\n    observed = operation()"),
    TEST.replace("def test_value():", "@pytest.mark.skip\ndef test_value():"),
    TEST.replace("def test_value():", "def test_value(disabled_fixture):"),
    TEST.replace("    assert observed == BASELINE", "    try:\n        assert observed == BASELINE\n    except AssertionError:\n        pass"),
])
def test_existing_test_semantics_cannot_be_changed_even_with_a_new_test(after):
    after += '\n\ndef test_regression():\n    assert operation() == BASELINE\n'
    with pytest.raises(ValueError):
        validate_file_edit("tests/test_value.py", TEST, after)


def test_unittest_assertion_method_and_arguments_cannot_be_weakened():
    before = "class Tests:\n    def test_value(self):\n        self.assertEqual(operation(), 2)\n"
    for method in ("self.assertTrue(True)", "self.assertEqual(operation(), operation())"):
        with pytest.raises(ValueError):
            validate_file_edit("tests/test_value.py", before, before.replace("self.assertEqual(operation(), 2)", method))


def test_existing_test_can_gain_assertions_and_an_independent_regression():
    after = TEST + "    assert observed is not None\n\ndef test_boundary():\n    assert operation(1) == 1\n"
    validate_file_edit("tests/test_value.py", TEST, after)


@pytest.mark.parametrize("body", ["assert True", "assert 1", "assert value == value", "self.assertEqual(1, 1)", "self.assertTrue(True)"])
def test_vacuous_regression_cannot_unlock_existing_test_changes(body):
    with pytest.raises(ValueError, match="nonvacuous"):
        validate_file_edit("tests/test_value.py", TEST, TEST + f"\ndef test_regression():\n    {body}\n")


@pytest.mark.parametrize("extra", ["pytest.skip('bad')", "pytest.importorskip('unavailable')", "pytest.xfail('bad')", "return", "try:\n        assert value\n    except Exception:\n        pass"])
def test_dynamic_test_bypasses_are_rejected(extra):
    with pytest.raises(ValueError):
        validate_file_edit("tests/test_value.py", TEST, TEST + f"\ndef test_regression():\n    {extra}\n    assert operation() == 2\n")


def test_new_collection_hooks_are_not_treated_as_normal_regressions():
    with pytest.raises(ValueError, match="hooks"):
        validate_file_edit("tests/conftest.py", "", "def pytest_collection_modifyitems(items):\n    items.clear()\n")


@pytest.mark.parametrize("name, before, after", [
    ("pytest.ini", "[pytest]\naddopts =\n", "[pytest]\naddopts = -k passing_only\n"),
    ("tox.ini", "[testenv]\ncommands = pytest\n", "[testenv]\ncommands = true\n"),
    ("setup.cfg", "[tool:pytest]\ntestpaths = tests\n", "[tool:pytest]\ntestpaths = passing\n"),
    ("pyproject.toml", '[tool.pytest.ini_options]\naddopts=""\n', '[tool.pytest.ini_options]\naddopts="-k passing"\n'),
    ("setup.cfg", "[coverage:run]\nomit =\n", "[coverage:run]\nomit = broken.py\n"),
    ("debian/tests/control", "Tests: smoke\nDepends: @, python3-example (>= 2)\n", "Tests: smoke\nDepends: @, python3-example\n"),
    ("debian/tests/control", "Tests: smoke\nRestrictions: needs-root\n", "Tests: smoke\nRestrictions: needs-root, superficial\n"),
    ("debian/tests/smoke", "#!/bin/sh\nset -eu\npytest tests\n", "#!/bin/sh\nset -eu\ntrue\n"),
])
def test_discovery_coverage_runner_and_autopkgtest_contracts_are_preserved(name, before, after):
    with pytest.raises(ValueError):
        validate_file_edit(name, before, after)


RULES = '#!/usr/bin/make -f\n%:\n\tdh $@ --with python3\n\noverride_dh_auto_test:\n\tpytest tests\n\noverride_dh_missing:\n\tdh_missing --fail-missing\n'


@pytest.mark.parametrize("after", [
    RULES.replace("\tpytest tests", "\tpytest tests || true"),
    RULES.replace("\tpytest tests", "\t-pytest tests"),
    RULES.replace("\tpytest tests", "\tif false; then\n\tpytest tests\n\tfi"),
    RULES.replace("--fail-missing", "--list-missing"),
    RULES.replace("\tpytest tests", "\ttrue"),
    ".IGNORE:\n" + RULES,
    "export DEB_BUILD_OPTIONS=nocheck\n" + RULES,
    "export PYTHONOPTIMIZE=1\n" + RULES,
    "export PYTEST_ADDOPTS=-k passing\n" + RULES,
    "ifeq (0,1)\n" + RULES + "endif\n",
])
def test_packaging_checks_cannot_be_suppressed_or_made_unreachable(after):
    with pytest.raises(ValueError):
        validate_file_edit("debian/rules", RULES, after)


def test_adding_empty_auto_test_override_cannot_disable_inherited_dh_tests():
    before = "#!/usr/bin/make -f\n%:\n\tdh $@\n"
    with pytest.raises(ValueError):
        validate_file_edit("debian/rules", before, before + "\noverride_dh_auto_test:\n")


def test_legitimate_metadata_architecture_and_unrelated_rule_option_changes_are_available():
    validate_file_edit("debian/control", control(), control().replace("Architecture: all", "Architecture: any"))
    before = RULES + "\noverride_dh_install:\n\toslopolicy-sample-generator --format yaml\n"
    validate_file_edit("debian/rules", before, before.replace(" --format yaml", ""))
    validate_file_edit("setup.cfg", "[entry_points]\nconsole_scripts = sample=old:main\n", "[entry_points]\nconsole_scripts = sample=new:main\n")


def file_patch(name, before, after):
    mode = "new file mode 100644\n" if not before else ""
    return f"diff --git a/{name} b/{name}\n{mode}" + "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile="a/" + name if before else "/dev/null", tofile="b/" + name))


@pytest.mark.parametrize("apply", [False, True])
def test_raw_patch_gate_rejects_dependency_weakening_without_mutating_original(tmp_path, apply):
    (tmp_path / "debian").mkdir()
    baseline = control()
    (tmp_path / "debian/control").write_text(baseline)
    patch = file_patch("debian/control", baseline, control("python3-example"))
    result = validate_source_patch(patch, tmp_path, apply=apply)
    assert result["result"] == "REJECTED"
    assert "Dependency" in result["error"]
    assert (tmp_path / "debian/control").read_text() == baseline


def test_raw_new_quilt_cannot_hide_weakened_assertions(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "test_value.py").write_text(TEST)
    inner = file_patch("test_value.py", TEST, TEST.replace("assert observed == BASELINE", "assert True"))
    outer = file_patch("debian/patches/repair.patch", "", inner) + file_patch("debian/patches/series", "", "repair.patch\n")
    result = validate_source_patch(outer, tmp_path, apply=True)
    assert result["result"] == "REJECTED"
    assert "test" in result["error"].lower()
    assert (tmp_path / "test_value.py").read_text() == TEST
    assert not (tmp_path / "debian/patches/repair.patch").exists()


def test_raw_quilt_cannot_remove_requirement_version(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "requirements.txt").write_text("example>=2\n")
    inner = file_patch("requirements.txt", "example>=2\n", "example\n")
    outer = file_patch("debian/patches/repair.patch", "", inner) + file_patch("debian/patches/series", "", "repair.patch\n")
    result = validate_source_patch(outer, tmp_path)
    assert result["result"] == "REJECTED"
    assert "dependency relaxation" in result["error"]


@pytest.mark.parametrize("setting", ["DISABLE_TESTS = 1", "include generated.mk", ".ONESHELL:", ".PHONY: different-target"])
def test_arbitrary_global_make_settings_cannot_change_check_execution(setting):
    with pytest.raises(ValueError, match="settings"):
        validate_file_edit("debian/rules", RULES, setting + "\n" + RULES)


def test_cumulative_new_quilt_patches_are_validated_against_their_prefix(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "requirements.txt").write_text("example>=2\n")
    first = file_patch("requirements.txt", "example>=2\n", "example>=2,<5\n")
    second = file_patch("requirements.txt", "example>=2,<5\n", "example>=2,<5\nother>=1\n")
    outer = (file_patch("debian/patches/first.patch", "", first)
             + file_patch("debian/patches/second.patch", "", second)
             + file_patch("debian/patches/series", "", "first.patch\nsecond.patch\n"))
    assert validate_source_patch(outer, tmp_path)["result"] == "APPLIES"


def test_cumulative_quilt_cannot_relax_a_requirement_added_by_its_prefix(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "requirements.txt").write_text("example>=2\n")
    first = file_patch("requirements.txt", "example>=2\n", "example>=2,<5\n")
    second = file_patch("requirements.txt", "example>=2,<5\n", "example>=2\n")
    outer = (file_patch("debian/patches/first.patch", "", first)
             + file_patch("debian/patches/second.patch", "", second)
             + file_patch("debian/patches/series", "", "first.patch\nsecond.patch\n"))
    assert validate_source_patch(outer, tmp_path)["result"] == "REJECTED"


def test_invalid_config_fails_as_a_validation_result(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts=\n")
    inner = file_patch("pytest.ini", "[pytest]\naddopts=\n", "[pytest]\naddopts=\naddopts=-k passing\n")
    outer = file_patch("debian/patches/repair.patch", "", inner) + file_patch("debian/patches/series", "", "repair.patch\n")
    result = validate_source_patch(outer, tmp_path)
    assert result["result"] == "REJECTED"
    assert "malformed configuration" in result["error"]


@pytest.mark.parametrize("body", ["assert 1 < 2", "assert operation() or True"])
def test_literal_comparisons_and_boolean_escape_hatches_are_not_regressions(body):
    with pytest.raises(ValueError, match="nonvacuous"):
        validate_file_edit("tests/test_value.py", "", f"def test_value():\n    {body}\n")


def test_duplicate_definition_cannot_replace_a_preserved_test():
    after = TEST + "\ndef test_value():\n    assert operation() >= 0\n"
    with pytest.raises(ValueError, match="shadow"):
        validate_file_edit("tests/test_value.py", TEST, after)


def test_added_test_cannot_shadow_an_imported_fixture_or_test():
    before = "from framework import test_shared\n" + TEST
    after = before + "\ndef test_shared():\n    assert operation() >= 0\n"
    with pytest.raises(ValueError, match="shadow"):
        validate_file_edit("tests/test_value.py", before, after)


@pytest.mark.parametrize("setting", ["__test__ = False", "collect_ignore = ['test_value.py']", "pytestmark = disable_marker", "pytest_plugins = ['skip_everything']"])
def test_new_test_modules_cannot_disable_collection(setting):
    with pytest.raises(ValueError, match="skipping"):
        validate_file_edit("tests/test_value.py", "", setting + "\ndef test_value():\n    assert operation() == 2\n")


@pytest.mark.parametrize("body", ["def unused():\n        assert operation() == 2", "if False:\n        assert operation() == 2"])
def test_uncalled_or_unreachable_assertion_is_not_regression_coverage(body):
    with pytest.raises(ValueError, match="nonvacuous"):
        validate_file_edit("tests/test_value.py", "", f"def test_value():\n    {body}\n")


def test_production_file_named_latest_is_not_mistaken_for_a_test():
    validate_file_edit("package/latest.py", "def latest():\n    return 1\n", "def latest():\n    return 2\n")


def test_requirement_fragments_in_named_directories_are_protected():
    for name in ("requirements/runtime.txt", "constraints/lower.in"):
        with pytest.raises(ValueError, match="relaxation"):
            validate_file_edit(name, "example>=2\n", "example\n")


@pytest.mark.parametrize("name, before, after", [
    ("debian/rules", RULES, RULES.replace("--fail-missing", "--list-missing")),
    ("debian/tests/control", "Tests: smoke\n", "Tests: smoke\nRestrictions: superficial\n"),
])
def test_raw_packaging_check_rejection_preserves_the_original(tmp_path, name, before, after):
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text(before)
    result = validate_source_patch(file_patch(name, before, after), tmp_path, apply=True)
    assert result["result"] == "REJECTED"
    assert path.read_text() == before


@pytest.mark.parametrize("name", ["debian/source/options", "debian/source/format", "debian/postinst"])
def test_raw_patches_cannot_change_protected_source_options_or_maintainer_hooks(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    before = "original\n"
    path.write_text(before)
    result = validate_source_patch(file_patch(name, before, "replacement\n"), tmp_path, apply=True)
    assert result["result"] == "REJECTED"
    assert path.read_text() == before


@pytest.mark.parametrize("line", [
    "sample: statically-linked-binary [usr/bin/*]",
    "sample: arch-independent-package-contains-binary-or-object [usr/bin/helper]",
    "sample: statically-linked-binary [usr/../bin/helper]",
])
def test_raw_patches_cannot_bypass_structural_lintian_override_policy(tmp_path, line):
    (tmp_path / "debian").mkdir()
    patch = file_patch("debian/sample.lintian-overrides", "", "# Explanation\n" + line + "\n")
    assert validate_source_patch(patch, tmp_path)["result"] == "REJECTED"


def test_ownership_metadata_cannot_be_deleted_instead_of_replaced():
    before = control().replace("Source: sample", "Source: sample\nMaintainer: Example <example@example.org>")
    with pytest.raises(ValueError, match="protected control field"):
        validate_file_edit("debian/control", before, before.replace("Maintainer: Example <example@example.org>\n", ""))


def test_new_module_cannot_replace_a_regression_with_a_later_assignment():
    with pytest.raises(ValueError, match="Duplicate"):
        validate_file_edit("tests/test_value.py", "", "def test_value():\n    assert operation() == 2\n\ntest_value = lambda: None\n")


def test_control_comment_only_paragraphs_do_not_create_dependency_stanzas():
    before = "# First header\n\n# Another header\n\n" + control()
    validate_file_edit("debian/control", before, before.replace("Architecture: all", "Architecture: any"))


def test_existing_quilt_cannot_retarget_the_same_transformation_into_a_test(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    old = file_patch("worker.py", "return value\n", "return None\n")
    new = old.replace("worker.py", "tests/test_value.py")
    (tmp_path / "debian/patches/existing.patch").write_text(old)
    result = validate_source_patch(file_patch("debian/patches/existing.patch", old, new), tmp_path)
    assert result["result"] == "REJECTED"
    assert "requires review" in result["error"]


def test_quilt_series_cannot_drop_dependency_patch(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/patches/series").write_text("requirements.patch\n")
    (tmp_path / "debian/patches/requirements.patch").write_text(file_patch("requirements.txt", "example\n", "example>=2\n"))
    result = validate_source_patch(file_patch("debian/patches/series", "requirements.patch\n", "# no longer needed\n"), tmp_path)
    assert result["result"] == "REJECTED"
    assert "Dropping" in result["error"]


def test_quilt_series_cannot_reorder_the_existing_stack(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/patches/series").write_text("first.patch\nsecond.patch\n")
    for name in ("first.patch", "second.patch"):
        (tmp_path / "debian/patches" / name).write_text(file_patch("worker.py", "old\n", "new\n"))
    result = validate_source_patch(file_patch("debian/patches/series", "first.patch\nsecond.patch\n", "second.patch\nfirst.patch\n"), tmp_path)
    assert result["result"] == "REJECTED"
    assert "reordering" in result["error"]


def test_invalid_debian_version_is_not_treated_as_an_equal_bound():
    with pytest.raises(ValueError, match="compare Debian versions"):
        validate_file_edit("debian/control", control("example (>= 2)"), control("example (>= 2:)"))
