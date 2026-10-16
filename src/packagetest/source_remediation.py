"""Repair packaging drift that prevents creation of an immutable source package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from packagetest.failure_analysis import (
    QuiltPatchSuperseded,
    normalize_repair_decision,
    parse_repair_decision,
    validate_repair_decision,
    validate_source_patch,
)


def failed_tree(work: Path, source: str) -> Path:
    """Locate the single failed package tree from its Debian control file."""
    matches = [path.parent.parent for path in work.glob(f"{source}-*/debian/control")]
    if len(matches) != 1:
        raise ValueError(
            f"expected one failed prepared source tree, found {len(matches)}"
        )
    return matches[0]


def _quilt_log_evidence(logs: Path) -> list[str]:
    """Read only command logs that contain source-package or patch failures."""
    blocks = []
    for path in sorted(logs.glob("*.log")):
        text = path.read_text(errors="replace")
        if any(
            marker in text
            for marker in ("Hunk #", ".patch", "dpkg-source: error", "does not apply")
        ):
            blocks.append(f"--- {path.name} ---\n{text}")

    return blocks


def failure_evidence(work: Path, output: Path, limit: int = 20_000) -> str:
    """Combine result records with relevant logs, retaining the newest bounded evidence."""
    blocks = [
        path.read_text(errors="replace")
        for path in (work / "resolution.json", output / "result.json")
        if path.is_file()
    ]
    logs = work / "logs"
    if logs.is_dir():
        blocks.extend(_quilt_log_evidence(logs))

    return "\n\n".join(blocks)[-limit:]


def write_json(path: Path, value: object) -> None:
    """Persist a human-readable remediation record with a trailing newline."""
    path.write_text(json.dumps(value, indent=2) + "\n")


def _active_series(tree: Path) -> list[str]:
    """Read active patch names, retaining duplicate entries for the retry budget."""
    series = tree / "debian/patches/series"
    if not series.is_file():
        return []

    return [
        line.split()[0]
        for line in series.read_text().splitlines()
        if line.split() and not line.lstrip().startswith("#")
    ]


def _selected_patches(names: list[str], active: set[str]) -> list[str]:
    """Filter evidence names against the series, preserving their first occurrence."""
    return list(dict.fromkeys(name for name in names if name in active))


def _preceding_applications(evidence: str) -> list[str]:
    """Associate older hunk-error logs with their nearest preceding patch application."""
    names = []
    for failure in re.finditer(r"Hunk #\d+ FAILED|does not apply", evidence, re.I):
        applications = re.findall(
            r"applying\s+([A-Za-z0-9][A-Za-z0-9_.+~-]*[.]patch)",
            evidence[max(0, failure.start() - 2000) : failure.start()],
            re.I,
        )
        if applications:
            names.append(applications[-1])

    return names


def failed_quilt_patches(tree: Path, evidence: str) -> list[str]:
    """Prefer final dpkg-source errors, then explicit paths, then nearby hunk failures."""
    active = set(_active_series(tree))
    # Final command errors identify the failing patch more reliably than progress logs.
    command_failures = re.findall(
        r"(?m)^dpkg-source: error:.*(?:debian/patches/|[.]pc/)([A-Za-z0-9][A-Za-z0-9_.+~-]*[.]patch)",
        evidence,
    )
    selected = _selected_patches(list(reversed(command_failures)), active)
    if selected:
        return selected

    named = re.findall(
        r"(?:debian/patches/|[.]pc/)([A-Za-z0-9][A-Za-z0-9_.+~-]*[.]patch)", evidence
    )
    selected = _selected_patches(named, active)
    if selected:
        return selected

    return _selected_patches(_preceding_applications(evidence), active)


def _arguments() -> argparse.Namespace:
    """Parse source repair inputs, output locations, and inference limits."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--initial-work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--llama-cli", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--model-timeout", type=int, default=180)
    parser.add_argument("--max-model-calls", type=int, default=2)
    return parser.parse_args()


