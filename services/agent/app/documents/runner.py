"""Run the extraction helper as a contained subprocess (Milestone 10 S1).

The runtime has already read and verified the file's bytes from a handle; the helper gets **bytes**, never
a path. It is started with:

* a fixed argument vector (`python -E -s -m app.documents.helper --format <closed format>`) and no shell;
* an environment built from nothing but the few variables Python needs to start -- no database URL, no
  runtime token, no provider key, no `LUMI_*` variable, no proxy;
* on Windows, its own Job Object: kill-on-close, a per-process memory limit, and a one-process limit, so
  it cannot start any other program;
* a wall-clock limit, after which the job is terminated and the result is `extraction_timeout`.

Its stdout is bounded and parsed as one closed JSON object. Anything else is `extraction_failed`.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from app.documents.errors import EXTRACTION_CODES, ExtractionRefusal
from app.documents.limits import (
    HELPER_MEMORY_BYTES,
    HELPER_TIMEOUT_SECONDS,
    MAX_FILE_BYTES,
    MAX_HELPER_OUTPUT_BYTES,
    MAX_TEXT_CHARS,
    SUPPORTED_FORMATS,
)

AGENT_ROOT: Final = Path(__file__).resolve().parents[2]
#: The only inherited variables. Python needs `SystemRoot` to start on Windows; nothing else is passed on.
_INHERITED: Final = ("SystemRoot", "SYSTEMROOT", "WINDIR")


@dataclass(frozen=True, slots=True)
class Extracted:
    format: str
    text: str
    pages: int | None
    truncated: bool
    flags: dict[str, int] = field(default_factory=dict)


def helper_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    inherited = os.environ if source is None else source
    environment = {key: inherited[key] for key in _INHERITED if key in inherited}
    environment.update({"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    return environment


def helper_interpreter() -> str:
    """The real interpreter, not a virtual-environment launcher.

    A venv's `python.exe` on Windows is a small redirector that starts the base interpreter as a *second*
    process, which the helper job's one-process limit correctly forbids. The helper needs only the
    standard library and this package (imported from `cwd`), so the base interpreter is sufficient; in the
    packaged runtime the two are the same file.
    """
    base = getattr(sys, "_base_executable", None)
    return base if isinstance(base, str) and base and os.path.isfile(base) else sys.executable


def helper_command(declared: str) -> list[str]:
    if declared not in SUPPORTED_FORMATS:
        raise ExtractionRefusal("unsupported_format")
    return [helper_interpreter(), "-E", "-s", "-S", "-m", "app.documents.helper", "--format", declared]


def _parse(output: bytes) -> Extracted:
    if len(output) > MAX_HELPER_OUTPUT_BYTES:
        raise ExtractionRefusal("extraction_failed")
    try:
        payload = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ExtractionRefusal("extraction_failed") from None
    if not isinstance(payload, dict):
        raise ExtractionRefusal("extraction_failed")
    if payload.get("ok") is False:
        code = payload.get("code")
        raise ExtractionRefusal(code if isinstance(code, str) and code in EXTRACTION_CODES else "extraction_failed")
    text = payload.get("text")
    kind = payload.get("format")
    pages = payload.get("pages")
    truncated = payload.get("truncated")
    flags = payload.get("flags")
    if (
        payload.get("ok") is not True
        or not isinstance(text, str)
        or len(text) > MAX_TEXT_CHARS
        or kind not in SUPPORTED_FORMATS
        or not (pages is None or (isinstance(pages, int) and 0 <= pages <= 10_000))
        or not isinstance(truncated, bool)
        or not isinstance(flags, dict)
        or not all(isinstance(key, str) and isinstance(value, int) for key, value in flags.items())
    ):
        raise ExtractionRefusal("extraction_failed")
    return Extracted(format=kind, text=text, pages=pages, truncated=truncated, flags=dict(flags))


def run_helper(data: bytes, declared: str, *, timeout_seconds: float = HELPER_TIMEOUT_SECONDS) -> Extracted:
    """Blocking. Call it from a worker thread."""
    if len(data) > MAX_FILE_BYTES:
        raise ExtractionRefusal("file_too_large")
    command = helper_command(declared)
    job = None
    if os.name == "nt":
        from app.services.windows_job import ProcessJob

        job = ProcessJob(memory_limit_bytes=HELPER_MEMORY_BYTES, max_processes=1)
    try:
        process = subprocess.Popen(  # noqa: S603 - a fixed module and a closed format; no shell.
            command,
            shell=False,
            cwd=str(AGENT_ROOT),
            env=helper_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            if job is not None:
                # Assigned before a single byte is written: the helper blocks on stdin until then.
                job.assign(process.pid)
            output, _ = process.communicate(data, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            if job is not None:
                job.terminate()
            process.kill()
            process.communicate()
            raise ExtractionRefusal("extraction_timeout") from None
        except OSError:
            process.kill()
            process.communicate()
            raise ExtractionRefusal("extraction_failed") from None
        if process.returncode != 0:
            raise ExtractionRefusal("extraction_failed")
        return _parse(output)
    finally:
        if job is not None:
            job.close()
