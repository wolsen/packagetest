"""Preserve test meaning, dependency constraints, and checks during automatic repair.

The policy deliberately rejects changes it cannot compare conservatively. It is
an admission boundary, not a proof that arbitrary Python or shell is equivalent.
"""

from __future__ import annotations

import ast
import configparser
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from .prepared_plan import RelationSplitter
from .relations import DependencyRelation, is_deferred_relation, parse_relation
from .versioning import debian_compare

DEBIAN_DEPENDENCIES = {
    "build-depends",
    "build-depends-indep",
    "build-depends-arch",
    "depends",
    "pre-depends",
    "recommends",
    "suggests",
    "enhances",
}
IDENTITY_FIELDS = {
    "source",
    "package",
    "maintainer",
    "uploaders",
    "xsbc-original-maintainer",
}
CONTROL_CHECK_FIELDS = {
    "testsuite",
    "testsuite-triggers",
    "build-profiles",
    "rules-requires-root",
}
CHECK_COMMAND = re.compile(
    r"\b(?:dh(?:_auto_test|_missing|_shlibdeps|_gencontrol)?|pytest|py\.test|stestr|testr|lintian|nosetests|unittest|tox)\b"
)
BYPASS = re.compile(
    r"(?:\|\|\s*(?:true\b|:|exit\s+0\b)|\bset\s+\+e\b|\bexit\s+0\b|"
    r"\b(?:nocheck|notest|noautopkgtest)\b|PYTHONOPTIMIZE|PYBUILD_DISABLE|"
    r"--(?:no-check|nocheck|no-lintian|skip-tests|ignore-errors)\b|^\s*\.IGNORE\s*:|^\t\s*-)"
)
TEST_CONFIG_NAMES = {
    "tox.ini",
    "pytest.ini",
    ".coveragerc",
    ".stestr.conf",
    ".testr.conf",
}


def _fingerprint(node: ast.AST) -> str:
    """Compare syntax without line numbers or formatting differences."""
    return ast.dump(node, include_attributes=False)


def _is_docstring(node: ast.AST) -> bool:
    """Allow documentation-only changes without weakening executable statements."""
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _statements(nodes: Sequence[ast.stmt]) -> list[ast.stmt]:
    """Exclude a leading docstring while retaining every executable statement."""
    return list(nodes[1:] if nodes and _is_docstring(nodes[0]) else nodes)


def _is_test(node: ast.AST) -> bool:
    """Recognize synchronous and asynchronous discovered Python test functions."""
    return isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ) and node.name.startswith("test")


def _assertion(node: ast.AST) -> ast.AST | None:
    """Extract a Python assertion or unittest/mock assertion call."""
    if isinstance(node, ast.Assert):
        return node.test

    value = node.value if isinstance(node, ast.Expr) else node
    return value if _assertion_call(value) else None


def _assertion_call(node: ast.AST) -> bool:
    """Recognize unittest and mock assertion methods, including context managers."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("assert")
    )


def _vacuous(node: ast.AST) -> bool:
    """Reject literal-only and self-comparison assertions used as replacement coverage."""
    if isinstance(node, ast.Constant):
        return True

    if isinstance(node, ast.Compare):
        return _vacuous_comparison(node)

    return _vacuous_call(node) or _vacuous_boolean(node)


def _vacuous_comparison(node: ast.Compare) -> bool:
    """Reject comparisons of constants and syntactically repeated operands."""
    return _literal_arguments([node.left, *node.comparators]) or all(
        _fingerprint(value) == _fingerprint(node.left) for value in node.comparators
    )


def _vacuous_boolean(node: ast.AST) -> bool:
    """Reject Boolean branches with literal escape hatches such as condition or True."""
    return isinstance(node, ast.BoolOp) and any(
        isinstance(value, ast.Constant) for value in node.values
    )


def _vacuous_call(node: ast.AST) -> bool:
    """Recognize unittest assertions over constants or the same expression twice."""
    if not isinstance(node, ast.Call) or not node.args:
        return False

    if _literal_arguments(node.args):
        return True

    return _same_assertion_operands(node.args)


def _literal_arguments(args: Sequence[ast.expr]) -> bool:
    """Identify assertions whose operands contain no runtime observations."""
    return all(isinstance(value, ast.Constant) for value in args)


def _same_assertion_operands(args: Sequence[ast.expr]) -> bool:
    """Recognize repeated operands in binary unittest assertions."""
    return len(args) > 1 and _fingerprint(args[0]) == _fingerprint(args[1])


def _validate_added_assertion(node: ast.AST) -> None:
    """Admit additional checks, rejecting vacuous assertions and executable replacements."""
    predicate = _assertion(node)
    if predicate is None or _vacuous(predicate):
        raise ValueError(
            "Existing tests may only gain nonvacuous assertions; changed test meaning requires review"
        )


def _function_shell(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Compare decorators, defaults, fixture arguments, and annotations independently of the body."""
    import copy

    shell = copy.deepcopy(node)
    shell.body = []
    return _fingerprint(shell)


