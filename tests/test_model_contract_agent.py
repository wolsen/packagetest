"""Verify the real-model contract harness without requiring model weights locally."""

import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

from packagetest import remediation


@pytest.fixture
def contract():
    path = Path(__file__).resolve().parents[1] / "scripts/model-contract-test.py"
    spec = importlib.util.spec_from_file_location("model_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.packaging_component
def test_contract_exercises_inspection_quilt_and_independent_invariant(tmp_path, monkeypatch, contract):
    evidence = "supported(2026) returned False"
    responses = iter([
        {"action": "inspect", "requests": [{"op": "read", "path": "compatibility.py"}]},
        {"action": "propose_patch", "diagnosis": "Accept supported versions above the minimum.", "evidence": evidence,
         "edits": [{"path": "compatibility.py", "before": "return version == MINIMUM", "after": "return version >= MINIMUM"}]},
    ])
    prompts = []

    def generate(*args):
        prompts.append(args[2])
        return json.dumps(next(responses)), {}

    monkeypatch.setattr(remediation, "llama_generate", generate)
    result = contract._run_investigation_case(Namespace(output=tmp_path, llama_cli=tmp_path / "llama", model=tmp_path / "model"))
    assert result["result"] == "PASS"
    assert result["model_calls"] == 2
    assert "return version == MINIMUM" in prompts[1]
    assert (tmp_path / "general-source-investigation/turn-2/patch-validation.json").is_file()


def test_contract_invariant_rejects_a_passing_constant_or_lowered_floor(tmp_path, contract):
    tree = tmp_path / "source"
    contract._investigation_fixture(tree)
    path = tree / "compatibility.py"
    original = path.read_text()
    path.write_text(original.replace("return version == MINIMUM", "return True"))
    assert not contract._minimum_contract(tree)
    path.write_text(original.replace("MINIMUM = 2024", "MINIMUM = 2023").replace("== MINIMUM", ">= MINIMUM"))
    assert not contract._minimum_contract(tree)
