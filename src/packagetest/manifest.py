from __future__ import annotations

import json
from pathlib import Path

from .models import GenerationManifest


def write_generation_manifest(path: Path, manifest: GenerationManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