def _preserve_test(old: ast.FunctionDef | ast.AsyncFunctionDef, new: ast.stmt) -> None:
    """Keep fixture inputs and the complete original control flow, allowing extra assertions."""
    if not isinstance(new, (ast.FunctionDef, ast.AsyncFunctionDef)) or _function_shell(
        old
    ) != _function_shell(new):
        raise ValueError(
            "Test decorators, fixture arguments, and defaults cannot change automatically"
        )

    _preserve_sequence(
        _statements(old.body), _statements(new.body), _validate_added_assertion
    )


def _preserve_sequence(
    old: Sequence[ast.stmt], new: Sequence[ast.stmt], added: Callable[[ast.stmt], None]
) -> None:
    """Keep original statement order and validate every inserted statement independently."""
    remaining = iter(old)
    expected = next(remaining, None)
    for node in new:
        if expected is not None and _fingerprint(node) == _fingerprint(expected):
            expected = next(remaining, None)
        else:
            added(node)

    if expected is not None:
        raise ValueError(
            "A repair cannot remove or change existing test assertions, setup, or control flow; review required"
        )


def _validate_new_test(node: ast.stmt) -> None:
    """Allow executable regression functions without import-time hooks or vacuous coverage."""
    function = _regression_function(node)
    _validate_regression_shell(function)
    predicates = _test_predicates(function)
    if not predicates or any(_vacuous(value) for value in predicates):
        raise ValueError("A new regression requires nonvacuous assertions")


def _regression_function(node: ast.stmt) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Require an added declaration to be a discovered test, rather than a replacement helper."""
    if not isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ) or not node.name.startswith("test"):
        raise ValueError(
            "Existing test module helpers, imports, and constants must be preserved; add regression functions only"
        )

    return node


def _validate_regression_shell(node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    """Reject import-time hooks in new test declarations."""
    defaults = (*node.args.defaults, *node.args.kw_defaults)
    if node.decorator_list or any(value is not None for value in defaults):
        raise ValueError(
            "New regression functions cannot introduce decorators or evaluated defaults"
        )


def _test_predicates(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Require regression checks in the test body, rather than uncalled nested definitions."""
    return [
        predicate
        for statement in node.body
        for predicate in _direct_predicates(statement)
    ]


def _direct_predicates(node: ast.stmt) -> list[ast.AST]:
    """Include assertion statements and unconditional unittest assertion contexts."""
    predicate = _assertion(node)
    if predicate is not None:
        return [predicate]

    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [
            item.context_expr
            for item in node.items
            if _assertion_call(item.context_expr)
        ]

    return []


def _preserve_scope(old: Sequence[ast.stmt], new: Sequence[ast.stmt]) -> None:
    """Match existing declarations by position, retaining executable module and class setup."""
    remaining = iter(_statements(old))
    bindings = _scope_names(old)
    expected = next(remaining, None)
    for node in _statements(new):
        if expected is not None and _same_declaration(expected, node):
            _preserve_declaration(expected, node)
            expected = next(remaining, None)
        else:
            _validate_scope_addition(node, bindings)

    if expected is not None:
        raise ValueError(
            "A repair cannot remove existing tests, classes, helpers, or fixture setup"
        )


def _scope_names(nodes: Sequence[ast.stmt]) -> set[str]:
    """Reserve existing definitions, imports, and assigned names against test shadowing."""
    return {name for node in nodes for name in _binding_names(node)}


