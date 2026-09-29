import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

from packagetest.failure_analysis import (
    normalize_repair_decision,
    parse_repair_decision,
    parse_model_response,
    render_source_repair,
    repository_context,
    safe_evidence_text,
    select_direct_failures,
    validate_source_patch,
    validate_repair_decision,
    validate_patch,
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


def test_missing_import_adds_relevant_candidate_producer_context(tmp_path):
    (tmp_path / "config/patches/consumer").mkdir(parents=True)
    (tmp_path / "config/patches/python-client").mkdir(parents=True)
    (tmp_path / "config/patches/consumer/adjustments.json").write_text('{"consumer": true}\n')
    (tmp_path / "config/patches/python-client/adjustments.json").write_text('{"producer": true}\n')
    (tmp_path / "config/hibiscus-catalog.json").write_text(json.dumps({"packages": [{
        "source": "python-client", "binaries": ["python3-client"]}]}))
    context = repository_context(tmp_path, "consumer", "ModuleNotFoundError: No module named 'client.v1'")
    assert '"consumer": true' in context
    assert '"producer": true' in context


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