class SourceRepairSession:
    """Own cumulative proposals, retry evidence, and selection of a prepared source."""

    def __init__(self, args: argparse.Namespace) -> None:
        """Capture the original tree and evidence before any repair attempts run."""
        self.args = args
        self.tree = failed_tree(args.initial_work, args.source)
        self.current_tree = self.tree
        self.evidence = failure_evidence(args.initial_work, args.output)
        (args.report / "failure-evidence.txt").write_text(self.evidence + "\n")
        self.decisions: list[dict] = []
        self.attempts: list[dict] = []
        self.selected: str | int | None = None
        self.feedback = ""
        self.deterministic_seen: set[tuple[str, str]] = set()

    def _next_subject(self) -> str | None:
        """Select the first failing patch unless the same evidence has already been tried."""
        patches = failed_quilt_patches(self.current_tree, self.evidence)
        if not patches:
            return None

        signature = (patches[0], hashlib.sha256(self.evidence.encode()).hexdigest())
        if signature in self.deterministic_seen:
            return None

        self.deterministic_seen.add(signature)
        return patches[0]

    def _render_deterministic(self, subject: str, record: dict) -> str:
        """Refresh cumulative patches, converting proven supersession to a series edit."""
        from packagetest import remediation as helper

        self.decisions.append(record["decision"])
        try:
            return helper.render_cumulative_repair(self.decisions, self.tree)
        except QuiltPatchSuperseded as exc:
            # The renderer's explicit signal permits a drop; a generic error does not.
            decision = {
                "action": "drop_quilt_patch",
                "subject": subject,
                "replacement": "",
                "evidence": str(exc),
            }
            self.decisions[-1] = decision
            record.update(
                decision=decision,
                classification="superseded-upstream",
                classification_evidence=str(exc),
            )
            return helper.render_cumulative_repair(self.decisions, self.tree)

    def _validated_patch(self, attempt: Path, patch: str) -> tuple[Path, dict]:
        """Save and validate the cumulative proposal against the original source tree."""
        path = attempt / "proposal.patch"
        path.write_text(patch)
        validation = validate_source_patch(patch, self.tree)
        write_json(attempt / "patch-validation.json", validation)
        if validation["result"] != "APPLIES":
            raise ValueError(validation["error"])

        return path, validation

    def _prepare(
        self, attempt: Path, patch: str, record: dict
    ) -> tuple[Path, Path, Path]:
        """Run fresh source preparation and record its status and patch validation."""
        patch_path, validation = self._validated_patch(attempt, patch)
        args = self.args
        work = args.work / f"{attempt.name}-work"
        output = args.work / f"{attempt.name}-source"
        log = attempt / "source-preparation.log"
        command = [
            sys.executable,
            "-m",
            "packagetest.prepared_source",
            "--catalog",
            str(args.catalog),
            "--source",
            args.source,
            "--work",
            str(work),
            "--output",
            str(output),
            "--run-id",
            args.run_id,
            "--run-attempt",
            args.run_attempt,
            "--remediation-patch",
            str(patch_path),
        ]
        with log.open("w") as stream:
            completed = subprocess.run(
                command,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                env={**os.environ, "PYTHONPATH": "src"},
                timeout=3600,
            )

        record.update(
            result="REPAIRED" if completed.returncode == 0 else "PREPARATION_FAILED",
            patch_validation=validation,
            preparation_returncode=completed.returncode,
        )
        return work, output, log

    def _accept_candidate(self, output: Path, record: dict) -> bool:
        """Promote only successful preparation that produced the immutable source manifest."""
        if (
            record["preparation_returncode"] != 0
            or not (output / "prepared-source.json").is_file()
        ):
            return False

        self.selected = record["number"]
        shutil.rmtree(self.args.output)
        shutil.move(str(output), self.args.output)
        return True

    def _retry_evidence(
        self, work: Path, output: Path, log: Path, record: dict
    ) -> None:
        """Retain failed preparation evidence and its attempt record for the next retry."""
        self.evidence = (
            failure_evidence(work, output)
            + "\n"
            + log.read_text(errors="replace")[-8000:]
        )
        self.feedback = json.dumps(record, indent=2)

    def _retry_tree(self, work: Path) -> None:
        """Use the latest failed tree for patch selection, falling back to the original."""
        try:
            self.current_tree = failed_tree(work, self.args.source)
        except ValueError:
            self.current_tree = self.tree

    def _record_error(self, record: dict, error: Exception) -> None:
        """Keep attempt errors visible both in the final report and subsequent prompts."""
        record["error"] = str(error)
        self.attempts.append(record)
        self.feedback = json.dumps(record, indent=2)

    def _deterministic_attempt(self, number: int, subject: str) -> bool:
        """Try a deterministic proposal, stopping on success or a rendering/setup error."""
        attempt = self.args.report / f"attempt-d{number}"
        attempt.mkdir()
        decision = {
            "action": "refresh_quilt_patch",
            "subject": subject,
            "replacement": "",
            "evidence": "deterministic failed-patch classification",
        }
        record = {
            "number": f"D{number}",
            "result": "DETERMINISTIC_ERROR",
            "decision": decision,
            "inference": {"backend": "deterministic-quilt-refresh"},
        }
        decision_count = len(self.decisions)
        try:
            patch = self._render_deterministic(subject, record)
            work, output, log = self._prepare(attempt, patch, record)
            self.attempts.append(record)
            if self._accept_candidate(output, record):
                return True

            self._retry_evidence(work, output, log, record)
            self._retry_tree(work)
            return False
        except Exception as exc:
            del self.decisions[decision_count:]
            self._record_error(record, exc)
            return True

    def deterministic_repairs(self) -> None:
        """Bound deterministic retries by the initial active series and repeated evidence."""
        for number in range(1, len(_active_series(self.current_tree)) + 1):
            subject = self._next_subject()
            if subject is None:
                break

            if self._deterministic_attempt(number, subject):
                break

    def _model_decision(
        self, attempt: Path, number: int, prompt: str
    ) -> tuple[dict, dict]:
        """Record inference output and require an accepted normalized repair decision."""
        from packagetest import remediation as helper

        output, inference = helper.llama_generate(
            self.args.llama_cli,
            self.args.model,
            prompt,
            number,
            self.args.model_timeout,
        )
        (attempt / "model-output.txt").write_text(output)
        decision = normalize_repair_decision(
            parse_repair_decision(output), self.tree, self.evidence
        )
        write_json(attempt / "decision.json", decision)
        validation = validate_repair_decision(decision, self.evidence)
        write_json(attempt / "decision-validation.json", validation)
        if validation["result"] != "ACCEPTED":
            raise ValueError(validation["error"])

        return decision, inference

    def _model_patch(self, decision: dict) -> str:
        """Restrict source preparation repairs to quilt refreshes and explicit drops."""
        from packagetest import remediation as helper

        if decision["action"] not in {"refresh_quilt_patch", "drop_quilt_patch"}:
            raise ValueError(
                "source preparation currently accepts only quilt refresh or drop decisions"
            )

        self.decisions.append(decision)
        return helper.render_cumulative_repair(self.decisions, self.tree)

    def _model_attempt(self, number: int) -> bool:
        """Try one model proposal, stopping on an explicit no-fix decision or success."""
        from packagetest import remediation as helper

        attempt = self.args.report / f"attempt-{number}"
        attempt.mkdir()
        prompt = helper.prompt(
            self.args.source,
            "source-preparation",
            self.evidence,
            helper.source_context(self.tree, self.evidence),
            self.feedback,
        )
        (attempt / "prompt.txt").write_text(prompt)
        record = {"number": number, "result": "MODEL_ERROR"}
        try:
            decision, inference = self._model_decision(attempt, number, prompt)
            if decision["action"] == "no_fix":
                record.update(result="NO_FIX", decision=decision, inference=inference)
                self.attempts.append(record)
                return True

            patch = self._model_patch(decision)
            work, output, log = self._prepare(attempt, patch, record)
            record.update(decision=decision, inference=inference)
            self.attempts.append(record)
            if self._accept_candidate(output, record):
                return True

            self._retry_evidence(work, output, log, record)
        except Exception as exc:
            self._record_error(record, exc)

        return False

    def model_repairs(self) -> None:
        """Use the inference budget only when deterministic preparation was unresolved."""
        if self.selected is not None:
            return

        for number in range(1, self.args.max_model_calls + 1):
            if self._model_attempt(number):
                break

    def report(self) -> dict:
        """Describe the selected attempt and preserve model and CI provenance."""
        from packagetest import remediation as helper

        args = self.args
        return {
            "schema_version": 1,
            "source": args.source,
            "result": "REPAIRED" if self.selected is not None else "UNRESOLVED",
            "selected_attempt": self.selected,
            "attempts": self.attempts,
            "model": {"name": helper.MODEL_NAME, "sha256": args.model_sha256},
            "ci": {
                "run_id": args.run_id,
                "run_attempt": args.run_attempt,
                "checkout_sha": os.environ.get("GITHUB_SHA"),
            },
            "finished_at": datetime.now(UTC).isoformat(),
        }


