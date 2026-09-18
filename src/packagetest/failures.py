from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import CommandResult


@dataclass(frozen=True)
class FailureBundle:
    category: str
    source_package: str
    generation_id: str
    upstream_sha: str | None
    packaging_sha: str | None
    failed_command: list[str]
    command_exit_code: int


def write_failure_bundle(
    *,
    out_dir: Path,
    bundle: FailureBundle,
    command_result: CommandResult,
    files: dict[str, str],
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    failure_json = out_dir / "failure.json"
    failure_json.write_text(
        json.dumps(
            {
                **asdict(bundle),
                "command": command_result.to_dict(),
                "files": files,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    analysis_md = out_dir / "analysis.md"
    analysis_md.write_text(
        "\n".join(
            [
                f"# Failure analysis placeholder: {bundle.source_package}",
                "",
                f"- Category: `{bundle.category}`",
                f"- Failed command: `{' '.join(bundle.failed_command)}`",
                f"- Exit code: `{bundle.command_exit_code}`",
                "",
                "This file is intentionally a human/AI review boundary.",
                "No automatic patch application is performed.",
            ]
        ),
        encoding="utf-8",
    )

    (out_dir / "proposed-fix.patch").write_text("", encoding="utf-8")
    return failure_json
