import json

from packagetest.remediation_evidence import extract_test_failure_evidence, lintian_evidence, structured_failure


def lintian_payload(fail_on, output):
    return {
        "category": "LINTIAN",
        "command_exit_code": 2,
        "failed_command": ["lintian", f"--fail-on={fail_on}", "sample.changes"],
        "command": {"stdout": "", "stderr": output},
    }


def test_lintian_evidence_respects_error_threshold():
    evidence = lintian_evidence(lintian_payload("error", "E: bad-error\nW: useful-warning\n"))
    assert "fail_on: error" in evidence
    assert "gating_tags: 1" in evidence
    assert "E: bad-error" in evidence
    assert "W: useful-warning" in evidence


def test_lintian_evidence_treats_errors_and_warnings_as_gating_at_warning_threshold():
    evidence = lintian_evidence(lintian_payload("warning", "E: bad-error\nW: bad-warning\nI: note\n"))
    assert "gating_tags: 2" in evidence
    assert "E: bad-error" in evidence
    assert "W: bad-warning" in evidence


def test_structured_lintian_failure_uses_full_command_output(tmp_path):
    payload = lintian_payload("error", "E: fatal-before-many-warnings\n" + "W: noise\n" * 500)
    path = tmp_path / "failure.json"
    path.write_text(json.dumps(payload))
    evidence = structured_failure(path)
    assert "E: fatal-before-many-warnings" in evidence
    assert evidence.count("W: noise") == 20


def test_test_failure_evidence_keeps_traceback_and_drops_pip_freeze():
    log = """FAIL: pkg.tests.TestDriver.test_client
Traceback (most recent call last):
  File \"test_driver.py\", line 1, in test_client
AttributeError: module 'taskflow.utils' has no attribute 'kazoo_utils'
----------------------------------------------------------------------
Ran 1649 tests in 47.979s
FAILED (failures=8)
======> STESTR TEST SUITE FAILED FOR python3.14: displaying pip3 freeze output...
taskflow==6.4.0
hundreds-of-packages==1
"""
    evidence = extract_test_failure_evidence(log)
    assert "FAIL: pkg.tests.TestDriver.test_client" in evidence
    assert "kazoo_utils" in evidence
    assert "FAILED (failures=8)" in evidence
    assert "taskflow==6.4.0" not in evidence