def _write_summary(directory: Path, report: dict) -> None:
    """Publish a concise attempt table, including it in the workflow summary if present."""
    summary = [
        f"## {report['source']} source-preparation remediation: {report['result']}",
        "",
        "| Attempt | Decision | Validation | Preparation | Result |",
        "|---:|---|---|---|---|",
    ]
    for item in report["attempts"]:
        summary.append(
            f"| {item['number']} | {item.get('decision', {}).get('action', '—')} | "
            f"{item.get('patch_validation', {}).get('result', '—')} | "
            f"{item.get('preparation_returncode', '—')} | {item['result']} |"
        )

    summary.extend(["", "Any packaging change is a proposal requiring human review."])
    path = directory / "summary.md"
    path.write_text("\n".join(summary) + "\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(path.read_text())


def _publish_report(args: argparse.Namespace, report: dict) -> None:
    """Persist remediation evidence alongside the prepared source output."""
    write_json(args.report / "result.json", report)
    _write_summary(args.report, report)
    destination = args.output / "ai-source-remediation"
    if destination.exists():
        shutil.rmtree(destination)

    shutil.copytree(args.report, destination)


def main() -> int:
    """Retry failed source preparation with validated quilt repair proposals."""
    from packagetest import remediation as helper

    args = _arguments()
    args.report.mkdir(parents=True, exist_ok=True)
    if helper.sha256(args.model) != args.model_sha256:
        raise SystemExit("local model checksum mismatch")

    session = SourceRepairSession(args)
    session.deterministic_repairs()
    session.model_repairs()
    _publish_report(args, session.report())
    return 0 if session.selected is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
