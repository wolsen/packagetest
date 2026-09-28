import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


def load_script():
    path = Path("scripts/remediate-package.py").resolve()
    spec = importlib.util.spec_from_file_location("remediate_package", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_source_context_exposes_failed_patch_target_and_moved_path(tmp_path):
    module = load_script()
    (tmp_path / "debian/patches").mkdir(parents=True)
    (tmp_path / "debian/control").write_text("Source: sample\n")
    (tmp_path / "debian/rules").write_text(
        "oslo-config-generator --config-file=aodh/cmd/aodh-config-generator.conf\n")
    (tmp_path / "debian/patches/series").write_text("feature.patch\n")
    (tmp_path / "debian/patches/feature.patch").write_text(
        "--- a/sample/module.py\n+++ b/sample/module.py\n@@ -1 +1 @@\n-old\n+new\n")
    (tmp_path / "sample").mkdir()
    (tmp_path / "sample/module.py").write_text("final upstream implementation\n")
    (tmp_path / "etc/aodh").mkdir(parents=True)
    (tmp_path / "etc/aodh/aodh-config-generator.conf").write_text("[DEFAULT]\n")
    evidence = ("feature.patch subprocess returned exit status 1; "
                "aodh/cmd/aodh-config-generator.conf was not found")
    context = module.source_context(tmp_path, evidence)
    assert "current upstream sample/module.py" in context
    assert "final upstream implementation" in context
    assert "upstream candidates for missing aodh/cmd/aodh-config-generator.conf" in context
    assert "etc/aodh/aodh-config-generator.conf" in context


def test_missing_import_context_contains_only_source_build_dependencies(tmp_path):
    module = load_script()
    (tmp_path / "debian").mkdir()
    (tmp_path / "debian/control").write_text("""Source: watcher
Build-Depends:
 debhelper-compat (= 13),
Build-Depends-Indep:
 python3-stestr,

Package: python3-watcher
Depends:
 python3-runtime-only,
Description: binary stanza must not reach the model
""")
    evidence = """ModuleNotFoundError: No module named 'wsgi_intercept'
ModuleNotFoundError: No module named 'gabbi'
"""
    context = module.source_context(tmp_path, evidence)
    assert "Build-Depends-Indep:" in context
    assert "python3-stestr" in context
    assert "python3-wsgi-intercept" in context
    assert "python3-gabbi" in context
    assert "python3-runtime-only" not in context
    assert "binary stanza" not in context


def test_missing_import_focus_omits_unrelated_build_log(tmp_path):
    module = load_script()
    evidence = """thousands of irrelevant build lines
Failed to import test module: watcher.tests.functional.test_basic
traceback detail
ModuleNotFoundError: No module named 'wsgi_intercept'
more unrelated output
dpkg-buildpackage: error: debian/rules binary subprocess failed
"""
    focused = module.failure_focus(evidence)
    assert "Failed to import test module" in focused
    assert "ModuleNotFoundError" in focused
    assert "dpkg-buildpackage: error" in focused
    assert "irrelevant" not in focused


def test_batches_straightforward_missing_imports_into_one_rebuild(tmp_path):
    module = load_script()
    evidence = """ModuleNotFoundError: No module named 'wsgi_intercept'
ModuleNotFoundError: No module named 'gabbi'
ModuleNotFoundError: No module named 'oslo_config'
"""
    primary = {
        "action": "add_dependency", "subject": "python3-wsgi-intercept", "replacement": "",
        "evidence": "ModuleNotFoundError: No module named 'wsgi_intercept'",
    }
    batched = module.batch_missing_dependency_decisions(primary, evidence)
    assert [decision["subject"] for decision in batched] == [
        "python3-wsgi-intercept", "python3-gabbi"]
    assert all(module.validate_repair_decision(decision, evidence)["result"] == "ACCEPTED"
               for decision in batched)


def test_failure_signature_tracks_progress_between_missing_module_sets():
    module = load_script()
    first = """volatile path /home/runner/work/_temp/one
ModuleNotFoundError: No module named 'gabbi'
ModuleNotFoundError: No module named 'werkzeug'
"""
    reordered = """different surrounding log
ModuleNotFoundError: No module named 'werkzeug'
ModuleNotFoundError: No module named 'gabbi'
"""
    progressed = "ModuleNotFoundError: No module named 'werkzeug'\n"
    assert module.failure_signature(first) == module.failure_signature(reordered)
    assert module.failure_signature(first) != module.failure_signature(progressed)


def test_llama_timeout_retains_partial_output_and_uses_small_budget(tmp_path, monkeypatch):
    module = load_script()
    executable = tmp_path / "llama-cli"
    executable.write_text("fake")
    model = tmp_path / "model.gguf"
    model.write_text("fake")
    captured = {}

    def timeout(command, **kwargs):
        captured["command"] = command
        raise subprocess.TimeoutExpired(command, kwargs["timeout"],
                                        output='{"action":"add_', stderr="still running")

    monkeypatch.setattr(module.subprocess, "run", timeout)
    try:
        module.llama_generate(executable, model, "prompt", 1, 7)
    except module.ModelTimeoutError as exc:
        assert exc.stdout == '{"action":"add_'
        assert exc.stderr == "still running"
        assert exc.metadata["timeout_seconds"] == 7
    else:
        raise AssertionError("expected inference timeout")
    assert captured["command"][captured["command"].index("-n") + 1] == "128"


def test_successful_attempt_promotes_rebuilt_and_retested_candidate(tmp_path, monkeypatch):
    module = load_script()
    outputs = tmp_path / "outputs"
    tree = outputs / "source-preparation/sample-1"
    (tree / "debian").mkdir(parents=True)
    (tree / "debian/control").write_text("""Source: sample
Build-Depends: debhelper-compat (= 13),
Build-Depends-Indep:
 python3-pbr,

Package: python3-sample
Architecture: all
Depends:
 ${python3:Depends},
""")
    (outputs / "result.json").write_text('{"result":"FAILED"}\n')
    (outputs / "nightly-build.log").write_text("ModuleNotFoundError: No module named 'futurist'\n")
    tests = tmp_path / "test-results"
    (tests / "result").mkdir(parents=True)
    (tests / "result/result.json").write_text('{"result":"BLOCKED"}\n')
    model = tmp_path / "model.gguf"
    model.write_bytes(b"model")
    llama = tmp_path / "llama-cli"
    llama.write_text("fake")
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"packages":[]}\n')
    inputs = tmp_path / "inputs"
    inputs.mkdir()

    response = '{"action":"add_dependency","subject":"python3-futurist","replacement":"","evidence":"missing futurist"}'
    monkeypatch.setattr(module, "llama_generate", lambda *args, **kwargs: (response, {"returncode": 0}))

    def build(args, patch, destination, log):
        destination.mkdir(parents=True)
        (destination / "result.json").write_text('{"result":"SUCCEEDED"}\n')
        (destination / "fixed.deb").write_text("fixed")
        log.write_text("built")
        return 0

    def test(args, candidate, destination, log):
        result = destination / "test-results/result"
        result.mkdir(parents=True)
        report = {"result": "PASS"}
        (result / "result.json").write_text(json.dumps(report))
        log.write_text("passed")
        return 0, report

    monkeypatch.setattr(module, "build_attempt", build)
    monkeypatch.setattr(module, "test_attempt", test)
    monkeypatch.setattr(sys, "argv", [
        "remediate-package.py", "--source", "sample", "--catalog", str(catalog),
        "--inputs", str(inputs), "--outputs", str(outputs), "--tests", str(tests),
        "--report", str(tmp_path / "report"), "--work", str(tmp_path / "work"),
        "--llama-cli", str(llama), "--model", str(model),
        "--model-sha256", hashlib.sha256(b"model").hexdigest(),
        "--run-id", "1", "--run-attempt", "1",
    ])

    assert module.main() == 0
    assert (outputs / "fixed.deb").read_text() == "fixed"
    assert json.loads((tests / "result/result.json").read_text())["result"] == "PASS"
    report = json.loads((tmp_path / "report/result.json").read_text())
    assert report["result"] == "REPAIRED"
    assert report["selected_attempt"] == 1
    assert (tmp_path / "report/attempt-1/proposal.patch").is_file()
    assert "python3-futurist" in (tmp_path / "report/attempt-1/proposal.patch").read_text()


