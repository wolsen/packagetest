"""Run installed-package tests in a fresh VM, preserving exact candidate provenance."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
from pathlib import Path

from .artifacts import sha256, verify_binaries, verify_source
from .repositories import repository_setup


def _summary_tests(summary: str) -> list[dict]:
    """Parse recognized autopkgtest summary rows while ignoring unrelated output."""
    tests = []
    for line in summary.splitlines():
        match = re.match(r"^(\S+)\s+(PASS|FAIL|SKIP|FLAKY)(?:\s+(.*))?$", line)
        if match:
            tests.append(
                {"name": match[1], "result": match[2], "detail": match[3] or ""}
            )

    return tests


def _has_test_result(tests: list[dict], results: set[str]) -> bool:
    """Report whether any parsed test carries one of the requested outcomes."""
    return any(test["result"] in results for test in tests)


def _failed_result(returncode: int, tests: list[dict]) -> str | None:
    """Give infrastructure exits precedence over test failures and coverage outcomes."""
    if returncode in {16, 20} or returncode < 0:
        return "INFRA_ERROR"

    if returncode in {4, 6, 12, 14} or _has_test_result(tests, {"FAIL"}):
        return "FAIL"

    return None


def _all_superficial_passes(tests: list[dict]) -> bool:
    """Require actual test rows and a superficial PASS in every row."""
    return bool(tests) and all(
        test["result"] == "PASS" and "(superficial)" in test["detail"] for test in tests
    )


def _no_substantive_result(tests: list[dict]) -> str:
    """Distinguish superficial coverage, named skips, and absence of runnable tests."""
    if _all_superficial_passes(tests):
        return "SUPERFICIAL"

    return "SKIP" if any(test["name"] != "*" for test in tests) else "NO_TESTS"


def _successful_result(returncode: int, tests: list[dict]) -> str:
    """Accept a clean exit only with a PASS row, preserving superficial coverage."""
    if returncode == 0 and _has_test_result(tests, {"PASS"}):
        return (
            "SUPERFICIAL"
            if all("(superficial)" in test["detail"] for test in tests)
            else "PASS"
        )

    return "INFRA_ERROR"


def _test_result(returncode: int, tests: list[dict]) -> str:
    """Apply coverage rules only after excluding infrastructure and test failures."""
    failure = _failed_result(returncode, tests)
    if failure is not None:
        return failure

    if returncode == 8:
        return _no_substantive_result(tests)

    if returncode == 2 or _has_test_result(tests, {"SKIP", "FLAKY"}):
        return "SKIP"

    return _successful_result(returncode, tests)


def classify(returncode: int, summary: str) -> dict:
    """Classify autopkgtest status while retaining every parsed test and the raw exit code."""
    tests = _summary_tests(summary)
    return {
        "result": _test_result(returncode, tests),
        "returncode": returncode,
        "tests": tests,
    }


def exit_status(result: str) -> int:
    """Fail CI only for test failures, infrastructure errors, or blocked builds.

    Coverage gaps remain explicit in result.json and the workflow summary, but
    they do not make the GitHub job fail.  This lets a nightly run distinguish
    missing or superficial coverage from a broken package without presenting
    both as the same red job conclusion.
    """
    return 0 if result in {"PASS", "SKIP", "NO_TESTS", "SUPERFICIAL"} else 1


def _test_diagnostic(report: dict, test: dict) -> str | None:
    """Combine one nonpassing test's summary detail and captured output tail."""
    if test.get("result") not in {"FAIL", "SKIP", "FLAKY"}:
        return None

    text = f"{test.get('name', 'unknown')} {test['result']}"
    if test.get("detail"):
        text += f": {test['detail']}"

    diagnostic = report.get("test_diagnostics", {}).get(test.get("name"))
    if diagnostic:
        text += f": {diagnostic}"

    return text


def _summary_diagnostics(summary: str) -> list[str]:
    """Retain nonblank raw summary lines when no structured diagnostic exists."""
    return [line for line in str(summary).splitlines() if line.strip()]


def _bounded_diagnostic(details: list[str], limit: int) -> str:
    """Bound the combined diagnostic while supplying a useful empty-report fallback."""
    message = (
        "; ".join(details)
        or "No test-level diagnostic was recorded; inspect console.log"
    )
    return message if len(message) <= limit else message[: limit - 3] + "..."


