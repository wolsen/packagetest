"""Exercise candidate boundaries without launching packaging tools or a model."""

import hashlib
import json
import subprocess
from argparse import Namespace

import pytest

from packagetest import remediation


@pytest.mark.parametrize(
    ("build_rc", "build_result", "test_result", "expected"),
    [
        (1, "SUCCEEDED", None, "BUILD_FAILED"),
        (0, "FAILED", None, "BUILD_FAILED"),
        (0, "SUCCEEDED", "FAIL", "AUTOPKGTEST_FAILED"),
        (0, "SUCCEEDED", "INFRA_ERROR", "AUTOPKGTEST_FAILED"),
        (0, "SUCCEEDED", "BLOCKED", "AUTOPKGTEST_FAILED"),
        (0, "SUCCEEDED", "PASS", "REPAIRED"),
        (0, "SUCCEEDED", "SUPERFICIAL", "REPAIRED"),
        (0, "SUCCEEDED", "SKIP", "REPAIRED"),
        (0, "SUCCEEDED", "NO_TESTS", "REPAIRED"),
    ],
)
def test_candidate_execution_keeps_gates_separate_from_promotion(
    tmp_path, build_rc, build_result, test_result, expected
):
    calls = []
    outputs = tmp_path / "canonical"
    outputs.mkdir()
    (outputs / "original.deb").write_text("original")

    def build(args, patch, destination, log):
        calls.append("build")
        destination.mkdir(parents=True)
        (destination / "result.json").write_text(json.dumps({"result": build_result}))
        return build_rc

    def test(args, candidate, destination, log):
        calls.append("test")
        # Acceptance follows the classified result, including nonzero NO_TESTS.
        return 8, {"result": test_result}

    record = {}
    executor = remediation.CandidateExecutor(
        Namespace(work=tmp_path / "work"), build, test
    )
    outcome = executor.execute(1, tmp_path / "patch", tmp_path, record)
    assert outcome.status.value == expected
    assert calls == (["build"] if expected == "BUILD_FAILED" else ["build", "test"])
    assert (outputs / "original.deb").read_text() == "original"
    assert record["build_returncode"] == build_rc
    if test_result:
        assert record["test_returncode"] == 8


@pytest.mark.parametrize(
    "status",
    [
        remediation.CandidateStatus.BUILD_FAILED,
        remediation.CandidateStatus.AUTOPKGTEST_FAILED,
    ],
)
def test_rejected_candidate_cannot_replace_canonical_artifacts(tmp_path, status):
    outputs, tests = tmp_path / "outputs", tmp_path / "tests"
    for directory in (outputs, tests):
        directory.mkdir()
        (directory / "original").write_text("preserved")
    candidate = remediation.CandidateOutcome(
        status, tmp_path / "candidate", tmp_path / "candidate-tests", tmp_path / "log"
    )
    with pytest.raises(ValueError, match="Only a rebuilt"):
        remediation.ArtifactPromoter(outputs, tests).promote(candidate)
    assert all(
        (directory / "original").read_text() == "preserved"
        for directory in (outputs, tests)
    )


@pytest.fixture
def failed_run(tmp_path):
    outputs, tests = tmp_path / "outputs", tmp_path / "tests"
    tree = outputs / "source-preparation/sample-1/debian"
    tree.mkdir(parents=True)
    (tree / "control").write_text(
        "Source: sample\nBuild-Depends-Indep: python3-pbr\n\n"
        "Package: python3-sample\nArchitecture: all\n"
    )
    (outputs / "result.json").write_text('{"result":"FAILED"}')
    (outputs / "nightly-build.log").write_text(
        "ModuleNotFoundError: No module named 'futurist'\n"
    )
    (tests / "result").mkdir(parents=True)
    (tests / "result/result.json").write_text('{"result":"BLOCKED"}')
    model = tmp_path / "model"
    model.write_bytes(b"model")
    return Namespace(
        source="sample",
        outputs=outputs,
        tests=tests,
        model=model,
        model_sha256=hashlib.sha256(b"model").hexdigest(),
        report=tmp_path / "report",
        work=tmp_path / "work",
        llama_cli=tmp_path / "llama",
        model_timeout=10,
        max_model_calls=8,
        max_rebuilds=4,
        max_repairs=8,
        remediation_seconds=60,
        run_id="1",
        run_attempt="1",
    )


def read_report(args):
    return json.loads((args.report / "result.json").read_text())


