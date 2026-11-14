"""Isolation runners for the extraction child (docs 10.5). A subprocess is NOT a sandbox.

* ``LinuxContainerRunner`` (``linux_container_reference``): OCI container, no network, read-only root, bounded
  memory/CPU/processes, no capabilities, empty environment. Verified by the canary before use.
* ``UnconfinedDevelopmentRunner`` (``unconfined_development``): separate process, empty environment, ``rlimit``
  and a deadline only. Development only; refused unless the operator sets ``untrusted_input=False``.
* ``host_provided``: any object with ``profile = "host_provided"``, ``run`` and ``verify``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

from ..errors import IsolationUnavailableError

_BOOT = (
    "import sys; sys.path.insert(0, '/opt/cce/src'); from cawbase_content_engine.documents.child import main; main()"
)


class ChildExit(RuntimeError):
    """The isolated child ended with a non-zero status (the code is evidence: 137 = killed by the OOM killer)."""

    def __init__(self, code: int) -> None:
        super().__init__("extraction process failed")
        self.code = code


def _header(params: dict[str, Any]) -> bytes:
    return json.dumps(params).encode() + b"\n"


async def _communicate(
    proc: asyncio.subprocess.Process, payload: bytes, cap: int, timeout_s: float, kill: Any
) -> bytes:
    """Writes the payload, reads at most ``cap`` bytes, enforces the deadline; kills the whole child on any failure."""
    assert proc.stdin is not None and proc.stdout is not None
    try:
        async with asyncio.timeout(timeout_s):
            proc.stdin.write(payload)
            try:
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
            out = bytearray()
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                out += chunk
                if len(out) > cap:
                    from ..jobs.worker import JobFailure

                    raise JobFailure("limit_exceeded")
            code = await proc.wait()
    except BaseException:
        kill()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except (TimeoutError, ProcessLookupError):
            pass
        raise
    if code == 3:
        from ..jobs.worker import JobFailure

        raise JobFailure("limit_exceeded")
    if code != 0:
        raise ChildExit(code)
    return bytes(out)


class UnconfinedDevelopmentRunner:
    profile = "unconfined_development"

    def __init__(self, python: str | None = None) -> None:
        self._python = python or sys.executable

    async def verify(self) -> bool:
        return True  # nothing to verify: there are no controls

    async def run(self, data: bytes, params: dict[str, Any], *, timeout_s: float, limits: Any) -> dict[str, Any]:
        cpu, mem = int(limits.extraction_cpu_s), int(limits.extraction_memory_bytes)

        def preexec() -> None:  # pragma: no cover - runs in the child
            import resource

            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
            if sys.platform.startswith("linux"):
                for name, val in (("RLIMIT_AS", mem), ("RLIMIT_NPROC", int(limits.extraction_max_pids))):
                    try:
                        resource.setrlimit(getattr(resource, name), (val, val))
                    except (ValueError, OSError):
                        pass

        proc = await asyncio.create_subprocess_exec(
            self._python,
            "-I",
            "-m",
            "cawbase_content_engine.documents.child",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={},
            cwd="/",
            start_new_session=True,
            preexec_fn=preexec,  # noqa: PLW1509
        )

        def kill() -> None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        raw = await _communicate(
            proc, _header(params) + data, int(params["max_extraction_output_bytes"]), timeout_s, kill
        )
        return json.loads(raw)  # type: ignore[no-any-return]


class LinuxContainerRunner:
    profile = "linux_container_reference"

    def __init__(self, image: str, source_dir: str | os.PathLike[str] | None = None, docker: str = "docker") -> None:
        self._image, self._docker = image, docker
        self._src = str(Path(source_dir or Path(__file__).resolve().parents[2]).resolve())

    def _args(self, name: str, limits: Any, *, interactive: bool = True) -> list[str]:
        mem = f"{int(limits.extraction_memory_bytes) // (1024 * 1024)}m"
        return [
            self._docker,
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",  # noqa: S108
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(int(limits.extraction_max_pids)),
            "--memory",
            mem,
            "--memory-swap",
            mem,
            "--cpus",
            "1",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-v",
            f"{self._src}:/opt/cce/src:ro",
            self._image,
            "python",
            "-I",
            "-c",
            _BOOT,
        ]

    async def _exec(self, payload: bytes, params: dict[str, Any], limits: Any, timeout_s: float) -> bytes:
        name = "cce-ext-" + uuid.uuid4().hex[:12]
        proc = await asyncio.create_subprocess_exec(
            *self._args(name, limits),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        def kill() -> None:
            import subprocess

            subprocess.run([self._docker, "kill", name], capture_output=True, check=False, timeout=15)  # noqa: S603
            try:
                proc.kill()
            except ProcessLookupError:
                pass

        return await _communicate(
            proc, payload, int(params.get("max_extraction_output_bytes", 64 * 1024 * 1024)), timeout_s, kill
        )

    async def run(self, data: bytes, params: dict[str, Any], *, timeout_s: float, limits: Any) -> dict[str, Any]:
        return json.loads(await self._exec(_header(params) + data, params, limits, timeout_s))  # type: ignore[no-any-return]

    async def verify(self, limits: Any | None = None) -> bool:
        """Canary INSIDE the container, with the limits the job will really use (docs 10.5, AC-A05-20):

        1. network, writes outside the tmpfs, sensitive environment and the process limit are probed in the child;
           the child also reports the memory limit it actually sees (cgroup) and the process limit is probed with
           ``extraction_max_pids + 16`` threads;
        2. memory: a second run tries to allocate (and touch) more than ``extraction_memory_bytes``; the control is
           applied only if that run does NOT complete (killed by the OOM killer, exit 137, or ``MemoryError``).
        Any failure to run the probes is ``IsolationUnavailableError``; there is no fallback."""
        from ..jobs.setup import JobsLimits

        lim = limits or JobsLimits()
        mem, pids = int(lim.extraction_memory_bytes), int(lim.extraction_max_pids)
        try:
            raw = await self._exec(_header({"canary": True, "expected_pids": pids}), {"canary": True}, lim, 60.0)
            out = json.loads(raw)
        except Exception:
            raise IsolationUnavailableError() from None
        if not out.get("all_blocked"):
            return False
        seen = out.get("memory_limit_bytes")
        if seen is not None and int(seen) > mem:
            return False  # the effective limit inside the container is looser than the configured one
        try:
            await self._exec(_header({"canary_memory": mem + 64 * 1024 * 1024}), {"canary": True}, lim, 120.0)
        except ChildExit as exc:
            return exc.code in (137, 3, -9)  # the allocation was denied: the memory control is applied
        except Exception:
            raise IsolationUnavailableError() from None
        return False  # the child allocated beyond the limit: NOT confined