def diagnostic_message(report: dict, *, limit: int = 2000) -> str:
    """Return the useful failure evidence hidden behind autopkgtest's exit code."""
    details = list(
        filter(
            None, (_test_diagnostic(report, test) for test in report.get("tests", []))
        )
    )
    if report.get("error"):
        details.append(str(report["error"]))

    if not details and report.get("summary"):
        details.extend(_summary_diagnostics(report["summary"]))

    return _bounded_diagnostic(details, limit)


def _test_output_tail(
    directory: Path, name: str, suffix: str, limit: int
) -> str | None:
    """Read a bounded nonempty test-output tail only from a regular nonsymlink file."""
    path = directory / f"{name}-{suffix}"
    if not path.is_file() or path.is_symlink():
        return None

    value = path.read_text(errors="replace").strip()
    if value:
        return f"{suffix}: {value[-limit:]}"

    return None


def _failed_test_tail(directory: Path, test: dict, limit: int) -> str:
    """Join stderr before stdout for named failing tests, retaining the overall limit."""
    if test.get("result") != "FAIL" or not test.get("name"):
        return ""

    parts = filter(
        None,
        (
            _test_output_tail(directory, test["name"], suffix, limit)
            for suffix in ("stderr", "stdout")
        ),
    )
    return "; ".join(parts)[-limit:]


def collect_test_diagnostics(
    directory: Path, tests: list[dict], *, limit: int = 4000
) -> dict:
    """Collect bounded per-test stderr/stdout tails from autopkgtest's output dir."""
    diagnostics = {}
    if not directory.is_dir():
        return diagnostics

    for test in tests:
        value = _failed_test_tail(directory, test, limit)
        if value:
            diagnostics[test["name"]] = value

    return diagnostics


def candidate_check_script(versions: dict[str, str]) -> str:
    """Check candidates after dpkg operations, allowing mutually exclusive binaries.

    autopkgtest publishes supplied debs at priority 1002, reinstalls candidates
    already in the base image and installs each test's Depends itself. This hook
    additionally checks tests that dynamically install/remove package variants.
    Residual configuration from removed packages does not count as installation.
    """
    return """import json
import os
from pathlib import Path
import subprocess
import sys
expected = json.loads(EXPECTED_JSON)
output = subprocess.check_output([
    'dpkg-query', '-W', '-f=${binary:Package}\\t${Version}\\t${db:Status-Status}\\n'
], text=True)
installed = {}
errors = []
for line in output.splitlines():
    name, version, state = line.split('\\t')
    name = name.split(':', 1)[0]
    if name not in expected or state in {'not-installed', 'config-files'}:
        continue
    installed[name] = version
    if version != expected[name]:
        errors.append(f'{name}: installed {version}, expected {expected[name]}')
print('PACKAGETEST_INSTALLED_CANDIDATES ' + json.dumps(installed, sort_keys=True), flush=True)
if os.environ.get('AUTOPKGTEST_ARTIFACTS'):
    with (Path(os.environ['AUTOPKGTEST_ARTIFACTS']) / 'candidate-versions.jsonl').open('a') as evidence:
        evidence.write(json.dumps(installed, sort_keys=True) + '\\n')
if errors:
    print('Candidate version mismatch: ' + '; '.join(errors), file=sys.stderr)
    raise SystemExit(1)
""".replace("EXPECTED_JSON", repr(json.dumps(versions, sort_keys=True)))


def checked_file(directory: Path, record: dict) -> Path:
    """Resolve a basename artifact only after checking its recorded digest."""
    name = record["file"]
    if Path(name).name != name:
        raise ValueError("Artifact filename is not a basename")

    path = directory / name
    if not path.is_file() or sha256(path) != record["sha256"]:
        raise ValueError(f"Missing or corrupt artifact: {path}")

    return path


def _source_descriptor(base: Path, package: dict, source: str) -> Path:
    """Verify source artifacts and require exactly one matching Debian descriptor."""
    sources = [
        checked_file(base / "source", record) for record in package["source_artifacts"]
    ]
    dscs = [path for path in sources if path.suffix == ".dsc"]
    if len(dscs) != 1:
        raise ValueError("Expected one source descriptor")

    verify_source(dscs[0], source, package["version"])
    return dscs[0]