def propose_dependency(*args):
    return json.dumps(
        {
            "action": "add_dependency",
            "subject": "python3-futurist",
            "replacement": "",
            "evidence": "missing futurist",
        }
    ), {"returncode": 0}


def test_repeated_proposal_after_unchanged_failure_stops_without_promoting(failed_run, monkeypatch):
    monkeypatch.setattr(remediation, "llama_generate", propose_dependency)

    def build(args, patch, destination, log):
        destination.mkdir(parents=True)
        (destination / "result.json").write_text('{"result":"FAILED"}')
        log.write_text("ModuleNotFoundError: No module named 'futurist'\n")
        return 1

    monkeypatch.setattr(remediation, "build_attempt", build)
    assert remediation.run(failed_run) == 1
    report = read_report(failed_run)
    assert report["progress"] == {
        "model_calls": 2,
        "rebuilds": 1,
        "validated_repairs": 1,
        "stop_reason": "model repeated an existing decision without progress",
    }
    assert report["attempts"][0]["result"] == "BUILD_FAILED"
    assert (
        json.loads((failed_run.outputs / "result.json").read_text())["result"]
        == "FAILED"
    )
    assert not failed_run.work.exists()


def test_failed_test_retains_original_artifacts_at_rebuild_budget(
    failed_run, monkeypatch
):
    failed_run.max_rebuilds = 1
    monkeypatch.setattr(remediation, "llama_generate", propose_dependency)

    def build(args, patch, destination, log):
        destination.mkdir(parents=True)
        (destination / "result.json").write_text('{"result":"SUCCEEDED"}')
        log.write_text("built")
        return 0

    def test(args, candidate, destination, log):
        path = destination / "test-results/result"
        path.mkdir(parents=True)
        (path / "result.json").write_text('{"result":"FAIL"}')
        log.write_text("FAIL: service behavior changed")
        return 4, {"result": "FAIL"}

    monkeypatch.setattr(remediation, "build_attempt", build)
    monkeypatch.setattr(remediation, "test_attempt", test)
    assert remediation.run(failed_run) == 1
    report = read_report(failed_run)
    assert report["selected_attempt"] is None
    assert report["attempts"][0]["result"] == "AUTOPKGTEST_FAILED"
    assert report["progress"]["stop_reason"] == "rebuild budget exhausted"
    assert (
        json.loads((failed_run.outputs / "result.json").read_text())["result"]
        == "FAILED"
    )
    assert (
        json.loads((failed_run.tests / "result/result.json").read_text())["result"]
        == "BLOCKED"
    )


@pytest.mark.parametrize("timeout_kind", ["model", "process"])
def test_timeout_stops_and_retains_diagnostics(failed_run, monkeypatch, timeout_kind):
    def timeout(*args):
        if timeout_kind == "model":
            raise remediation.ModelTimeoutError(
                10, "partial JSON", "still running", 10.0
            )
        raise subprocess.TimeoutExpired(["llama"], 10)

    monkeypatch.setattr(remediation, "llama_generate", timeout)
    assert remediation.run(failed_run) == 1
    report = read_report(failed_run)
    expected = "MODEL_TIMEOUT" if timeout_kind == "model" else "REMEDIATION_TIMEOUT"
    assert [attempt["result"] for attempt in report["attempts"]] == [expected]
    assert report["progress"]["model_calls"] == 1
    assert report["progress"]["rebuilds"] == 0
    if timeout_kind == "model":
        assert (
            failed_run.report / "attempt-1/model-output.txt"
        ).read_text() == "partial JSON"
        assert (
            failed_run.report / "attempt-1/model-stderr.txt"
        ).read_text() == "still running"


@pytest.mark.parametrize('budget, reason', [
    ('max_model_calls', 'model-call budget exhausted'),
    ('max_rebuilds', 'rebuild budget exhausted'),
])
def test_exhausted_initial_budget_does_not_start_an_attempt(failed_run, monkeypatch, budget, reason):
    """Publish the budget reason without invoking inference or replacing original artifacts."""
    setattr(failed_run, budget, 0)

    def unexpected(*args):
        """Fail immediately if a session with no available attempts invokes the model."""
        raise AssertionError('inference must not run with an exhausted budget')

    monkeypatch.setattr(remediation, 'llama_generate', unexpected)
    assert remediation.run(failed_run) == 1
    report = read_report(failed_run)
    assert report['attempts'] == []
    assert report['selected_attempt'] is None
    assert report['progress']['model_calls'] == 0
    assert report['progress']['rebuilds'] == 0
    assert report['progress']['stop_reason'] == reason
    assert json.loads((failed_run.outputs / 'result.json').read_text())['result'] == 'FAILED'