def test_rejected_decision_is_preserved_without_building(tmp_path, monkeypatch):
    module = load_script()
    outputs = tmp_path / "outputs"
    tree = outputs / "source-preparation/sample-1"
    (tree / "debian").mkdir(parents=True)
    (tree / "debian/control").write_text("""Source: sample
Build-Depends: python3-oslo.config,

Package: sample
Architecture: all
Depends: ${misc:Depends},
""")
    (tree / "debian/rules").write_text("cmd --format yaml\n")
    (outputs / "result.json").write_text('{"result":"FAILED"}\n')
    (outputs / "nightly-build.log").write_text("tool: error: unrecognized arguments: --format yaml\n")
    tests = tmp_path / "test-results/result"
    tests.mkdir(parents=True)
    (tests / "result.json").write_text('{"result":"BLOCKED"}\n')
    model = tmp_path / "model.gguf"
    model.write_bytes(b"model")
    llama = tmp_path / "llama-cli"
    llama.write_text("fake")
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"packages":[]}\n')
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    response = '{"action":"add_dependency","subject":"python3-oslo.config","replacement":"","evidence":"wrong"}'
    monkeypatch.setattr(module, "llama_generate", lambda *args, **kwargs: (response, {"returncode": 0}))
    monkeypatch.setattr(sys, "argv", [
        "remediate-package.py", "--source", "sample", "--catalog", str(catalog),
        "--inputs", str(inputs), "--outputs", str(outputs), "--tests", str(tmp_path / "test-results"),
        "--report", str(tmp_path / "report"), "--work", str(tmp_path / "work"),
        "--llama-cli", str(llama), "--model", str(model),
        "--model-sha256", hashlib.sha256(b"model").hexdigest(), "--run-id", "1", "--run-attempt", "1",
    ])
    assert module.main() == 1
    report = json.loads((tmp_path / "report/result.json").read_text())
    assert [attempt["result"] for attempt in report["attempts"]] == [
        "DECISION_REJECTED", "DECISION_REJECTED"
    ]
    assert report["progress"]["stop_reason"] == (
        "model could not produce a valid decision for unchanged evidence")
    assert json.loads((tmp_path / "report/attempt-1/decision.json").read_text())["subject"] == "python3-oslo.config"