def _binding_names(node: ast.stmt) -> list[str]:
    """Read direct scope bindings without reserving local variables in test functions."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]

    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [alias.asname or alias.name.split(".")[0] for alias in node.names]

    return _assigned_names(node)


def _assigned_names(node: ast.stmt) -> list[str]:
    """Reserve assignment targets that a new test function could otherwise overwrite."""
    if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return []

    return [
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
    ]


def _validate_scope_addition(node: ast.stmt, bindings: set[str]) -> None:
    """Reject replacement definitions even when the original declaration remains in the file."""
    function = _regression_function(node)
    if function.name in bindings:
        raise ValueError(
            "A new test cannot shadow an existing definition, import, or fixture binding"
        )

    _validate_new_test(node)
    bindings.add(function.name)


def _same_declaration(old: ast.stmt, new: ast.stmt) -> bool:
    """Pair named definitions without conflating tests in different classes."""
    if isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return type(old) is type(new) and old.name == getattr(new, "name", None)

    return _fingerprint(old) == _fingerprint(new)


def _preserve_declaration(old: ast.stmt, new: ast.stmt) -> None:
    """Compare class setup or test bodies while freezing all other existing declarations."""
    if isinstance(old, ast.ClassDef):
        _preserve_class_pair(old, new)
        return

    if _is_test(old):
        _preserve_test(_regression_function(old), new)
        return

    if _fingerprint(old) != _fingerprint(new):
        raise ValueError(
            "Existing test helpers and fixture setup cannot change automatically"
        )


def _preserve_class_pair(old: ast.ClassDef, new: ast.stmt) -> None:
    """Check the declaration shape before comparing class contracts."""
    if not isinstance(new, ast.ClassDef):
        raise ValueError("Existing test classes must remain classes")

    _preserve_class(old, new)


def _preserve_class(old: ast.ClassDef, new: ast.ClassDef) -> None:
    """Retain test class inheritance and decorators while admitting new regression methods."""
    import copy

    old_shell, new_shell = copy.deepcopy(old), copy.deepcopy(new)
    old_shell.body, new_shell.body = [], []
    if _fingerprint(old_shell) != _fingerprint(new_shell):
        raise ValueError(
            "Test class inheritance and decorators cannot change automatically"
        )

    _preserve_scope(old.body, new.body)


def _python_test_file(name: str) -> bool:
    """Include helpers named conftest and Python autopkgtest scripts in test protection."""
    path = PurePosixPath(name)
    return path.suffix == ".py" and _test_location(path)


def _test_location(path: PurePosixPath) -> bool:
    """Recognize standard test modules and test-directory helpers without matching latest.py."""
    names = {"test.py", "conftest.py"}
    location = bool(set(path.parts[:-1]) & {"test", "tests", "testing"})
    return (
        location
        or path.name in names
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    )


def _validate_tests(name: str, before: str, after: str) -> None:
    """Preserve existing test execution rather than comparing assertion counts alone."""
    if not _python_test_file(name):
        return

    old, new = ast.parse(before), ast.parse(after)
    if before:
        _preserve_scope(old.body, new.body)

    _validate_collection_hook(name, old, new)

    _reject_test_bypasses(new, old)
    if not before:
        _validate_new_test_module(new)


def _validate_collection_hook(name: str, old: ast.Module, new: ast.Module) -> None:
    """Keep pytest collection hooks and shared fixture configuration immutable."""
    if PurePosixPath(name).name == "conftest.py" and _fingerprint(old) != _fingerprint(
        new
    ):
        raise ValueError("Test collection hooks and shared fixtures require review")


def _validate_new_test_module(module: ast.Module) -> None:
    """Check newly created test functions and reject duplicate definitions in each scope."""
    for node in ast.walk(module):
        if _is_test(node):
            assert isinstance(node, ast.stmt)
            _validate_new_test(node)

    _unique_definitions(module.body)


def _unique_definitions(nodes: Sequence[ast.stmt]) -> None:
    """Prevent later definitions from replacing tests discovered earlier in the same scope."""
    names: set[str] = set()
    for node in nodes:
        _check_definition(node, names)


def _check_definition(node: ast.stmt, names: set[str]) -> None:
    """Retain unique named definitions and recursively check class scopes."""
    bindings = set(_binding_names(node))
    if names & bindings:
        raise ValueError(
            "Duplicate test, class, import, or assignment binding requires review"
        )

    names.update(bindings)
    if isinstance(node, ast.ClassDef):
        _unique_definitions(node.body)


def _reject_test_bypasses(new: ast.Module, old: ast.Module) -> None:
    """Reject newly introduced dynamic skip/failure suppression and early-return test bodies."""
    previous = Counter(_fingerprint(node) for node in ast.walk(old))
    for node in ast.walk(new):
        key = _fingerprint(node)
        if previous[key]:
            previous[key] -= 1
            continue

        if _bypassing_node(node):
            raise ValueError(
                "A repair cannot add test skipping, exception suppression, or early returns"
            )


def _bypassing_node(node: ast.AST) -> bool:
    """Recognize common executable ways to avoid reaching or propagating assertions."""
    if isinstance(node, (ast.Return, ast.Try, ast.TryStar, ast.Global, ast.Nonlocal)):
        return True

    if isinstance(node, ast.Call):
        name = ast.unparse(node.func).split(".")[-1]
        return name in {
            "skip",
            "skipIf",
            "skipUnless",
            "skipTest",
            "importorskip",
            "xfail",
            "exit",
            "_exit",
        }

    return _test_collection_assignment(node)


def _test_collection_assignment(node: ast.AST) -> bool:
    """Reject new module settings that disable discovery or install collection hooks."""
    if not isinstance(node, ast.stmt):
        return False

    protected = {
        "__test__",
        "collect_ignore",
        "collect_ignore_glob",
        "pytestmark",
        "pytest_plugins",
    }
    return any(name in protected for name in _assigned_names(node))


def _split(value: str, separator: str) -> list[str]:
    """Reuse the target-independent Debian relation tokenizer without evaluating restrictions."""
    splitter = RelationSplitter(separator)
    for character in value:
        splitter.consume(character)

    splitter.finish_item()
    return splitter.result


def _relation_implies(new: str, old: str) -> bool:
    """Require the same provider and activation scope with an equal or tighter version range."""
    if is_deferred_relation(old) or is_deferred_relation(new):
        return new == old

    left, right = parse_relation(new), parse_relation(old)
    return _relation_scope(left) == _relation_scope(right) and _version_implies(
        left, right
    )


def _relation_scope(
    relation: DependencyRelation,
) -> tuple[str, str | None, tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Keep architecture, profile, and multiarch activation conservative across all targets."""
    return (
        relation.package,
        relation.qualifier,
        relation.architectures,
        relation.profile_groups,
    )


def _version_implies(new: DependencyRelation, old: DependencyRelation) -> bool:
    """Compare Debian range endpoints without treating versions as lexical strings."""
    if old.operator is None:
        return True

    if new.operator is None or new.version is None or old.version is None:
        return False

    order = debian_compare(new.version, old.version)
    return _operator_implies(new.operator, old.operator, order)


