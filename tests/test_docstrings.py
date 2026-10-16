"""Verify documentation gates include definitions omitted by public-only lint rules."""
import runpy
from pathlib import Path


CHECKER = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/check-docstrings.py'))


def test_docstring_gate_checks_private_nested_async_and_constructor_definitions():
    """Private names and nested definitions receive the same gate as public APIs."""
    source = '''
class _Owner:
    def __init__(self):
        pass

    async def _consume(self):
        def nested():
            pass
'''
    errors = CHECKER['missing_docstrings'](source, 'example.py')
    assert len(errors) == 5
    for name in ('<module>', '_Owner', '__init__', '_consume', 'nested'):
        assert any(f'{name} needs a purpose docstring' in error for error in errors)


def test_docstring_gate_accepts_documented_definitions_and_rejects_empty_strings():
    """A real docstring is required; comments and empty string statements do not suffice."""
    source = '''"""Explain the module's goal."""
# A comment does not document the following function.
def _undocumented():
    ""
    pass

def documented():
    """Explain why this function exists."""
    return None
'''
    errors = CHECKER['missing_docstrings'](source, 'example.py')
    assert errors == ['example.py:3: _undocumented needs a purpose docstring']
