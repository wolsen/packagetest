import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from packagetest.failure_analysis import (
    QuiltPatchSuperseded,
    normalize_repair_decision,
    parse_model_response,
    parse_repair_decision,
    render_source_repair,
    repository_context,
    safe_evidence_text,
    select_direct_failures,
    validate_patch,
    validate_repair_decision,
    validate_source_patch,
)
from packagetest.remediation_evidence import extract_test_failure_evidence


def test_selects_direct_failures_and_omits_blocked_dependents():
    rows = [
        {"source": "build-bad", "build": {"result": "FAILED"}, "autopkgtest": {"result": "BLOCKED"}},
        {"source": "test-bad", "build": {"result": "SUCCEEDED"}, "autopkgtest": {"result": "FAIL"}},
        {"source": "blocked", "build": {"result": "BLOCKED"}, "autopkgtest": {"result": "BLOCKED"}},
        {"source": "good", "build": {"result": "SUCCEEDED"}, "autopkgtest": {"result": "PASS"}},
    ]
    assert select_direct_failures(rows) == [
        {"source": "build-bad", "phase": "build", "result": "FAILED"},
        {"source": "test-bad", "phase": "autopkgtest", "result": "FAIL"},
    ]


def test_response_parser_accepts_plain_or_fenced_patch():
    response = """BEGIN_DIAGNOSIS\nmissing dependency\nEND_DIAGNOSIS
BEGIN_PATCH
```diff
diff --git a/config/a b/config/a
--- a/config/a
+++ b/config/a
@@ -1 +1 @@
-old
+new
```
END_PATCH
"""
    parsed = parse_model_response(response)
    assert parsed.diagnosis == "missing dependency"
    assert parsed.patch.startswith("diff --git")
    assert parsed.patch.endswith("\n")


def test_response_parser_recovers_last_unwrapped_llama_diff():
    response = """llama.cpp banner
> echoed prompt containing diff --git a/bad b/bad
```diff
diff --git a/debian/control b/debian/control
--- a/debian/control
+++ b/debian/control
@@ -1 +1,2 @@
 Source: sample
+Build-Depends: python3-futurist
```

Exiting...
"""
    parsed = parse_model_response(response)
    assert parsed.patch.startswith("diff --git a/debian/control")
    assert "python3-futurist" in parsed.patch


def test_parses_noisy_structured_repair_decision():
    output = 'banner\n> prompt\n{"action":"add_dependency","subject":"python3-futurist","replacement":"","evidence":"missing futurist"}\nExiting...\n'
    assert parse_repair_decision(output)["subject"] == "python3-futurist"


def test_repair_decision_must_match_failure_evidence():
    missing = "ModuleNotFoundError: No module named 'ncclient'"
    good_dependency = {"action": "add_dependency", "subject": "python3-ncclient",
                       "replacement": "", "evidence": missing}
    bad_dependency = {**good_dependency, "subject": "python3-oslo.config"}
    assert validate_repair_decision(good_dependency, missing)["result"] == "ACCEPTED"
    assert validate_repair_decision(bad_dependency, missing)["result"] == "REJECTED"
    rejected = r"tool: error: unrecognized arguments: --format yaml\nmake: failed"
    good_argument = {"action": "remove_rule_argument", "subject": "--format yaml",
                     "replacement": "", "evidence": rejected}
    assert validate_repair_decision(good_argument, rejected)["result"] == "ACCEPTED"
    assert validate_repair_decision({**good_argument, "subject": "--namespace"}, rejected)["result"] == "REJECTED"


def test_renders_dependency_and_rule_argument_repairs(tmp_path):
    (tmp_path / "debian").mkdir()
    control = """Source: sample
Build-Depends: debhelper-compat (= 13),
Build-Depends-Indep:
 python3-pbr,

Package: python3-sample
Architecture: all
Depends:
 ${python3:Depends},
"""
    (tmp_path / "debian/control").write_text(control)
    (tmp_path / "debian/rules").write_text("cmd \\\n\t--output file \\\n\t--format yaml \\\n\t--namespace sample\n")
    dependency = render_source_repair({
        "action": "add_dependency", "subject": "python3-futurist", "replacement": "", "evidence": "missing"
    }, tmp_path)
    assert dependency.count("+ python3-futurist,") == 2
    assert dependency.index("+ python3-futurist,") < dependency.index(" ${python3:Depends},")
    assert validate_source_patch(dependency, tmp_path)["result"] == "APPLIES"
    argument = render_source_repair({
        "action": "remove_rule_argument", "subject": "--format yaml", "replacement": "", "evidence": "rejected"
    }, tmp_path)
    assert "--- a/debian/rules" in argument
    assert "\n-\t--format yaml" in argument
    assert validate_source_patch(argument, tmp_path)["result"] == "APPLIES"


