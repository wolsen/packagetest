#!/usr/bin/env python3
"""Require purpose docstrings for all production definitions, including private helpers."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def missing_docstrings(source: str, filename: str) -> list[str]:
    """Find undocumented modules, classes, methods, and nested or private functions."""
    definitions = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return [
        f'{filename}:{getattr(node, "lineno", 1)}: '
        f'{getattr(node, "name", "<module>")} needs a purpose docstring'
        for node in ast.walk(ast.parse(source, filename=filename))
        if isinstance(node, definitions) and not ast.get_docstring(node)
    ]


def documentation_errors(root: Path) -> list[str]:
    """Check source modules and executable Python scripts without exempting internal names."""
    return [
        error
        for directory in ('src', 'scripts')
        for path in sorted((root / directory).rglob('*.py'))
        for error in missing_docstrings(path.read_text(), str(path.relative_to(root)))
    ]


def main() -> int:
    """Print missing documentation locations and fail if any definition lacks a docstring."""
    errors = documentation_errors(ROOT)
    for error in errors:
        print(error)

    print(f'Checked production docstrings; {len(errors)} missing.')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