def generic_decision(before="Architecture: all", after="Architecture: any"):
    return {
        "action": "propose_patch", "diagnosis": "The binary requires a target architecture.",
        "evidence": "ModuleNotFoundError: No module named 'futurist'",
        "edits": [{"path": "debian/control", "before": before, "after": after}],
    }


def successful_gates(monkeypatch):
    def build(args, patch, destination, log):
        destination.mkdir(parents=True)
        (destination / "result.json").write_text('{"result":"SUCCEEDED"}')
        log.write_text("built")
        return 0

    def test(args, candidate, destination, log):
        (destination / "test-results/result").mkdir(parents=True)
        (destination / "test-results/result/result.json").write_text('{"result":"PASS"}')
        log.write_text("PASS")
        return 0, {"result": "PASS"}

    monkeypatch.setattr(remediation, "build_attempt", build)
    monkeypatch.setattr(remediation, "test_attempt", test)


def test_general_agent_inspects_then_passes_both_candidate_gates(failed_run, monkeypatch):
    responses = iter([
        {"action": "inspect", "requests": [{"op": "read", "path": "debian/control"}]},
        generic_decision(),
    ])
    prompts = []

    def generate(*args):
        prompts.append(args[2])
        return json.dumps(next(responses)), {"returncode": 0}

    monkeypatch.setattr(remediation, "llama_generate", generate)
    successful_gates(monkeypatch)
    assert remediation.run(failed_run) == 0
    report = read_report(failed_run)
    assert report["progress"]["model_calls"] == 2
    assert report["progress"]["rebuilds"] == 1
    assert [item["result"] for item in report["attempts"]] == ["INSPECTED", "REPAIRED"]
    assert "Architecture: all" in prompts[1]
    assert (failed_run.report / "attempt-1/inspection.json").is_file()
    assert report["attempts"][1]["decision_validation"]["human_review_required"] is True


def test_inspection_turns_obey_model_budget_without_rebuild(failed_run, monkeypatch):
    failed_run.max_model_calls = 2
    monkeypatch.setattr(remediation, "llama_generate", lambda *args: (json.dumps({"action": "inspect", "requests": [{"op": "list", "path": ""}]}), {}))
    assert remediation.run(failed_run) == 1
    report = read_report(failed_run)
    assert report["progress"]["stop_reason"] == "model-call budget exhausted"
    assert report["progress"]["rebuilds"] == 0
    assert [item["result"] for item in report["attempts"]] == ["INSPECTED", "INSPECTED"]


def test_rejected_preimage_is_feedback_and_never_consumes_a_build(failed_run, monkeypatch):
    responses = iter([generic_decision("not in the source"), generic_decision()])
    prompts = []

    def generate(*args):
        prompts.append(args[2])
        return json.dumps(next(responses)), {}

    monkeypatch.setattr(remediation, "llama_generate", generate)
    successful_gates(monkeypatch)
    assert remediation.run(failed_run) == 0
    report = read_report(failed_run)
    assert report["progress"]["rebuilds"] == 1
    assert report["attempts"][0]["result"] == "RENDER_REJECTED"
    assert "Preimage must occur exactly once" in prompts[1]


def test_ineffective_patch_is_rolled_back_before_a_different_proposal(failed_run, monkeypatch):
    responses = iter([generic_decision(), generic_decision("Build-Depends-Indep: python3-pbr", "Build-Depends-Indep: python3-pbr, python3-futurist")])
    monkeypatch.setattr(remediation, "llama_generate", lambda *args: (json.dumps(next(responses)), {}))
    successful_gates(monkeypatch)
    good_build = remediation.build_attempt
    patches = []

    def build(args, patch, destination, log):
        patches.append(patch.read_text())
        if len(patches) == 1:
            destination.mkdir(parents=True)
            (destination / "result.json").write_text('{"result":"FAILED"}')
            log.write_text("ModuleNotFoundError: No module named 'futurist'\n")
            return 1
        return good_build(args, patch, destination, log)

    monkeypatch.setattr(remediation, "build_attempt", build)
    assert remediation.run(failed_run) == 0
    assert "+Architecture: any" in patches[0]
    assert "+Architecture: any" not in patches[1]
    assert "+Build-Depends-Indep: python3-pbr, python3-futurist" in patches[1]
    assert read_report(failed_run)["progress"]["rebuilds"] == 2
