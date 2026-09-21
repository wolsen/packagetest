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
    (tree / "debian/control").write_text("old\n")
    (outputs / "result.json").write_text('{"result":"FAILED"}\n')
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

    response = """BEGIN_DIAGNOSIS
The control file is wrong.
END_DIAGNOSIS
BEGIN_PATCH
diff --git a/debian/control b/debian/control
--- a/debian/control
+++ b/debian/control
@@ -1 +1 @@
-old
+new
END_PATCH
"""
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