def _operator_implies(new: str, old: str, order: int) -> bool:
    """Admit exact pins and strictly narrower lower or upper bounds."""
    exact = {
        "=": order == 0,
        ">=": order >= 0,
        ">>": order > 0,
        "<=": order <= 0,
        "<<": order < 0,
    }
    if new == "=":
        return exact[old]

    ranges = {
        ">=": {">=", ">>"},
        ">>": {">=", ">>"},
        "<=": {"<=", "<<"},
        "<<": {"<=", "<<"},
    }
    if new not in ranges.get(old, set()):
        return False

    stricter = order > 0 if old.startswith(">") else order < 0
    return stricter or _same_endpoint(new, old, order)


def _same_endpoint(new: str, old: str, order: int) -> bool:
    """Keep strict endpoints strict when both versions compare equal."""
    return order == 0 and (old in {">=", "<="} or new == old)


def _group_implies(new: str, old: str) -> bool:
    """Every alternative in the new group must satisfy an original alternative."""
    alternatives = _split(old, "|")
    return all(
        any(_relation_implies(item, baseline) for baseline in alternatives)
        for item in _split(new, "|")
    )


def _preserve_debian_dependencies(before: str, after: str) -> None:
    """Require each original dependency clause in the same stanza and field."""
    candidates = _split(after, ",")
    for group in _split(before, ","):
        if not any(_group_implies(candidate, group) for candidate in candidates):
            raise ValueError(
                f"Dependency removal, alternative expansion, restriction change, or relaxed version requires review: {group}"
            )


def _paragraphs(text: str) -> list[dict[str, str]]:
    """Parse control stanzas with case-insensitive unique fields and continuations."""
    paragraphs: list[dict[str, str]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        if block.strip():
            fields = _control_fields(block)
            if fields:
                paragraphs.append(fields)

    return paragraphs


def _control_fields(block: str) -> dict[str, str]:
    """Reject ambiguous control fields rather than silently losing an earlier constraint."""
    fields: dict[str, str] = {}
    current = ""
    for line in block.splitlines():
        if line.startswith("#"):
            continue

        current = _control_line(line, current, fields)

    return fields


def _control_line(line: str, current: str, fields: dict[str, str]) -> str:
    """Append continuations or introduce one unambiguous named control field."""
    if line[:1].isspace() and current:
        fields[current] += " " + line.strip()
        return current

    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9-]*):\s*(.*)", line)
    if match is None:
        raise ValueError("Unsupported control syntax requires review")

    key, value = match[1].lower(), match[2]
    if key in fields:
        raise ValueError(f"Duplicate control field: {key}")

    fields[key] = value
    return key


def _control_identity(record: Mapping[str, str]) -> tuple[str, str]:
    """Identify source, binary, and autopkgtest stanzas independently of their ordering."""
    for key in ("source", "package", "tests", "test-command"):
        if key in record:
            return key, record[key]

    return "", ""


def _control_records(text: str) -> dict[tuple[str, str], dict[str, str]]:
    """Reject duplicate stanza identities before matching existing package requirements."""
    records: dict[tuple[str, str], dict[str, str]] = {}
    for record in _paragraphs(text):
        key = _control_identity(record)
        if key in records:
            raise ValueError("Duplicate control stanza identity")

        records[key] = record

    return records


def _preserve_control(before: str, after: str, *, tests: bool = False) -> None:
    """Compare requirements within each existing package or autopkgtest stanza."""
    old, new = _control_records(before), _control_records(after)
    for identity, record in old.items():
        if identity not in new:
            raise ValueError(
                "Removing or renaming source, binary, or autopkgtest stanzas requires review"
            )

        _preserve_control_record(record, new[identity], tests=tests)


def _preserve_control_record(
    old: Mapping[str, str], new: Mapping[str, str], *, tests: bool
) -> None:
    """Keep dependency scopes and checking metadata while allowing unrelated packaging changes."""
    negative = {
        "build-conflicts",
        "build-conflicts-arch",
        "build-conflicts-indep",
        "conflicts",
        "breaks",
        "replaces",
    }
    protected = (
        set(old) | set(new)
        if tests
        else IDENTITY_FIELDS | CONTROL_CHECK_FIELDS | negative
    )
    for field in sorted(protected - DEBIAN_DEPENDENCIES):
        if old.get(field) != new.get(field):
            raise ValueError(
                f"Changing protected control field requires review: {field}"
            )

    for field in sorted(DEBIAN_DEPENDENCIES):
        _preserve_debian_dependencies(old.get(field, ""), new.get(field, ""))


