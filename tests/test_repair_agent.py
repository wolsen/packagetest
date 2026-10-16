"""Exercise bounded investigation, exact edits, and real quilt application."""

import json

import pytest

from packagetest.failure_analysis import validate_source_patch
from packagetest.repair_agent import (
    MAX_FILE_BYTES,
    PatchPlan,
    SourceInspector,
    materialize_source,
    parse_response,
    render_edit_repair,
    safe_path,
)


@pytest.fixture
def source(tmp_path):
    tree = tmp_path / "source"
    (tree / "debian/source").mkdir(parents=True)
    (tree / "debian/source/format").write_text("3.0 (quilt)\n")
    (tree / "debian/control").write_text(
        "Source: sample\nBuild-Depends: debhelper-compat (= 13)\n\n"
        "Package: sample\nArchitecture: all\nDescription: sample\n example\n"
    )
    (tree / "debian/changelog").write_text(
        "sample (1.0-1) unstable; urgency=medium\n\n  * Initial release.\n\n"
        " -- Example <example@example.org>  Fri, 16 Oct 2026 12:00:00 +0000\n"
    )
    (tree / "worker.py").write_text("def pending(queue):\n    return queue.pop()\n")
    return tree


def proposal(path="worker.py", before="return queue.pop()", after="return queue[0]", **fields):
    return {
        "action": "propose_patch",
        "diagnosis": "A pending item must remain visible until dispatch.",
        "evidence": "pending task disappeared",
        "edits": [{"path": path, "before": before, "after": after}],
        **fields,
    }


@pytest.mark.parametrize("name", ["/etc/passwd", "../secret", "a/../b", "./worker.py", "a//b", "a\\b", "a\nb", ".git/config", ".pc/applied-patches"])
def test_inspection_rejects_escaping_private_or_ambiguous_paths(source, name):
    with pytest.raises(ValueError):
        safe_path(source, name)