def test_build_only_dependency_repair_does_not_add_runtime_dependency(tmp_path):
    (tmp_path / "debian").mkdir()
    (tmp_path / "debian/control").write_text("""Source: demo
Build-Depends-Indep:
 python3-stestr,

Package: python3-demo
Depends:
 ${python3:Depends},
Description: demo
""")
    patch = render_source_repair({
        "action": "add_dependency", "subject": "python3-gabbi", "replacement": "",
        "evidence": "ModuleNotFoundError: No module named 'gabbi'", "scope": "build",
    }, tmp_path)
    assert patch.count("+ python3-gabbi,") == 1


def test_renders_failed_quilt_patch_as_reviewed_drop_candidate(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/patches/series").write_text("embedded-xstatic.patch\nkeep.patch -p1\n")
    evidence = ("embedded-xstatic.patch subprocess returned exit status 1; "
                "Hunk #1 FAILED at 8")
    decision = {
        "action": "drop_quilt_patch", "subject": "embedded-xstatic.patch",
        "replacement": "", "evidence": evidence,
    }
    assert validate_repair_decision(decision, evidence)["result"] == "ACCEPTED"
    patch = render_source_repair(decision, tmp_path)
    assert "+# Superseded upstream after snapshot rebase: embedded-xstatic.patch" in patch
    assert validate_source_patch(patch, tmp_path)["result"] == "APPLIES"


def test_mechanically_refreshes_quilt_patch_with_changed_context(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/patches/series").write_text("feature.patch\n")
    (tmp_path / "debian/patches/feature.patch").write_text("""Description: retain feature
diff --git a/sample.txt b/sample.txt
--- a/sample.txt
+++ b/sample.txt
@@ -1,4 +1,4 @@
 heading
 keep
-old
+new
 tail
""")
    (tmp_path / "sample.txt").write_text("changed heading\nkeep\nold\ntail\n")
    evidence = "feature.patch subprocess returned exit status 1; Hunk #1 FAILED"
    decision = {
        "action": "refresh_quilt_patch", "subject": "feature.patch",
        "replacement": "", "evidence": evidence,
    }
    assert validate_repair_decision(decision, evidence)["result"] == "ACCEPTED"
    patch = render_source_repair(decision, tmp_path)
    assert "diff --git a/debian/patches/feature.patch" in patch
    assert " changed heading" in patch
    assert validate_source_patch(patch, tmp_path)["result"] == "APPLIES"


def test_mechanically_refreshes_traditional_quilt_patch(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/patches/series").write_text("entry-point.patch\n")
    (tmp_path / "debian/patches/entry-point.patch").write_text("""Description: expose config options
Author: Ubuntu OpenStack Team
--- a/setup.cfg
+++ b/setup.cfg
@@ -1,4 +1,5 @@
 [entry_points]
 console_scripts =
     heat-tests = heat_tempest_plugin.cmd:main
+oslo.config.opts = heat_tempest_plugin.config:list_opts
 tail = value
""")
    (tmp_path / "setup.cfg").write_text("""[entry_points]
# Snapshot source gained this comment.
console_scripts =
    heat-tests = heat_tempest_plugin.cmd:main
tail = value
""")
    decision = {
        "action": "refresh_quilt_patch", "subject": "entry-point.patch",
        "replacement": "", "evidence": "entry-point.patch Hunk #1 FAILED",
    }
    patch = render_source_repair(decision, tmp_path)
    assert "automatic quilt refresh requires git-style" not in patch
    assert " Snapshot source gained this comment." in patch
    assert validate_source_patch(patch, tmp_path, apply=True)["result"] == "APPLIED"
    refreshed = (tmp_path / "debian/patches/entry-point.patch").read_text()
    assert refreshed.startswith("Description: expose config options\nAuthor: Ubuntu OpenStack Team\n")
    assert "+++ b/setup.cfg" in refreshed
    assert "+oslo.config.opts = heat_tempest_plugin.config:list_opts" in refreshed


def test_quilt_refresh_keeps_needed_section_and_omits_upstream_section(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/patches/series").write_text("mixed.patch\n")
    (tmp_path / "debian/patches/mixed.patch").write_text("""Description: mixed upstream status
diff --git a/needed.txt b/needed.txt
--- a/needed.txt
+++ b/needed.txt
@@ -1 +1 @@
-old
+new
diff --git a/upstream.txt b/upstream.txt
--- a/upstream.txt
+++ b/upstream.txt
@@ -1 +1 @@
-removed upstream
+replacement upstream
""")
    (tmp_path / "needed.txt").write_text("old\n")
    (tmp_path / "upstream.txt").write_text("replacement upstream\n")
    decision = {
        "action": "refresh_quilt_patch", "subject": "mixed.patch",
        "replacement": "", "evidence": "mixed.patch Hunk #1 FAILED",
    }
    patch = render_source_repair(decision, tmp_path)
    assert validate_source_patch(patch, tmp_path, apply=True)["result"] == "APPLIED"
    refreshed = (tmp_path / "debian/patches/mixed.patch").read_text()
    assert "diff --git a/needed.txt b/needed.txt" in refreshed
    assert "diff --git a/upstream.txt b/upstream.txt" not in refreshed


def test_quilt_refresh_preserves_debian_created_upstream_file(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/patches/series").write_text("debian-file.patch\n")
    (tmp_path / "debian/patches/debian-file.patch").write_text("""Description: add Debian settings
--- a/manifests/init.pp
+++ b/manifests/init.pp
@@ -1,2 +1,3 @@
 class horizon {
+  include debian::settings
 }
--- /dev/null
+++ b/templates/debian_settings.py.erb
@@ -0,0 +1,2 @@
+# Managed by Puppet
+DEBUG = false
""")
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests/init.pp").write_text(
        "class horizon {\n  # Upstream comment\n}\n")
    decision = {
        "action": "refresh_quilt_patch", "subject": "debian-file.patch",
        "replacement": "", "evidence": "debian-file.patch Hunk #1 FAILED",
    }
    patch = render_source_repair(decision, tmp_path)
    assert validate_source_patch(patch, tmp_path, apply=True)["result"] == "APPLIED"
    refreshed = (tmp_path / "debian/patches/debian-file.patch").read_text()
    assert "--- /dev/null" in refreshed
    assert "+++ b/templates/debian_settings.py.erb" in refreshed


def test_quilt_refresh_classifies_removed_target_as_superseded(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/patches/series").write_text("retired-service.patch\n")
    (tmp_path / "debian/patches/retired-service.patch").write_text("""Description: disable retired service policy
--- a/manifests/retired.pp
+++ b/manifests/retired.pp
@@ -1 +1 @@
-include retired::policy
+# include retired::policy
""")
    decision = {
        "action": "refresh_quilt_patch", "subject": "retired-service.patch",
        "replacement": "", "evidence": "retired-service.patch does not apply",
    }
    with pytest.raises(QuiltPatchSuperseded, match="target was removed"):
        render_source_repair(decision, tmp_path)


def test_quilt_refresh_does_not_drop_partially_drifted_deletion(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/patches/series").write_text("broken-test.patch\n")
    (tmp_path / "debian/patches/broken-test.patch").write_text("""Description: remove broken test
--- a/tests/test_service.py
+++ b/tests/test_service.py
@@ -1,3 +1 @@
-def test_broken():
-    old_helper()
-    assert result
+# other tests remain
""")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_service.py").write_text(
        "def test_broken():\n    new_helper()\n    assert result\n")
    decision = {
        "action": "refresh_quilt_patch", "subject": "broken-test.patch",
        "replacement": "", "evidence": "broken-test.patch Hunk #1 FAILED",
    }
    with pytest.raises(ValueError, match="cannot be refreshed"):
        render_source_repair(decision, tmp_path)


def test_quilt_refresh_omits_drifted_test_section_after_runtime_is_upstream(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/patches/series").write_text("security-fix.patch\n")
    (tmp_path / "debian/patches/security-fix.patch").write_text("""Description: security fix and tests
--- a/service/policy.py
+++ b/service/policy.py
@@ -1 +1 @@
-ALLOW_ALL = True
+ALLOW_ALL = False
--- a/service/tests/base.py
+++ b/service/tests/base.py
@@ -1,2 +1,2 @@
-class OldFixture:
-    pass
+class SecureFixture:
+    enabled = True
""")
    (tmp_path / "service/tests").mkdir(parents=True)
    (tmp_path / "service/policy.py").write_text("ALLOW_ALL = False\n")
    (tmp_path / "service/tests/base.py").write_text(
        "class OldFixture:\n    secure_by_default = True\n")
    decision = {
        "action": "refresh_quilt_patch", "subject": "security-fix.patch",
        "replacement": "", "evidence": "security-fix.patch Hunk #1 FAILED",
    }
    with pytest.raises(QuiltPatchSuperseded, match="runtime sections were incorporated"):
        render_source_repair(decision, tmp_path)


def test_quilt_refresh_removes_drifted_python_test_function(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/patches/series").write_text("broken-test.patch\n")
    (tmp_path / "debian/patches/broken-test.patch").write_text("""Description: remove broken test
--- a/tests/test_trace.py
+++ b/tests/test_trace.py
@@ -1,7 +1 @@
-    @mock.patch('service.helper')
-    def test_trace(self):
-        payload = old_wrapper(stream.read())
-        result = trace(payload)
-        self.assertEqual(0, result)
-
     def test_retained(self):
""")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_trace.py").write_text("""class TestTrace:
    @mock.patch('service.helper')
    def test_trace(self):
        payload = bytes(stream.read())
        result = trace(payload)
        self.assertEqual(0, result)

    def test_retained(self):
        pass
""")
    decision = {
        "action": "refresh_quilt_patch", "subject": "broken-test.patch",
        "replacement": "", "evidence": "broken-test.patch Hunk #1 FAILED",
    }
    patch = render_source_repair(decision, tmp_path)
    assert validate_source_patch(patch, tmp_path, apply=True)["result"] == "APPLIED"
    refreshed = (tmp_path / "debian/patches/broken-test.patch").read_text()
    assert "def test_trace" in refreshed
    assert "-        payload = bytes(stream.read())" in refreshed
    assert " def test_retained" in refreshed


def test_quilt_refresh_applies_independent_groups_in_drifted_function(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/patches/series").write_text("assets.patch\n")
    (tmp_path / "debian/patches/assets.patch").write_text("""Description: use packaged assets
--- a/module.py
+++ b/module.py
@@ -1,14 +1,5 @@
 def copy_assets(app):
-    fonts = (
-        'old.ttf'
-    )
-    dirtree = app.output / 'fonts'
-    dirtree.mkdir()
     for asset in assets:
-        source = package_dir / asset
-        copyfile(source, app.output / asset)
-    for font in fonts:
-        copyfile(package_dir / font, app.output / font)
+        symlink(shared_dir / asset, app.output / asset)
""")
    (tmp_path / "module.py").write_text("""def copy_assets(app: Builder) -> None:
    fonts = (
        'old.ttf',
    )
    dirtree = app.output / 'fonts'
    dirtree.mkdir()
    for asset in assets:
        source = package_dir / asset
        copyfile(source, app.output / asset)
    for font in fonts:
        copyfile(package_dir / font, app.output / font)
""")
    decision = {
        "action": "refresh_quilt_patch", "subject": "assets.patch",
        "replacement": "", "evidence": "assets.patch Hunk #1 FAILED",
    }
    patch = render_source_repair(decision, tmp_path)
    assert validate_source_patch(patch, tmp_path, apply=True)["result"] == "APPLIED"
    refreshed = (tmp_path / "debian/patches/assets.patch").read_text()
    assert "symlink(shared_dir / asset" in refreshed
    assert "-    fonts = (" in refreshed
    assert "-        'old.ttf'," in refreshed
    assert "def copy_assets(app: Builder)" in refreshed


def test_quilt_refresh_classifies_substantially_upstream_section(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/patches/series").write_text("security.patch\n")
    additions = "".join(f"+secure_{number} = True\n" for number in range(1, 11))
    (tmp_path / "debian/patches/security.patch").write_text(
        "Description: secure defaults\nOrigin: upstream, https://example.test/change\n"
        "--- a/service.py\n+++ b/service.py\n"
        "@@ -1,2 +1,11 @@\n-old = True\n+old = False\n" + additions)
    (tmp_path / "service.py").write_text(
        "old = False\n" + "".join(f"secure_{number} = True\n" for number in range(1, 10))
        + "secure_10 = policy.default_secure\n")
    decision = {
        "action": "refresh_quilt_patch", "subject": "security.patch",
        "replacement": "", "evidence": "security.patch Hunk #1 FAILED",
    }
    with pytest.raises(QuiltPatchSuperseded, match="most patch additions"):
        render_source_repair(decision, tmp_path)


def test_replaces_missing_upstream_path_only_when_candidate_exists(tmp_path):
    (tmp_path / "debian").mkdir()
    (tmp_path / "debian/rules").write_text(
        "oslo-config-generator --config-file=aodh/cmd/aodh-config-generator.conf\n")
    (tmp_path / "etc/aodh").mkdir(parents=True)
    (tmp_path / "etc/aodh/aodh-config-generator.conf").write_text("[DEFAULT]\n")
    evidence = ("ConfigFilesNotFoundError: Failed to find some config files: "
                "aodh/cmd/aodh-config-generator.conf")
    decision = {
        "action": "replace_packaging_path", "subject": "aodh/cmd/aodh-config-generator.conf",
        "replacement": "etc/aodh/aodh-config-generator.conf", "evidence": evidence,
    }
    assert validate_repair_decision(decision, evidence)["result"] == "ACCEPTED"
    patch = render_source_repair(decision, tmp_path)
    assert "+oslo-config-generator --config-file=etc/aodh/aodh-config-generator.conf" in patch
    assert validate_source_patch(patch, tmp_path)["result"] == "APPLIES"


def test_normalizes_reversed_missing_and_existing_paths(tmp_path):
    (tmp_path / "debian").mkdir()
    (tmp_path / "debian/rules").write_text(
        "oslo-config-generator --config-file=aodh/cmd/aodh-config-generator.conf\n")
    (tmp_path / "etc/aodh").mkdir(parents=True)
    (tmp_path / "etc/aodh/aodh-config-generator.conf").write_text("[DEFAULT]\n")
    reversed_decision = {
        "action": "replace_packaging_path",
        "subject": "etc/aodh/aodh-config-generator.conf",
        "replacement": "aodh/cmd/aodh-config-generator.conf",
        "evidence": "aodh/cmd/aodh-config-generator.conf was not found",
    }
    assert normalize_repair_decision(reversed_decision, tmp_path) == {
        **reversed_decision,
        "subject": "aodh/cmd/aodh-config-generator.conf",
        "replacement": "etc/aodh/aodh-config-generator.conf",
    }


def test_path_normalization_preserves_canonical_or_ambiguous_decisions(tmp_path):
    (tmp_path / "debian").mkdir()
    (tmp_path / "debian/rules").write_text("install old/path\n")
    (tmp_path / "new").mkdir()
    (tmp_path / "new/path").write_text("new\n")
    canonical = {
        "action": "replace_packaging_path", "subject": "old/path",
        "replacement": "new/path", "evidence": "old/path missing",
    }
    assert normalize_repair_decision(canonical, tmp_path) == canonical
    ambiguous = {**canonical, "subject": "unrelated", "replacement": "missing"}
    assert normalize_repair_decision(ambiguous, tmp_path) == ambiguous


def test_normalizes_dependency_subject_from_models_quoted_missing_import(tmp_path):
    evidence = """ModuleNotFoundError: No module named 'werkzeug'
ModuleNotFoundError: No module named 'gabbi'
"""
    confused = {
        "action": "add_dependency", "subject": "python3-wsgi-intercept", "replacement": "",
        "evidence": "ModuleNotFoundError: No module named 'werkzeug'",
    }
    assert normalize_repair_decision(confused, tmp_path, evidence) == {
        **confused, "subject": "python3-werkzeug",
    }


def test_dependency_normalization_requires_one_supported_quoted_import(tmp_path):
    decision = {
        "action": "add_dependency", "subject": "python3-existing", "replacement": "",
        "evidence": "ModuleNotFoundError: No module named 'unknown'",
    }
    assert normalize_repair_decision(
        decision, tmp_path, "ModuleNotFoundError: No module named 'gabbi'") == decision
    ambiguous = {
        **decision,
        "evidence": ("ModuleNotFoundError: No module named 'werkzeug'\n"
                     "ModuleNotFoundError: No module named 'gabbi'"),
    }
    assert normalize_repair_decision(ambiguous, tmp_path, ambiguous["evidence"]) == ambiguous


def test_test_evidence_preserves_import_discovery_runtime_error():
    log = """Failures during discovery
Failed to import test module: nova.tests.unit.cmd.test_compute
Traceback (most recent call last):
  File \"nova/cmd/compute.py\", line 19, in <module>
    monkey_patch.patch(backend='threading')
RuntimeError: eventlet library imported early preventing native threading
Ran 0 tests in 6.456s
"""
    evidence = extract_test_failure_evidence(log)
    assert "Failed to import test module: nova.tests.unit.cmd.test_compute" in evidence
    assert "RuntimeError: eventlet library imported early" in evidence


def test_patch_validation_uses_disposable_copy_and_restricts_paths(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/value").write_text("old\n")
    patch = """diff --git a/config/value b/config/value
--- a/config/value
+++ b/config/value
@@ -1 +1 @@
-old
+new
"""
    result = validate_patch(patch, tmp_path)
    assert result["result"] == "APPLIES"
    assert (tmp_path / "config/value").read_text() == "old\n"
    forbidden = patch.replace("config/value", ".github/workflows/pwn.yml")
    assert validate_patch(forbidden, tmp_path)["result"] == "REJECTED"


def test_source_patch_is_limited_to_debian_and_can_be_applied(tmp_path):
    (tmp_path / "debian").mkdir()
    (tmp_path / "debian/control").write_text("old\n")
    patch = """diff --git a/debian/control b/debian/control
--- a/debian/control
+++ b/debian/control
@@ -1 +1 @@
-old
+new
"""
    assert validate_source_patch(patch, tmp_path)["result"] == "APPLIES"
    assert validate_source_patch(patch, tmp_path, apply=True)["result"] == "APPLIED"
    assert (tmp_path / "debian/control").read_text() == "new\n"


def test_source_patch_applies_when_tree_is_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tree = Path("outputs/source-preparation/sample")
    (tree / "debian").mkdir(parents=True)
    (tree / "debian/control").write_text("old\n")
    patch = """diff --git a/debian/control b/debian/control
--- a/debian/control
+++ b/debian/control
@@ -1 +1 @@
-old
+new
"""
    assert validate_source_patch(patch, tree)["result"] == "APPLIES"


def test_source_patch_rejects_metadata_staging_and_missing_series_file(tmp_path):
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/control").write_text("Maintainer: Ubuntu Developers <ubuntu@example.com>\n")
    metadata = """diff --git a/debian/control b/debian/control
--- a/debian/control
+++ b/debian/control
@@ -1 +1 @@
-Maintainer: Ubuntu Developers <ubuntu@example.com>
+Maintainer: Your Name <your.email@example.com>
"""
    assert validate_source_patch(metadata, tmp_path)["result"] == "REJECTED"
    missing = """diff --git a/debian/patches/series b/debian/patches/series
--- a/debian/patches/series
+++ b/debian/patches/series
@@ -0,0 +1 @@
+missing.patch
"""
    result = validate_source_patch(missing, tmp_path)
    assert result["result"] == "REJECTED"
    assert "do not exist" in result["error"]


def test_source_patch_rejects_upstream_files_changelog_and_test_bypass(tmp_path):
    (tmp_path / "debian/tests").mkdir(parents=True)
    (tmp_path / "debian/tests/control").write_text("Tests: smoke\n")
    upstream = """diff --git a/setup.cfg b/setup.cfg
--- a/setup.cfg
+++ b/setup.cfg
@@ -0,0 +1 @@
+bad
"""
    changelog = upstream.replace("setup.cfg", "debian/changelog")
    bypass = """diff --git a/debian/tests/control b/debian/tests/control
--- a/debian/tests/control
+++ b/debian/tests/control
@@ -1 +1,2 @@
 Tests: smoke
+pytest.mark.skip
"""
    assert validate_source_patch(upstream, tmp_path)["result"] == "REJECTED"
    assert validate_source_patch(changelog, tmp_path)["result"] == "REJECTED"
    assert validate_source_patch(bypass, tmp_path)["result"] == "REJECTED"


def test_evidence_reader_ignores_tar_traversal_and_prioritizes_result(tmp_path):
    bundle = tmp_path / "evidence.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for name, content in (("result.json", b'{"result":"FAILED"}'), ("../secret.log", b"ignore")):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    text = safe_evidence_text(tmp_path)
    assert '"FAILED"' in text
    assert "ignore" not in text


def test_missing_import_adds_relevant_candidate_producer_catalog_context(tmp_path):
    (tmp_path / "nightly-plan").mkdir()
    (tmp_path / "nightly-plan/catalog.json").write_text(json.dumps({"packages": [
        {"source": "consumer", "binaries": ["python3-consumer"], "packaging_sha": "1" * 40},
        {"source": "python-client", "binaries": ["python3-client"], "packaging_sha": "2" * 40},
    ]}))
    context = repository_context(tmp_path, "consumer", "ModuleNotFoundError: No module named 'client.v1'")
    assert '"source": "consumer"' in context
    assert '"source": "python-client"' in context
    assert '"packaging_sha": "2222222222222222222222222222222222222222"' in context


def test_two_attempt_harness_keeps_raw_outputs_and_selects_applicable_patch(tmp_path):
    repository = tmp_path / "repo"
    (repository / "config").mkdir(parents=True)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "result.json").write_text('{"result":"FAILED","error":"boom"}\n')
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fake-model")
    runner = tmp_path / "llama-cli"
    runner.write_text("""#!/usr/bin/env python3
import sys
seed = sys.argv[sys.argv.index('--seed') + 1]
if seed == '1':
    patch = 'diff --git a/config/missing b/config/missing\\n--- a/config/missing\\n+++ b/config/missing\\n@@ -1 +1 @@\\n-old\\n+new\\n'
else:
    patch = 'diff --git a/config/fix.txt b/config/fix.txt\\nnew file mode 100644\\n--- /dev/null\\n+++ b/config/fix.txt\\n@@ -0,0 +1 @@\\n+fixed\\n'
print('BEGIN_DIAGNOSIS\\nboom requires a repository adaptation\\nEND_DIAGNOSIS')
print('BEGIN_PATCH')
print(patch, end='')
print('END_PATCH')
""")
    runner.chmod(0o755)
    output = tmp_path / "output"
    completed = subprocess.run([
        "python3", str(Path("scripts/analyze-packaging-failure.py").resolve()),
        "--source", "sample", "--phase", "build", "--original-result", "FAILED",
        "--evidence", str(evidence), "--repository", str(repository), "--output", str(output),
        "--llama-cli", str(runner), "--model", str(model), "--timeout", "10",
    ], text=True, capture_output=True, env={**os.environ, "GITHUB_RUN_ID": "123"})
    assert completed.returncode == 0, completed.stderr
    report = json.loads((output / "result.json").read_text())
    assert [attempt["result"] for attempt in report["attempts"]] == ["REJECTED", "APPLIES"]
    assert report["selected_attempt"] == 2
    assert (output / "attempt-1.output.txt").is_file()
    assert "Build correctness is unverified" in (output / "summary.md").read_text()


@pytest.mark.parametrize('target', ['/etc/passwd', 'a/../escape', 'a/debian/control', '/dev/null', 'file'])
def test_quilt_refresh_rejects_unsupported_target_paths(tmp_path, target):
    """Mechanical refresh must reject paths outside its upstream-file scope."""
    patches = tmp_path / 'debian/patches'
    patches.mkdir(parents=True)
    (patches / 'series').write_text('scope.patch\n')
    (patches / 'scope.patch').write_text(
        f'--- a/sample\n+++ {target}\n@@ -1 +1 @@\n-old\n+new\n')
    with pytest.raises(ValueError, match='does not support target'):
        render_source_repair({'action': 'refresh_quilt_patch', 'subject': 'scope.patch'}, tmp_path)


def test_quilt_refresh_rejects_duplicate_target_sections(tmp_path):
    """Repeated targets must fail before any partial section application occurs."""
    patches = tmp_path / 'debian/patches'
    patches.mkdir(parents=True)
    (patches / 'series').write_text('scope.patch\n')
    section = '--- a/sample\n+++ b/sample\n@@ -1 +1 @@\n-old\n+new\n'
    (patches / 'scope.patch').write_text(section + section)
    (tmp_path / 'sample').write_text('old\n')
    with pytest.raises(ValueError, match='unique upstream targets'):
        render_source_repair({'action': 'refresh_quilt_patch', 'subject': 'scope.patch'}, tmp_path)

    assert (tmp_path / 'sample').read_text() == 'old\n'


@pytest.mark.parametrize('strategy', ['_apply_hunks_with_normalized_context', '_apply_independent_change_groups'])
@pytest.mark.parametrize('tail', ['missing\n', 'second\nsecond\n'])
def test_context_repair_does_not_write_partial_edits(tmp_path, strategy, tail):
    """An absent or ambiguous later match must discard the earlier successful edit."""
    from packagetest import failure_analysis

    path = tmp_path / 'source.py'
    before = 'first\n' + tail
    path.write_text(before)
    section = '@@ -1 +1 @@\n-first\n+changed-first\n@@ -3 +3 @@\n-second\n+changed-second\n'

    assert getattr(failure_analysis, strategy)(path, section) is False
    assert path.read_text() == before


def test_normalized_repair_preserves_original_context_whitespace(tmp_path):
    """Only changed payload lines should adopt the patch's formatting."""
    from packagetest.failure_analysis import _apply_hunks_with_normalized_context

    path = tmp_path / 'source.py'
    path.write_text('\tcontext  value\n\told\n')
    section = '@@ -1,2 +1,2 @@\n context value\n- old\n+    new\n'

    assert _apply_hunks_with_normalized_context(path, section) is True
    assert path.read_text() == '\tcontext  value\n    new\n'


@pytest.mark.parametrize('invalid, message', [
    ({'action': 'unsupported', 'subject': '', 'replacement': '', 'evidence': ''}, 'unsupported repair action'),
    ({'action': 'no_fix', 'subject': None, 'replacement': '', 'evidence': ''}, 'values must be strings'),
])
def test_decision_parser_does_not_fall_back_past_invalid_final_decision(invalid, message):
    """An earlier valid response must not hide an invalid final model decision."""
    valid = {'action': 'no_fix', 'subject': '', 'replacement': '', 'evidence': ''}
    text = 'prompt echo: ' + json.dumps(valid) + '\nanswer: ' + json.dumps(invalid)

    with pytest.raises(ValueError, match=message):
        parse_repair_decision(text)


def test_evidence_reader_excludes_links_oversized_and_nontext_members(tmp_path):
    """Archive and filesystem exclusions must hold without extracting any members."""
    bundle = tmp_path / 'bundle.tar'
    with tarfile.open(bundle, 'w') as archive:
        for name, content in [('result.json', b'allowed-result'),
                              ('/absolute.log', b'absolute-secret'),
                              ('../escape.log', b'traversal-secret'),
                              ('binary.bin', b'binary-secret'),
                              ('huge.log', b'x' * (512 * 1024 + 1))]:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))

        link = tarfile.TarInfo('linked.log')
        link.type = tarfile.SYMTYPE
        link.linkname = 'result.json'
        archive.addfile(link)

    (tmp_path / 'linked.log').symlink_to(bundle)
    evidence = safe_evidence_text(tmp_path)

    assert 'allowed-result' in evidence
    assert 'secret' not in evidence
    assert 'huge.log' not in evidence
    assert 'linked.log' not in evidence
    assert not (tmp_path / 'result.json').exists()