def _requirement_lines(text: str) -> list[str]:
    """Ignore comments and whitespace while retaining opaque pip options and includes."""
    return [
        re.split(r"\s+#", line, maxsplit=1)[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _pep508_key(
    requirement: Requirement,
) -> tuple[str, frozenset[str], str, str | None]:
    """Keep environment activation, extras, and direct-source identity unchanged."""
    return (
        canonicalize_name(requirement.name),
        frozenset(requirement.extras),
        str(requirement.marker),
        requirement.url,
    )


def _pep508_implies(new: str, old: str) -> bool:
    """Keep all PEP 440 specifiers; added constraints may tighten the requirement."""
    try:
        left, right = Requirement(new), Requirement(old)
    except InvalidRequirement:
        return new == old

    return _pep508_key(left) == _pep508_key(right) and set(right.specifier) <= set(
        left.specifier
    )


def _preserve_python_requirements(before: Sequence[str], after: Sequence[str]) -> None:
    """Reject removals, marker/extras changes, and lost PEP 508 constraints."""
    _validate_added_requirements(before, after)
    for item in before:
        if not any(_pep508_implies(candidate, item) for candidate in after):
            raise ValueError(
                f"Python dependency relaxation or removal requires review: {item}"
            )


def _validate_added_requirements(before: Sequence[str], after: Sequence[str]) -> None:
    """Do not admit new pip options or recursive includes disguised as dependency additions."""
    for item in set(after) - set(before):
        try:
            Requirement(item)
        except InvalidRequirement as exc:
            raise ValueError(
                "New pip options, opaque requirements, or includes require review"
            ) from exc


def _ini(text: str) -> configparser.ConfigParser:
    """Parse configuration without interpolation, retaining duplicate-field diagnostics."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    return parser


def _test_section(name: str) -> bool:
    """Identify test discovery, runner, coverage, and lint sections in shared INI files."""
    return name.startswith(("testenv", "pytest", "coverage", "flake8")) or name in {
        "tox",
        "tool:pytest",
        "nosetests",
        "aliases",
    }


def _preserve_ini(name: str, before: str, after: str) -> None:
    """Freeze test and lint configuration while allowing unrelated setup metadata edits."""
    old, new = _ini(before), _ini(after)
    sections = set(old.sections()) | set(new.sections())
    for section in sections:
        if name in TEST_CONFIG_NAMES or _test_section(section):
            if _ini_section(old, section) != _ini_section(new, section):
                raise ValueError(
                    f"Changing test discovery, runner, or coverage configuration requires review: {section}"
                )

    _preserve_setup_requirements(old, new)


def _ini_section(parser: configparser.ConfigParser, name: str) -> dict[str, str] | None:
    """Include inherited DEFAULT values when comparing effective test settings."""
    return dict(parser[name]) if parser.has_section(name) else None


def _preserve_setup_requirements(
    old: configparser.ConfigParser, new: configparser.ConfigParser
) -> None:
    """Preserve setuptools dependency fields and every existing extras group."""
    for field in ("install_requires", "setup_requires", "tests_require"):
        _preserve_python_requirements(
            _requirement_lines(old.get("options", field, fallback="")),
            _requirement_lines(new.get("options", field, fallback="")),
        )

    _preserve_python_version(old, new)

    before, after = (
        _ini_section(old, "options.extras_require") or {},
        _ini_section(new, "options.extras_require") or {},
    )
    for field, value in before.items():
        _preserve_python_requirements(
            _requirement_lines(value), _requirement_lines(after.get(field, ""))
        )


def _preserve_python_version(
    old: configparser.ConfigParser, new: configparser.ConfigParser
) -> None:
    """Freeze interpreter eligibility independently of installed dependency constraints."""
    if old.get("options", "python_requires", fallback="") != new.get(
        "options", "python_requires", fallback=""
    ):
        raise ValueError("Changing the supported Python version requires review")


def _toml_table(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    """Require the expected mapping shape instead of treating malformed metadata as absent."""
    value = document.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"Expected TOML table: {key}")

    return value


def _toml_requirements(document: Mapping[str, object], key: str) -> list[str]:
    """Require a string-array dependency field before comparing its requirements."""
    value = document.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Expected TOML dependency string array: {key}")

    return value


def _preserve_toml(before: str, after: str) -> None:
    """Protect test tooling and dependency tables in standard pyproject metadata."""
    old, new = tomllib.loads(before), tomllib.loads(after)
    if old.get("dependency-groups") != new.get("dependency-groups"):
        raise ValueError("Changing dependency groups requires review")

    old_tool, new_tool = _toml_table(old, "tool"), _toml_table(new, "tool")
    for name in {
        "pytest",
        "tox",
        "coverage",
        "ruff",
        "mypy",
        "poetry",
        "pdm",
        "hatch",
        "uv",
        "setuptools",
    }:
        if old_tool.get(name) != new_tool.get(name):
            raise ValueError(
                f"Changing test tooling or unsupported dependency backend requires review: tool.{name}"
            )

    _preserve_project_requirements(
        _toml_table(old, "project"), _toml_table(new, "project")
    )
    _preserve_build_system(
        _toml_table(old, "build-system"), _toml_table(new, "build-system")
    )


def _preserve_project_requirements(
    old: Mapping[str, object], new: Mapping[str, object]
) -> None:
    """Preserve runtime requirements, extras, Python constraints, and dynamic metadata declarations."""
    _preserve_python_requirements(
        _toml_requirements(old, "dependencies"), _toml_requirements(new, "dependencies")
    )
    for key in ("requires-python", "dynamic"):
        if old.get(key) != new.get(key):
            raise ValueError(f"Changing project.{key} requires review")

    before, after = (
        _toml_table(old, "optional-dependencies"),
        _toml_table(new, "optional-dependencies"),
    )
    for group in before:
        _preserve_python_requirements(
            _toml_requirements(before, group), _toml_requirements(after, group)
        )


def _preserve_build_system(
    old: Mapping[str, object], new: Mapping[str, object]
) -> None:
    """Retain build backend identity while allowing additional backend requirements."""
    _preserve_python_requirements(
        _toml_requirements(old, "requires"), _toml_requirements(new, "requires")
    )
    if _without_requires(old) != _without_requires(new):
        raise ValueError("Changing the build backend or backend path requires review")


def _without_requires(table: Mapping[str, object]) -> dict[str, object]:
    """Separate backend identity from its independently validated requirement array."""
    return {key: value for key, value in table.items() if key != "requires"}


def _active_lines(text: str) -> list[str]:
    """Normalize continued commands without confusing comments with executable checks."""
    logical = text.replace("\\\n", " ")
    return [
        line.rstrip()
        for line in logical.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _rule_blocks(text: str) -> dict[str, str]:
    """Collect complete Make recipes so unreachable or ignored checks cannot hide in added lines."""
    blocks: dict[str, str] = {}
    current = ""
    for line in _active_lines(text):
        if not line[:1].isspace() and ":" in line and "=" not in line:
            current = line

        blocks[current] = blocks.get(current, "") + line + "\n"

    return blocks


def _protected_rule(target: str, body: str) -> bool:
    """Protect explicit check overrides and recipes that invoke checks or debhelper."""
    return (
        CHECK_COMMAND.search(body) is not None
        or re.search(r"(?:dh_auto_test|dh_missing|lintian)", target) is not None
    )


def _preserve_rules(before: str, after: str) -> None:
    """Freeze check recipe execution and reject newly introduced failure suppression."""
    old, new = _rule_blocks(before), _rule_blocks(after)
    for target in sorted(set(old) | set(new)):
        if _protected_rule(target, old.get(target, "") + new.get(target, "")):
            if old.get(target) != new.get(target):
                raise ValueError(
                    f"Changing packaging check recipes requires review: {target}"
                )

    _reject_added_bypasses(before, after)
    _preserve_rule_switches(before, after)


def _preserve_rule_switches(before: str, after: str) -> None:
    """Protect make conditionals and environment settings that can deactivate inherited checks."""
    if _make_settings(before) != _make_settings(after):
        raise ValueError(
            "Changing make conditionals, settings, or check environment requires review"
        )


def _make_settings(text: str) -> list[str]:
    """Freeze global settings, includes, special targets, and target-specific assignments."""
    return [line for line in _active_lines(text) if _make_setting(line)]


def _make_setting(line: str) -> bool:
    """Distinguish rule headers and recipe commands from make execution configuration."""
    if line.startswith("\t"):
        return False

    return "=" in line or ":" not in line or line.startswith(".")


def _reject_added_bypasses(before: str, after: str) -> None:
    """Check new executable lines for common shell and tool failure-bypass mechanisms."""
    added = Counter(_active_lines(after)) - Counter(_active_lines(before))
    if any(BYPASS.search(line) for line in added):
        raise ValueError(
            "A repair cannot suppress packaging checks or ignore command failures"
        )


def is_policy_file(name: str) -> bool:
    """Identify files whose executable checks or dependency contracts need semantic validation."""
    path = PurePosixPath(name)
    return (
        _python_test_file(name)
        or _metadata_file(path)
        or name.startswith("debian/tests/")
    )


def _metadata_file(path: PurePosixPath) -> bool:
    """Recognize standard Debian, Python, and test-configuration admission boundaries."""
    return (
        str(path) in {"debian/control", "debian/rules"}
        or path.name in TEST_CONFIG_NAMES | {"setup.cfg", "pyproject.toml", "setup.py"}
        or path.name.endswith(".lintian-overrides")
        or _requirement_file(str(path))
    )


def _requirement_file(name: str) -> bool:
    """Cover runtime, test, constraints, and named requirements variants."""
    path = PurePosixPath(name)
    return path.suffix in {".txt", ".in"} and _requirement_location(path)


def _requirement_location(path: PurePosixPath) -> bool:
    """Include requirement fragments whose purpose is expressed by the parent directory."""
    return (
        "requirements" in path.name
        or "constraints" in path.name
        or bool(set(path.parts[:-1]) & {"requirements", "constraints"})
    )


def validate_file_edit(
    name: str, before: str, after: str, *, evidence: str | None = None
) -> None:
    """Validate the complete effective file, including unchanged context surrounding an edit."""
    _protect_packaging_metadata(name)
    _validate_tests(name, before, after)
    _validate_override(name, before, after, evidence)
    try:
        _validate_metadata(name, before, after)
    except configparser.Error as exc:
        raise ValueError(
            "Ambiguous or malformed configuration requires review: " + str(exc)
        ) from exc
    if name.startswith("debian/tests/") and name != "debian/tests/control":
        if _active_lines(before) != _active_lines(after):
            raise ValueError(
                "Changing existing autopkgtest commands or adding executable hooks requires review"
            )


def _protect_packaging_metadata(name: str) -> None:
    """Keep independent raw-patch validation aligned with protected exact-edit targets."""
    if not name.startswith("debian/"):
        return

    if name == "debian/changelog" or name.startswith("debian/source/"):
        raise ValueError("Source identity and source validation options are protected")

    if re.search(
        r"(?:^|[.])(preinst|postinst|prerm|postrm|config|triggers)$",
        PurePosixPath(name).name,
    ):
        raise ValueError("Maintainer hooks require review")


def _validate_metadata(name: str, before: str, after: str) -> None:
    """Dispatch dependency and packaging guard comparisons without package-specific rules."""
    handlers: dict[str, Callable[[str, str], None]] = {
        "debian/control": _preserve_control,
        "debian/rules": _preserve_rules,
        "debian/tests/control": lambda old, new: _preserve_control(
            old, new, tests=True
        ),
        "pyproject.toml": _preserve_toml,
    }
    path = PurePosixPath(name)
    handler = handlers.get(name) or handlers.get(path.name)
    if handler is not None:
        handler(before, after)
        return

    _validate_python_metadata(str(path), before, after)


def _validate_python_metadata(name: str, before: str, after: str) -> None:
    """Keep legacy executable dependency metadata immutable when it cannot be compared safely."""
    basename = PurePosixPath(name).name
    if basename == "setup.py" and ast.dump(ast.parse(before)) != ast.dump(
        ast.parse(after)
    ):
        raise ValueError("Executable setup.py metadata changes require review")

    if basename in TEST_CONFIG_NAMES | {"setup.cfg"}:
        _preserve_ini(basename, before, after)

    if _requirement_file(name):
        _preserve_python_requirements(
            _requirement_lines(before), _requirement_lines(after)
        )


def _read_optional(root: Path, name: str) -> str:
    """Read a bounded policy file without traversing any source-tree symlink."""
    from .repair_agent import read_text, safe_path

    path = safe_path(root, name)
    return read_text(path) if path.exists() else ""


def validate_tree_edits(original: Path, proposed: Path, paths: Sequence[str]) -> None:
    """Recheck effective packaging files and nested quilt payloads at the independent patch gate."""
    for name in paths:
        before, after = _read_optional(original, name), _read_optional(proposed, name)
        if name.startswith("debian/patches/"):
            _validate_quilt_edit(name, before, after, original, proposed)
        else:
            validate_file_edit(name, before, after)


def _quilt_changes(text: str) -> tuple[str, ...]:
    """Retain transformation order and normalized target headers when comparing refresh intent."""
    return tuple(
        _normalized_quilt_line(line)
        for line in text.splitlines()
        if line.startswith(("--- ", "+++ ")) or _transformation_line(line)
    )


def _normalized_quilt_line(line: str) -> str:
    """Normalize historical package-version prefixes without ignoring file redirection."""
    if line.startswith(("--- ", "+++ ")):
        name = line[4:].split()[0]
        return line[:4] + (name if name == "/dev/null" else _quilt_target(name))

    return line


def _transformation_line(line: str) -> bool:
    """Recognize payload changes without treating unified-diff headers as edits."""
    return (
        line.startswith(("+", "-"))
        and not line.startswith(("+++ ", "--- "))
        and bool(line[1:].strip())
    )


def _quilt_targets(text: str) -> list[str]:
    """Require canonical inner diff paths before checking or applying a new quilt payload."""
    paths: list[str] = []
    for name in re.findall(r"^\+\+\+ (\S+)", text, re.MULTILINE):
        if name == "/dev/null":
            raise ValueError("Quilt file deletions require review")

        paths.append(_quilt_target(name))

    return paths


def _quilt_target(name: str) -> str:
    """Prevent an inner quilt patch from escaping the upstream source boundary."""
    from .repair_agent import _relative_path

    path = _relative_path(name)
    if len(path.parts) < 2:
        raise ValueError("Quilt payload paths require a strip-one prefix")

    relative = str(_relative_path("/".join(path.parts[1:])))
    if relative.startswith(("debian/", ".git/", ".pc/")):
        raise ValueError(
            "Quilt payload cannot change Debian identity, bookkeeping, or private state"
        )

    return relative


def _validate_quilt_edit(
    name: str, before: str, after: str, tree: Path, proposed: Path
) -> None:
    """Inspect newly authored quilt transformations while retaining context-only refresh compatibility."""
    if name == "debian/patches/series":
        _validate_series_edit(before, after, tree)
        return

    targets = _combined_policy_targets(before, after)
    if not targets or before == after:
        return

    if before:
        _validate_existing_quilt(before, after, tree, targets)
    else:
        _validate_new_quilt(name, after, tree, proposed, targets)


def _combined_policy_targets(before: str, after: str) -> list[str]:
    """Keep formerly protected files in scope when a replacement redirects its payload."""
    return sorted(
        set(_quilt_policy_targets(before)) | set(_quilt_policy_targets(after))
    )


def _validate_existing_quilt(
    before: str, after: str, tree: Path, targets: Sequence[str]
) -> None:
    """Check context-only refreshes against effective current files instead of trusting hunk counts."""
    if _quilt_changes(before) != _quilt_changes(after):
        raise ValueError(
            "Changing an existing quilt patch's test or dependency transformations requires review"
        )

    with tempfile.TemporaryDirectory(prefix="packagetest-policy-refresh-") as temporary:
        root = Path(temporary)
        working = root / "source"
        shutil.copytree(tree, working, symlinks=True)
        _validate_quilt_files(after, working, targets, root / "refresh.patch")


def _validate_series_edit(before: str, after: str, tree: Path) -> None:
    """Keep retained patch order and prevent dropping a patch that changes protected contracts."""
    from .repair_agent import _series_names

    old, new = _series_names(before), _series_names(after)
    _preserve_series_order(old, new)
    for name in set(old) - set(new):
        payload = _read_optional(tree, "debian/patches/" + name)
        if _quilt_policy_targets(payload):
            raise ValueError(
                "Dropping a test, dependency, or check configuration patch requires review"
            )


def _preserve_series_order(old: Sequence[str], new: Sequence[str]) -> None:
    """Allow reviewed nonpolicy drops and appended repairs without reordering the existing stack."""
    retained = [name for name in old if name in new]
    if list(new[: len(retained)]) != retained or len(set(new)) != len(new):
        raise ValueError(
            "Quilt series reordering, insertion before the existing stack, or duplicate entries requires review"
        )


def _quilt_policy_targets(payload: str) -> list[str]:
    """Select policy-sensitive upstream paths after validating every inner target."""
    return [path for path in _quilt_targets(payload) if is_policy_file(path)]


def _apply_quilt_prefix(
    original: Path, proposed: Path, working: Path, target: str, temporary: Path
) -> None:
    """Replay preceding newly generated patches when validating a cumulative proposal."""
    from .repair_agent import _series_names

    names = _series_names(_read_optional(proposed, "debian/patches/series"))
    for patch in names:
        name = "debian/patches/" + patch
        if name == target:
            return

        _apply_new_prefix(original, proposed, working, name, temporary)

    raise ValueError("New quilt patch must be referenced by the proposed series")


def _apply_new_prefix(
    original: Path, proposed: Path, working: Path, name: str, temporary: Path
) -> None:
    """Keep the materialized original stack and apply only added prefix payloads."""
    before, after = _read_optional(original, name), _read_optional(proposed, name)
    if not before:
        _quilt_targets(after)
        _apply_quilt_payload(after, working, temporary / "prefix.patch")


def _validate_new_quilt(
    name: str, payload: str, tree: Path, proposed: Path, targets: Sequence[str]
) -> None:
    """Compare complete effective upstream files without executing model-authored code."""
    from .repair_agent import materialize_source

    with tempfile.TemporaryDirectory(prefix="packagetest-policy-quilt-") as temporary:
        root = Path(temporary)
        working = root / "source"
        shutil.copytree(tree, working, symlinks=True)
        if (working / "debian/source/format").is_file():
            materialize_source(working)

        _apply_quilt_prefix(tree, proposed, working, name, root)
        _validate_quilt_files(payload, working, targets, root / "proposal.patch")


def _validate_quilt_files(
    payload: str, working: Path, targets: Sequence[str], patch: Path
) -> None:
    """Compare effective protected files after a purely mechanical patch application."""
    before = {name: _read_optional(working, name) for name in targets}
    _apply_quilt_payload(payload, working, patch)
    for name in targets:
        validate_file_edit(name, before[name], _read_optional(working, name))


def _apply_quilt_payload(payload: str, tree: Path, path: Path) -> None:
    """Use Git only as a patch applicator; source code remains inert during policy validation."""
    path.write_text(payload, encoding="utf-8")
    completed = subprocess.run(
        ["git", "apply", "--recount", "--whitespace=nowarn", str(path)],
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode:
        raise ValueError(
            "Cannot independently validate quilt payload: " + completed.stderr[-2000:]
        )


def _override_lines(text: str) -> list[str]:
    """Select active override declarations without treating comments as policy changes."""
    return [
        line for line in text.splitlines() if line.strip() and not line.startswith("#")
    ]


def _validate_override(
    name: str, before: str, after: str, evidence: str | None
) -> None:
    """Allow only explained file-specific Lintian exceptions tied to the observed diagnostic."""
    if not name.endswith(".lintian-overrides"):
        return

    if not any(line.startswith("# ") for line in after.splitlines()):
        raise ValueError("Lintian overrides require an explanatory comment")

    for line in sorted(set(_override_lines(after)) - set(_override_lines(before))):
        _validate_override_line(line, evidence)


def _validate_override_line(line: str, evidence: str | None) -> None:
    """Reject broad exceptions and architecture-mismatch suppression even when a build would pass."""
    from .repair_agent import _relative_path

    match = re.fullmatch(
        r"[a-z0-9+.-]+(?::| binary:) ([a-z0-9-]+) \[([a-zA-Z0-9_./+-]+)\]", line
    )
    if match is None:
        raise ValueError(
            "Lintian overrides must name one package, tag, and literal file context"
        )

    tag, path = match.groups()
    _relative_path(path)
    if tag == "arch-independent-package-contains-binary-or-object":
        raise ValueError(
            "Correct package architecture instead of overriding an architecture mismatch"
        )

    if evidence is not None and f"{tag} [{path}]" not in evidence:
        raise ValueError(
            "Lintian override must match a tag and file in the current failure"
        )