def _binary_inputs(
    base: Path, package: dict, source: str, architecture: str
) -> list[Path]:
    """Verify each candidate binary and its package, version, and architecture metadata."""
    binaries = [checked_file(base / "binary", record) for record in package["binaries"]]
    verify_binaries(
        base / "binary",
        source=source,
        version=package["version"],
        expected=[b["package"] for b in package["binaries"]],
        arch=architecture,
    )
    return binaries


def manifest_inputs(path: Path, source: str) -> tuple[dict, dict, Path, list[Path]]:
    """Load successful build inputs and verify their source and binary provenance."""
    manifest = json.loads(path.read_text())
    package = next(p for p in manifest["packages"] if p["source"] == source)
    if package["result"] != "SUCCEEDED":
        raise ValueError(f"Cannot test unsuccessful build: {source}")

    base = path.parent / "artifacts" / source
    dsc = _source_descriptor(base, package, source)
    binaries = _binary_inputs(base, package, source, manifest["target"]["architecture"])
    return manifest, package, dsc, binaries


def _dependency_candidate(repository: Path, record: dict) -> Path:
    """Reject escaped, missing, or modified dependency repository artifacts."""
    relative = Path(record["path"])
    path = (repository / relative).resolve()
    if (
        not path.is_relative_to(repository.resolve())
        or not path.is_file()
        or sha256(path) != record["sha256"]
    ):
        raise ValueError(f"Corrupt dependency repository artifact: {relative}")

    return path


def _merge_dependencies(candidates: dict[str, Path], repository: Path) -> dict:
    """Merge verified dependencies without accepting conflicting candidate filenames."""
    provenance = json.loads((repository / "provenance.json").read_text())
    for record in provenance["packages"]:
        path = _dependency_candidate(repository, record)
        if (
            path.name in candidates
            and sha256(candidates[path.name]) != record["sha256"]
        ):
            raise ValueError(f"Conflicting candidate: {path.name}")

        candidates[path.name] = path

    return provenance


def _candidate_versions(candidates: dict[str, Path], repo: Path) -> dict[str, str]:
    """Pin each candidate version and install the testbed's provenance-check hook."""
    pins = []
    versions = {}
    for path in candidates.values():
        name, version = (
            subprocess.check_output(
                ["dpkg-deb", "-f", str(path), "Package", "Version"], text=True
            )
            .strip()
            .splitlines()
        )
        # dpkg-deb emits field labels when multiple fields are requested.
        name, version = (
            name.removeprefix("Package: "),
            version.removeprefix("Version: "),
        )
        if name in versions and versions[name] != version:
            raise ValueError(f"Multiple versions in candidate repository: {name}")

        versions[name] = version
        pins.append(f"Package: {name}\nPin: version {version}\nPin-Priority: 1001\n")

    (repo / "preferences").write_text("\n".join(pins))
    (repo / "verify-installed.py").write_text(candidate_check_script(versions))
    return versions


def _candidate_repository(
    output: Path, candidates: dict[str, Path]
) -> tuple[Path, dict[str, str]]:
    """Copy and index the verified binary set before generating version preferences."""
    repo = output / "candidate-repository"
    repo.mkdir()
    for name, path in candidates.items():
        shutil.copy2(path, repo / name)

    with (repo / "Packages").open("w") as handle:
        subprocess.run(
            ["dpkg-scanpackages", ".", "/dev/null"], cwd=repo, stdout=handle, check=True
        )

    return repo, _candidate_versions(candidates, repo)


def _testbed_setup(output: Path, target: dict) -> Path:
    """Write the suite check, repository configuration, and installed-version hook."""
    archive_setup = repository_setup(target)
    setup = output / "setup.sh"
    setup.write_text(
        "set -eu\n"
        'test "$(. /etc/os-release; echo "$VERSION_CODENAME")" = '
        + shlex.quote(target["suite"])
        + "\n"
        + "apt-get update\n"
        + archive_setup
        + "printf '%s\\n' 'deb [trusted=yes] file:/opt/packagetest-candidate ./' > /etc/apt/sources.list.d/packagetest.list\n"
        "cp /opt/packagetest-candidate/preferences /etc/apt/preferences.d/packagetest\n"
        "apt-get update\n"
        "DEBIAN_FRONTEND=noninteractive apt-get install -y apt-utils\n"
        "command -v python3\n"
        "printf '%s\\n' 'post-invoke=python3 /opt/packagetest-candidate/verify-installed.py' > /etc/dpkg/dpkg.cfg.d/packagetest-versions\n"
    )
    return setup