def test_later_attempts_keep_every_valid_repair(tmp_path, monkeypatch):
    module = load_script()
    outputs = tmp_path / "outputs"
    tree = outputs / "source-preparation/sample-1"
    (tree / "debian").mkdir(parents=True)
    (tree / "debian/control").write_text("""Source: sample
Build-Depends-Indep:
 python3-pbr,

Package: python3-sample
Architecture: all
Depends:
 ${python3:Depends},
""")
    (outputs / "result.json").write_text('{"result":"FAILED"}\n')
    (outputs / "nightly-build.log").write_text("ModuleNotFoundError: No module named 'futurist'\n")
    tests = tmp_path / "test-results/result"
    tests.mkdir(parents=True)
    (tests / "result.json").write_text('{"result":"BLOCKED"}\n')
    model = tmp_path / "model.gguf"
    model.write_bytes(b"model")
    llama = tmp_path / "llama-cli"
    llama.write_text("fake")
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"packages":[]}\n')
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    responses = iter([
        '{"action":"add_dependency","subject":"python3-futurist","replacement":"","evidence":"missing futurist"}',
        '{"action":"add_dependency","subject":"python3-ncclient","replacement":"","evidence":"missing ncclient"}',
        '{"action":"add_dependency","subject":"python3-gabbi","replacement":"","evidence":"missing gabbi"}',
        '{"action":"add_dependency","subject":"python3-debtcollector","replacement":"","evidence":"missing debtcollector"}',
    ])
    monkeypatch.setattr(module, "llama_generate",
                        lambda *args, **kwargs: (next(responses), {"returncode": 0}))
    builds = []

    def build(args, patch, destination, log):
        builds.append(patch.read_text())
        destination.mkdir(parents=True)
        if len(builds) == 1:
            (destination / "result.json").write_text('{"result":"FAILED"}\n')
            log.write_text("ModuleNotFoundError: No module named 'ncclient'\n")
            return 1
        if len(builds) == 2:
            (destination / "result.json").write_text('{"result":"FAILED"}\n')
            log.write_text("ModuleNotFoundError: No module named 'gabbi'\n")
            return 1
        if len(builds) == 3:
            (destination / "result.json").write_text('{"result":"FAILED"}\n')
            log.write_text("ModuleNotFoundError: No module named 'debtcollector'\n")
            return 1
        (destination / "result.json").write_text('{"result":"SUCCEEDED"}\n')
        (destination / "fixed.deb").write_text("fixed")
        log.write_text("built")
        return 0

    def test(args, candidate, destination, log):
        result = destination / "test-results/result"
        result.mkdir(parents=True)
        report = {"result": "PASS"}
        (result / "result.json").write_text(json.dumps(report))
        log.write_text("passed")
        return 0, report

    monkeypatch.setattr(module, "build_attempt", build)
    monkeypatch.setattr(module, "test_attempt", test)
    monkeypatch.setattr(sys, "argv", [
        "remediate-package.py", "--source", "sample", "--catalog", str(catalog),
        "--inputs", str(inputs), "--outputs", str(outputs),
        "--tests", str(tmp_path / "test-results"), "--report", str(tmp_path / "report"),
        "--work", str(tmp_path / "work"), "--llama-cli", str(llama), "--model", str(model),
        "--model-sha256", hashlib.sha256(b"model").hexdigest(), "--run-id", "1", "--run-attempt", "1",
    ])

    assert module.main() == 0
    assert "python3-futurist" in builds[0]
    assert "python3-futurist" in builds[1]
    assert "python3-ncclient" in builds[1]
    assert "python3-futurist" in builds[2]
    assert "python3-ncclient" in builds[2]
    assert "python3-gabbi" in builds[2]
    assert "python3-futurist" in builds[3]
    assert "python3-ncclient" in builds[3]
    assert "python3-gabbi" in builds[3]
    assert "python3-debtcollector" in builds[3]
    report = json.loads((tmp_path / "report/result.json").read_text())
    assert [attempt["result"] for attempt in report["attempts"]] == [
        "BUILD_FAILED", "BUILD_FAILED", "BUILD_FAILED", "REPAIRED"]
    assert report["selected_attempt"] == 4
    assert report["progress"] == {
        "model_calls": 4, "rebuilds": 4, "validated_repairs": 4,
        "stop_reason": "package rebuilt and passed autopkgtest",
    }
