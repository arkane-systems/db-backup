"""Subprocess helpers.

Secrets are passed to child processes only through their environment or through
0600 temporary files (``secret_file``), never on the command line, where any user
on the host could read them from /proc.

Pipelines check every process in the pipe, as ``set -o pipefail`` would: a dump
that dies halfway leaves a perfectly valid, truncated zstd file behind it, so
zstd's exit code alone proves nothing.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path

log = logging.getLogger(__name__)

STDERR_TAIL = 4000


class CommandError(Exception):
    def __init__(self, cmd: Sequence[str], returncode: int, stderr: str):
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip()[-STDERR_TAIL:] or "(no output)"
        super().__init__(f"{cmd[0]} exited with status {returncode}: {detail}")


def _env(extra: dict[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
    if extra:
        env.update(extra)
    return env


def run(
    cmd: Sequence[str], *, env: dict[str, str] | None = None, input: str | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(cmd))
    proc = subprocess.run(list(cmd), env=_env(env), input=input, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stderr)
    return proc


def pipe_to_zstd(cmd: Sequence[str], out_path: Path, level: int, *, env: dict[str, str] | None = None) -> str:
    """Run ``cmd | zstd -<level> -o out_path``; return cmd's stderr (warnings)."""
    log.debug("dump: %s > %s", " ".join(cmd), out_path)
    zstd_cmd = ["zstd", "-q", "-f", "-T0", f"-{level}", "-o", str(out_path)]
    with tempfile.TemporaryFile() as err1, tempfile.TemporaryFile() as err2:
        producer = subprocess.Popen(list(cmd), env=_env(env), stdout=subprocess.PIPE, stderr=err1)
        try:
            consumer = subprocess.Popen(zstd_cmd, stdin=producer.stdout, stderr=err2)
        except BaseException:
            producer.kill()
            producer.wait()
            raise
        producer.stdout.close()  # so the producer gets SIGPIPE if zstd dies
        rc_consumer = consumer.wait()
        rc_producer = producer.wait()
        stderr = _read(err1)
        if rc_producer != 0:
            raise CommandError(cmd, rc_producer, stderr)
        if rc_consumer != 0:
            raise CommandError(zstd_cmd, rc_consumer, _read(err2))
        return stderr


def zstd_pipe_from(in_path: Path, cmd: Sequence[str], *, env: dict[str, str] | None = None) -> str:
    """Run ``zstd -dc in_path | cmd``; return cmd's stderr."""
    log.debug("load: %s < %s", " ".join(cmd), in_path)
    zstd_cmd = ["zstd", "-q", "-dc", str(in_path)]
    with tempfile.TemporaryFile() as err1, tempfile.TemporaryFile() as err2:
        producer = subprocess.Popen(zstd_cmd, stdout=subprocess.PIPE, stderr=err1)
        try:
            consumer = subprocess.Popen(list(cmd), env=_env(env), stdin=producer.stdout, stdout=subprocess.DEVNULL, stderr=err2)
        except BaseException:
            producer.kill()
            producer.wait()
            raise
        producer.stdout.close()
        rc_consumer = consumer.wait()
        rc_producer = producer.wait()
        stderr = _read(err2)
        if rc_consumer != 0:
            raise CommandError(cmd, rc_consumer, stderr)
        if rc_producer != 0:
            raise CommandError(zstd_cmd, rc_producer, _read(err1))
        return stderr


def zstd_head_tail(path: Path, head: int = 64, tail: int = 4096) -> tuple[bytes, bytes]:
    """Decompress the whole of a .zst file (proving it intact) and return its first and last bytes."""
    proc = subprocess.Popen(["zstd", "-q", "-dc", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    first = b""
    last = b""
    while chunk := proc.stdout.read(1 << 20):
        if len(first) < head:
            first += chunk[: head - len(first)]
        last = (last + chunk)[-tail:]
    stderr = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        raise CommandError(["zstd", "-dc", str(path)], proc.returncode, stderr)
    return first, last


@contextlib.contextmanager
def secret_file(content: str, suffix: str = "") -> Iterator[Path]:
    """A 0600 temporary file holding ``content``, deleted afterwards."""
    fd, name = tempfile.mkstemp(prefix="dbbackup-", suffix=suffix)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        yield Path(name)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def _read(f) -> str:
    f.seek(0)
    return f.read().decode(errors="replace")
