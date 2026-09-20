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

from .models import CommandResult

logger = logging.getLogger(__name__)


class CommandRunner:
    def __init__(self, log_path: Path, *, stream: bool = False, timeout: float = 3600):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = stream
        self.timeout = timeout
        self.sequence = 0

    def run(self, command: list[str], cwd: Path, env_diff: dict[str, str] | None = None) -> CommandResult:
        env_diff = env_diff or {}
        env = os.environ.copy()
        env.update(env_diff)
        rendered_command = shlex.join(command)
        logger.debug("Running command: %s", rendered_command)
        logger.debug("Command cwd: %s", cwd)
        logger.debug("Command env overrides: %s", env_diff)
        start = time.monotonic()
        self.sequence += 1
        output = {"stdout": bytearray(), "stderr": bytearray()}
        process = None
        exit_code = 127
        with (self.log_path.parent / f"{self.sequence:04d}.stdout.log").open("wb") as out, \
             (self.log_path.parent / f"{self.sequence:04d}.stderr.log").open("wb") as err:
            sinks = {"stdout": out, "stderr": err}
            try:
                process = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                    timed_out = False
                    while selector.get_map():
                        if not timed_out and time.monotonic() - start > self.timeout:
                            os.killpg(process.pid, signal.SIGKILL)
                            timed_out = True
                        for key, _ in selector.select(timeout=0.1):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                                continue
                            sinks[key.data].write(chunk)
                            sinks[key.data].flush()
                            output[key.data].extend(chunk)
                            # Full streams live in files; keep a bounded diagnostic tail in JSON.
                            del output[key.data][:-262144]
                            if self.stream:
                                sys.stderr.write(chunk.decode("utf-8", errors="replace"))
                                sys.stderr.flush()
                    exit_code = process.wait()
                    if timed_out:
                        exit_code = 124
                        message = f"\nCommand timed out after {self.timeout}s\n".encode()
                        err.write(message)
                        output["stderr"].extend(message)
            except OSError as exc:
                message = str(exc).encode()
                output["stderr"].extend(message)
                err.write(message)
            except KeyboardInterrupt:
                exit_code = 130
                output["stderr"].extend(b"Command interrupted\n")
            finally:
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
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
        )
        self._append(result)
        return result

    def _append(self, result: CommandResult) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result.to_dict(), sort_keys=True))
            f.write("\n")