def _test_command(
    output: Path,
    repo: Path,
    setup: Path,
    dsc: Path,
    candidates: dict[str, Path],
    backend: str,
    image: str,
    timeout: int,
) -> list[str]:
    """Build isolated testbed arguments with explicit candidate inputs and timeouts."""
    command = [
        "autopkgtest",
        "--no-built-binaries",
        "--no-apt-fallback",
        "--output-dir=" + str(output / "testbed"),
        "--summary=" + str(output / "summary"),
        "--timeout-test=" + str(timeout),
        "--timeout-install=1800",
        "--copy=" + str(repo.resolve()) + ":/opt/packagetest-candidate",
        "--setup-commands=" + str(setup.resolve()),
        str(dsc),
        *[str(p) for p in candidates.values()],
        "--",
        backend,
    ]
    if backend == "qemu":
        command += ["--ram-size=4096", "--cpus=2", str(Path(image).resolve())]
    else:
        command += [image]

    return command


def _execute_test(command: list[str], output: Path, timeout: int) -> int:
    """Capture test output and reap the complete process group after a timeout."""
    with (output / "console.log").open("w") as log:
        with subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        ) as process:
            try:
                returncode = process.wait(timeout=timeout + 3600)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise

    return returncode


def _run_tests(
    manifest_path: Path,
    source: str,
    output: Path,
    report: dict,
    *,
    backend: str,
    image: str,
    dependency_repository: Path | None,
    timeout: int,
) -> None:
    """Prepare verified candidates, run the isolated testbed, and collect its evidence."""
    manifest, package, dsc, binaries = manifest_inputs(manifest_path.resolve(), source)
    report.update(
        generation_id=manifest["generation_id"],
        version=package["version"],
        lock_sha256=manifest["lock_sha256"],
        target=manifest["target"],
    )
    if backend not in {"qemu", "lxd"}:
        raise ValueError("Only isolated qemu and lxd testbeds are supported")

    candidates = {path.name: path for path in binaries}
    if dependency_repository:
        report["dependency_provenance"] = _merge_dependencies(
            candidates, dependency_repository
        )

    repo, versions = _candidate_repository(output, candidates)
    setup = _testbed_setup(output, manifest["target"])
    command = _test_command(
        output, repo, setup, dsc, candidates, backend, image, timeout
    )
    report.update(
        command=command,
        expected_versions={
            binary["package"]: binary["version"] for binary in package["binaries"]
        },
        candidate_versions=versions,
        installation_policy="autopkgtest resolves each test Depends; dpkg hook verifies installed candidate subsets",
    )
    returncode = _execute_test(command, output, timeout)
    _record_test_results(report, output, returncode)


def _record_test_results(report: dict, output: Path, returncode: int) -> None:
    """Attach summary classification and bounded test diagnostics to the run report."""
    summary_path = output / "summary"
    summary = summary_path.read_text() if summary_path.exists() else ""
    report.update(summary=summary, **classify(returncode, summary))
    report["test_diagnostics"] = collect_test_diagnostics(
        output / "testbed", report["tests"]
    )


def run(
    manifest_path: Path,
    source: str,
    output: Path,
    *,
    backend: str,
    image: str,
    dependency_repository: Path | None = None,
    timeout: int = 7200,
) -> dict:
    """Run installed-package tests and persist failure evidence even when setup fails."""
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "source": source,
        "result": "INFRA_ERROR",
        "backend": backend,
        "image": image,
    }
    try:
        _run_tests(
            manifest_path,
            source,
            output,
            report,
            backend=backend,
            image=image,
            dependency_repository=dependency_repository,
            timeout=timeout,
        )
    except (
        OSError,
        ValueError,
        KeyError,
        StopIteration,
        subprocess.SubprocessError,
    ) as error:
        report.update(result="INFRA_ERROR", error=str(error))
    finally:
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")

    return report
