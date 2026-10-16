"""Execute bounded commands while preserving complete logs and diagnostic tails."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import selectors
import signal
import sys
import time
from pathlib import Path
from typing import Protocol, BinaryIO, IO

from .models import CommandResult

logger = logging.getLogger(__name__)


class CommandExecutor(Protocol):
    """Execute commands and expose complete stdout independently of log naming."""

    def run(
        self, command: list[str], cwd: Path, env_diff: dict[str, str] | None = None
    ) -> CommandResult:
        """Return diagnostic tails and access to complete captured stdout."""
        ...


class CommandCapture:
    """Own one process, its deadline, and bounded stream tails until it is reaped."""

    def __init__(
        self, sinks: dict[str, BinaryIO], stream: bool, timeout: float, start: float
    ) -> None:
        """Bind full-log sinks and initialize independent process and stream state."""
        self.sinks = sinks
        self.stream = stream
        self.timeout = timeout
        self.start = start
        self.output = {"stdout": bytearray(), "stderr": bytearray()}
        self.process: subprocess.Popen[bytes] | None = None
        self.timed_out = False

    def execute(self, command: list[str], cwd: Path, env: dict[str, str]) -> int:
        """Launch, capture, and reap a process, retaining launch and interrupt diagnostics."""
        exit_code = 127
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            exit_code = self._drain(self.process)
        except OSError as exc:
            message = str(exc).encode()
            self.output["stderr"].extend(message)
            self.sinks["stderr"].write(message)
        except KeyboardInterrupt:
            exit_code = 130
            self.output["stderr"].extend(b"Command interrupted\n")
        finally:
            self._reap_remaining_process()

        return exit_code

    def _drain(self, process: subprocess.Popen[bytes]) -> int:
        """Drain both streams while enforcing the deadline independently of pipe EOF."""
        with selectors.DefaultSelector() as selector:
            self._register_streams(selector, process)
            # A child may close its pipes before exiting; EOF does not end its budget.
            while selector.get_map() or process.poll() is None:
                self._enforce_deadline(process)
                for key, _ in selector.select(timeout=0.1):
                    self._read_ready(selector, key)

            exit_code = process.wait()
            if self.timed_out:
                message = f"\nCommand timed out after {self.timeout}s\n".encode()
                self.sinks["stderr"].write(message)
                self.output["stderr"].extend(message)
                return 124

            return exit_code

    def _register_streams(
        self, selector: selectors.BaseSelector, process: subprocess.Popen[bytes]
    ) -> None:
        """Register concrete pipe handles and retain them as typed selector payloads."""
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            if stream is None:
                raise RuntimeError(f"Command capture requires a {name} pipe")

            selector.register(stream, selectors.EVENT_READ, (name, stream))

    def _enforce_deadline(self, process: subprocess.Popen[bytes]) -> None:
        """Kill a timed-out process group once, allowing a simultaneous natural exit."""
        if not self.timed_out and time.monotonic() - self.start > self.timeout:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # The process group exited at the deadline.

            self.timed_out = True

    def _read_ready(
        self, selector: selectors.BaseSelector, key: selectors.SelectorKey
    ) -> None:
        """Write complete stream bytes and retain only a bounded diagnostic tail."""
        # SelectorKey.fileobj also permits integer descriptors; our payload owns
        # the actual pipe and its name, so EOF can close precisely that handle.
        name: str
        stream: IO[bytes]
        name, stream = key.data
        chunk = os.read(stream.fileno(), 65536)
        if not chunk:
            selector.unregister(key.fileobj)
            stream.close()
            return

        self.sinks[name].write(chunk)
        self.sinks[name].flush()
        self.output[name].extend(chunk)
        del self.output[name][:-262144]
        if self.stream:
            sys.stderr.write(chunk.decode("utf-8", errors="replace"))
            sys.stderr.flush()

    def _reap_remaining_process(self) -> None:
        """Ensure no live child survives error or interrupt handling."""
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()


class CommandRunner:
    """Sequence command logs and publish one structured result per invocation."""

    def __init__(
        self, log_path: Path, *, stream: bool = False, timeout: float = 3600
    ) -> None:
        """Configure log storage, optional console streaming, and process budgets."""
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = stream
        self.timeout = timeout
        self.sequence = 0

    def run(
        self, command: list[str], cwd: Path, env_diff: dict[str, str] | None = None
    ) -> CommandResult:
        """Execute one command and append its bounded diagnostic result."""
        env_diff = env_diff or {}
        env = os.environ.copy()
        env.update(env_diff)
        rendered_command = shlex.join(command)
        logger.debug("Running command: %s", rendered_command)
        logger.debug("Command cwd: %s", cwd)
        logger.debug("Command env overrides: %s", env_diff)
        start = time.monotonic()
        self.sequence += 1
        stdout_path = self.log_path.parent / f"{self.sequence:04d}.stdout.log"
        stderr_path = self.log_path.parent / f"{self.sequence:04d}.stderr.log"
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            capture = CommandCapture(
                {"stdout": out, "stderr": err}, self.stream, self.timeout, start
            )
            exit_code = capture.execute(command, cwd, env)

        output = capture.output
        stdout = output["stdout"].decode("utf-8", errors="replace")
        stderr = output["stderr"].decode("utf-8", errors="replace")
        duration = time.monotonic() - start
        logger.debug(
            "Command finished: %s (exit_code=%d, duration_seconds=%.3f)",
            rendered_command,
            exit_code,
            duration,
        )
        logger.debug("Command stdout (%s):\n%s", rendered_command, stdout or "<empty>")
        logger.debug("Command stderr (%s):\n%s", rendered_command, stderr or "<empty>")
        result = CommandResult(
            command=command,
            cwd=str(cwd),
            env_diff=env_diff,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_seconds=duration,
            stdout_path=str(stdout_path.resolve()),
            stderr_path=str(stderr_path.resolve()),
        )
        self._append(result)
        return result

    def _append(self, result: CommandResult) -> None:
        """Append a stable JSON record to the command history."""
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result.to_dict(), sort_keys=True))
            f.write("\n")
