import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

from packagetest.failure_analysis import (
    parse_model_response,
    repository_context,
    safe_evidence_text,
    select_direct_failures,
    validate_patch,
)


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
