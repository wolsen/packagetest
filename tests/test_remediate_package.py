import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def load_script():
    path = Path("scripts/remediate-package.py").resolve()
    spec = importlib.util.spec_from_file_location("remediate_package", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


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

    response = '{"action":"add_dependency","package":"python3-futurist","argument":"","evidence":"missing futurist"}'
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
    response = '{"action":"add_dependency","package":"python3-oslo.config","argument":"","evidence":"wrong"}'
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
        "DECISION_REJECTED", "DUPLICATE_DECISION"
    ]
    assert json.loads((tmp_path / "report/attempt-1/decision.json").read_text())["package"] == "python3-oslo.config"


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
        '{"action":"add_dependency","package":"python3-futurist","argument":"","evidence":"missing futurist"}',
        '{"action":"add_dependency","package":"python3-ncclient","argument":"","evidence":"missing ncclient"}',
        '{"action":"add_dependency","package":"python3-gabbi","argument":"","evidence":"missing gabbi"}',
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
    report = json.loads((tmp_path / "report/result.json").read_text())
    assert [attempt["result"] for attempt in report["attempts"]] == [
        "BUILD_FAILED", "BUILD_FAILED", "REPAIRED"]
    assert report["selected_attempt"] == 3