def test_inspection_never_follows_file_or_parent_symlinks(source, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("private")
    (source / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        safe_path(source, "link/secret")
    result = json.loads(SourceInspector(source).inspect({"action": "inspect", "requests": [
        {"op": "search", "path": "", "query": "private"},
        {"op": "read", "path": "link/secret"},
    ]}))
    assert result[0]["output"] == []
    assert "Symlink" in result[1]["error"]


def test_inspection_returns_numbered_evidence_and_explicit_tool_errors(source):
    result = json.loads(SourceInspector(source).inspect({"action": "inspect", "requests": [
        {"op": "read", "path": "worker.py", "start": 2, "lines": 1},
        {"op": "search", "path": "", "query": "queue.pop"},
        {"op": "shell", "path": "", "command": "env"},
    ]}))
    assert result[0]["output"] == ["2:     return queue.pop()"]
    assert result[1]["output"] == [{"path": "worker.py", "line": 2, "text": "    return queue.pop()"}]
    assert "Supported inspection" in result[2]["error"]


@pytest.mark.parametrize("content, error", [(b"a" * (MAX_FILE_BYTES + 1), "256 KiB"), (b"a\0b", "Binary"), (b"\xff", "decode")])
def test_inspection_bounds_and_rejects_nontext(source, content, error):
    (source / "worker.py").write_bytes(content)
    result = json.loads(SourceInspector(source).inspect({"action": "inspect", "requests": [{"op": "read", "path": "worker.py"}]}))
    assert error in result[0]["error"]


@pytest.mark.parametrize("tool_request", [{"op": "read", "path": "worker.py", "lines": True}, {"op": "read", "path": "worker.py", "lines": 121}, {"op": "read", "path": "worker.py", "command": "cat"}])
def test_invalid_inspection_parameters_produce_feedback(source, tool_request):
    assert "error" in json.loads(SourceInspector(source).inspect({"action": "inspect", "requests": [tool_request]}))[0]


def test_inventory_explicitly_marks_truncation(source):
    for number in range(110):
        (source / f"item{number}.txt").write_text("text\n")
    result = json.loads(SourceInspector(source).inspect({"action": "inspect", "requests": [{"op": "list", "path": ""}]}))[0]["output"]
    assert result["truncated"] is True
    assert len(result["paths"]) == 100


def test_response_rejects_duplicate_keys_and_wrong_evidence():
    with pytest.raises(ValueError, match="Duplicate"):
        parse_response('{"action":"inspect","action":"propose_patch"}')
    with pytest.raises(ValueError, match="current failure"):
        PatchPlan.from_mapping(proposal(), "a different failure")
    assert parse_response("```json\n" + json.dumps(proposal()) + "\n```")["action"] == "propose_patch"


@pytest.mark.parametrize("path", ["debian/changelog", "debian/source/format", "debian/patches/series", "debian/sample.postinst", "debian/postinst", "debian/config"])
def test_generic_edits_cannot_modify_identity_quilt_bookkeeping_or_hooks(source, path):
    with pytest.raises(ValueError):
        render_edit_repair(proposal(path, "", "replacement\n"), source)


@pytest.mark.parametrize("before", ["not present", "", "return"])
def test_edits_require_one_exact_preimage(source, before):
    (source / "worker.py").write_text("return one\nreturn two\n")
    with pytest.raises(ValueError, match="Preimage"):
        render_edit_repair(proposal(before=before), source)


@pytest.mark.packaging_component
def test_upstream_edit_becomes_quilt_and_applies_without_touching_original(source):
    patch = render_edit_repair(proposal(), source)
    assert "Origin: other, packagetest local model proposal" in patch
    assert validate_source_patch(patch, source)["result"] == "APPLIES"
    assert (source / "worker.py").read_text().endswith("return queue.pop()\n")
    assert not (source / "debian/patches").exists()
    assert validate_source_patch(patch, source, apply=True)["result"] == "APPLIED"
    materialize_source(source)
    assert (source / "worker.py").read_text().endswith("return queue[0]\n")


@pytest.mark.packaging_component
def test_new_upstream_regression_file_is_in_quilt_patch(source):
    decision = proposal("test_worker.py", "", "def test_pending():\n    assert True\n")
    patch = render_edit_repair(decision, source)
    assert validate_source_patch(patch, source, apply=True)["result"] == "APPLIED"
    materialize_source(source)
    assert (source / "test_worker.py").read_text() == decision["edits"][0]["after"]


@pytest.mark.packaging_component
def test_cumulative_upstream_edits_use_already_applied_quilt_preimages(source):
    from packagetest.remediation import render_cumulative_repair
    first = proposal()
    second = proposal(before="return queue[0]", after="return queue.peek()")
    patch = render_cumulative_repair([first, second], source)
    assert validate_source_patch(patch, source, apply=True)["result"] == "APPLIED"
    materialize_source(source)
    assert "queue.peek()" in (source / "worker.py").read_text()
    assert len((source / "debian/patches/series").read_text().splitlines()) == 2


@pytest.mark.parametrize("after", [
    "class A:\n    def test_value(self):\n        pass\nclass B:\n    def test_value(self):\n        assert True\ndef test_new():\n    assert True\n",
    "class B:\n    def test_value(self):\n        assert True\ndef test_new():\n    assert True\n",
])
def test_regression_guard_preserves_qualified_tests_and_assertions(source, after):
    before = "class A:\n    def test_value(self):\n        assert True\nclass B:\n    def test_value(self):\n        assert True\n"
    (source / "test_worker.py").write_text(before)
    with pytest.raises(ValueError, match="remove existing tests"):
        render_edit_repair(proposal("test_worker.py", before, after), source)


def test_changing_test_expectation_requires_regression(source):
    before = "def test_value():\n    assert current == 2024\n"
    (source / "test_worker.py").write_text(before)
    with pytest.raises(ValueError, match="added regression"):
        render_edit_repair(proposal("test_worker.py", "== 2024", ">= 2024"), source)


@pytest.mark.parametrize("line", ["sample: statically-linked-binary [usr/bin/*]", "sample: arch-independent-package-contains-binary-or-object [usr/bin/helper]", "sample: statically-linked-binary [usr/bin/other]"])
def test_overrides_cannot_hide_architecture_mismatch_or_unrelated_files(source, line):
    decision = proposal("debian/sample.lintian-overrides", "", "# The tool deliberately links its runtime.\n" + line + "\n", evidence="statically-linked-binary [usr/bin/helper]")
    with pytest.raises(ValueError):
        render_edit_repair(decision, source)


def test_explained_matching_override_preserves_unrelated_existing_declarations(source):
    path = source / "debian/sample.lintian-overrides"
    path.write_text("# Existing policy\nsample: existing-tag\n")
    decision = proposal("debian/sample.lintian-overrides", "sample: existing-tag\n", "sample: existing-tag\n# Runtime is intentionally linked into this one tool.\nsample: statically-linked-binary [usr/bin/helper]\n", evidence="statically-linked-binary [usr/bin/helper]")
    patch = render_edit_repair(decision, source)
    assert validate_source_patch(patch, source)["result"] == "APPLIES"


def test_nested_quilt_patch_still_passes_test_bypass_policy(source):
    patch = render_edit_repair(proposal("test_worker.py", "", "import unittest\n@unittest.skip('failure')\ndef test_value():\n    assert True\n"), source)
    assert validate_source_patch(patch, source)["result"] == "REJECTED"


def test_inspection_feedback_is_bounded_even_with_heavily_escaped_text(source):
    from packagetest.repair_agent import MAX_TOOL_OUTPUT
    (source / "worker.py").write_text(('"\\' * 1000 + '\n') * 30)
    result = SourceInspector(source).inspect({"action": "inspect", "requests": [{"op": "read", "path": "worker.py"}]})
    assert len(result) <= MAX_TOOL_OUTPUT
    assert json.loads(result)["truncated"] is True


def test_inventory_pages_are_stable_and_directories_are_navigable(source):
    for number in range(110):
        (source / f"item{number:03}.txt").write_text("text\n")
    inspector = SourceInspector(source)
    first, second = [json.loads(inspector.inspect({"action": "inspect", "requests": [{"op": "list", "path": "", "start": start}]}))[0]["output"] for start in (1, 101)]
    assert first["paths"][0] == "debian/"
    assert not set(first["paths"]) & set(second["paths"])
    assert second["truncated"] is False


@pytest.mark.packaging_component
def test_broken_quilt_stack_remains_inspectable_for_mechanical_repair(source):
    from argparse import Namespace

    from packagetest.remediation import ProposalPlanner, RepairProgress
    (source / "debian/patches").mkdir()
    (source / "debian/patches/series").write_text("broken.patch\n")
    (source / "debian/patches/broken.patch").write_text("--- a/worker.py\n+++ b/worker.py\n@@ -1 +1 @@\n-not present\n+replacement\n")
    planner = ProposalPlanner(Namespace(), source, RepairProgress())
    context = planner.context("broken.patch failed to apply")
    assert "QUILT MATERIALIZATION FAILED" in context
    assert "broken.patch" in context


def test_source_preparation_uses_general_inspection_without_a_rebuild(source, tmp_path, monkeypatch):
    from argparse import Namespace

    from packagetest import remediation, source_remediation
    initial = tmp_path / "initial"
    initial.mkdir()
    source.rename(initial / "sample-1")
    output, report = tmp_path / "output", tmp_path / "report"
    output.mkdir()
    report.mkdir()
    args = Namespace(initial_work=initial, source="sample", output=output, report=report,
                     work=tmp_path / "work", model=tmp_path / "model", llama_cli=tmp_path / "llama", model_timeout=10, max_model_calls=2)
    monkeypatch.setattr(source_remediation, "failure_evidence", lambda *args: "Architecture mismatch")
    responses = iter([
        {"action": "inspect", "requests": [{"op": "read", "path": "debian/control"}]},
        proposal("debian/control", "Architecture: all", "Architecture: any", evidence="Architecture mismatch"),
    ])
    monkeypatch.setattr(remediation, "llama_generate", lambda *args: (json.dumps(next(responses)), {}))
    session = source_remediation.SourceRepairSession(args)
    preparations = []

    def prepare(attempt, patch, record):
        path, validation = session._validated_patch(attempt, patch)
        preparations.append(path)
        candidate = tmp_path / "candidate"
        candidate.mkdir()
        (candidate / "prepared-source.json").write_text('{}')
        record.update(preparation_returncode=0, patch_validation=validation, result="REPAIRED")
        return tmp_path / "work", candidate, attempt / "log"

    monkeypatch.setattr(session, "_prepare", prepare)
    session.model_repairs()
    assert [item["result"] for item in session.attempts] == ["INSPECTED", "REPAIRED"]
    assert len(preparations) == 1
    assert (output / "prepared-source.json").is_file()


def test_generated_files_cannot_introduce_binary_content(source):
    with pytest.raises(ValueError, match="Binary"):
        render_edit_repair(proposal("debian/new-file", "", "binary\0payload\n"), source)
