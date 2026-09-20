"""Exercise the actual assertion shipped by the SDK packaging patch."""
import ast
import datetime
from pathlib import Path
import textwrap
from types import SimpleNamespace
import unittest

import pytest


def patched_check():
    patch = Path('config/patches/python-openstacksdk/allow-newer-service-type-catalog.patch').read_text()
    # Reconstruct the patch's post-image hunk context, then extract its actual
    # changed method. This checks the shipped assertion rather than mirroring it.
    lines = [line[1:] for line in patch.splitlines() if line[:1] in {'+', ' '} and not line.startswith('+++')]
    begin = next(i for i, line in enumerate(lines) if line.startswith('    def test_ost_version('))
    method = [lines[begin]]
    for line in lines[begin + 1:]:
        if line.strip() and not line.startswith('        '):
            break
        method.append(line)
    module = ast.parse(textwrap.dedent('\n'.join(method)))
    namespace = {'datetime': datetime}
    exec(compile(module, '<actual SDK catalog patch>', 'exec'), namespace)
    return namespace


@pytest.mark.parametrize('version', ['2024-05-08T19:22:13.804707', '2026-08-31T19:18:31.000000'])
def test_catalog_patch_accepts_supported_and_newer_catalog(version):
    scope = patched_check()
    scope['os_service_types'] = SimpleNamespace(ServiceTypes=lambda: SimpleNamespace(version=version))
    scope['test_ost_version'](unittest.TestCase())


def test_catalog_patch_rejects_stale_catalog():
    scope = patched_check()
    scope['os_service_types'] = SimpleNamespace(ServiceTypes=lambda: SimpleNamespace(version='2024-05-07T19:22:13.804707'))
    with pytest.raises(AssertionError, match='older than the supported'):
        scope['test_ost_version'](unittest.TestCase())


def test_catalog_patch_rejects_malformed_timestamp():
    scope = patched_check()
    scope['os_service_types'] = SimpleNamespace(ServiceTypes=lambda: SimpleNamespace(version='not-a-timestamp'))
    with pytest.raises(ValueError):
        scope['test_ost_version'](unittest.TestCase())
