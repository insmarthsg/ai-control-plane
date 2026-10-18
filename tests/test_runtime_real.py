"""REAL DSH runtime control smoke: start -> status -> cancel.

Runs only with ACP_REAL_DSH_SMOKE=1. Uses the configured real provider and
cancels a small in-flight generation; no long output is produced on purpose.
"""

from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path

import pytest

from core.execution import ExecutionRequest
from core.runtime import CancelStatus, RuntimeManager, RuntimeStatus

pytestmark = [
    pytest.mark.real_dsh,
    pytest.mark.skipif(
        os.environ.get("ACP_REAL_DSH_SMOKE") != "1",
        reason="real runtime smoke disabled; set ACP_REAL_DSH_SMOKE=1",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDER = os.environ.get("ACP_DSH_PROVIDER", "deepseek-official")
MODEL = os.environ.get("ACP_DSH_MODEL", "deepseek-chat")
DSH_HOME = os.environ.get("DSH_HOME", str(Path.home() / ".dsh"))


def adapter():
    from adapters.dsh_execution import DSHExecutionAdapter

    return DSHExecutionAdapter(
        provider=PROVIDER,
        model=MODEL,
        cwd=str(REPO_ROOT),
        timeout_seconds=180.0,
        env_extra={"DSH_PERMISSION_MODE": "read-only"},
    )


def session_log_counts(session_id: str) -> dict[str, int]:
    patterns = [
        str(Path(DSH_HOME) / "sessions" / "**" / session_id / "session.v4.jsonl*.zstd"),
        str(Path(DSH_HOME) / "sessions" / "**" / session_id / "session.v4.jsonl"),
    ]
    files: list[str] = []
    for pattern in patterns:
        files.extend(glob.glob(pattern, recursive=True))
    if not files:
        return {}
    path = Path(sorted(files)[-1])
    if path.suffix == ".zstd":
        import zstandard

        data = zstandard.ZstdDecompressor().stream_reader(path.open("rb")).read()
    else:
        data = path.read_bytes()
    counts: dict[str, int] = {}
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if isinstance(kind, str):
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def test_real_start_status_cancel() -> None:
    impl = adapter()
    manager = RuntimeManager({"dsh": impl}, cancel_timeout=30.0)
    record = manager.start(
        capability_id="dsh",
        request=ExecutionRequest(
            invocation_id="real-runtime-cancel-1",
            task_id="real-runtime-cancel",
            objective=(
                "Write a 900-word detailed essay about the history of computing. "
                "Do not use any tools."
            ),
        ),
    )
    assert record.execution_id
    print("REAL_RUNTIME_START execution_id=", record.execution_id)

    # Wait until the runtime exists and the prompt has been accepted, i.e. a
    # real generation is in flight, then give it a short window.
    deadline = time.monotonic() + 30
    running = None
    while time.monotonic() < deadline:
        running = manager.status(record.execution_id)
        internal = impl.get_execution(record.execution_id)
        if (
            running is not None
            and running.status is RuntimeStatus.RUNNING
            and internal is not None
            and internal.get("message_id")
        ):
            break
        time.sleep(0.1)
    assert running is not None and running.status is RuntimeStatus.RUNNING
    assert impl.get_execution(record.execution_id) is not None
    print("REAL_RUNTIME_STATUS RUNNING message_id=",
          impl.get_execution(record.execution_id).get("message_id"))
    session_id = impl.get_execution(record.execution_id).get("session_id")

    time.sleep(0.6)
    result = manager.cancel(record.execution_id)
    print("REAL_RUNTIME_CANCEL result=", result.to_dict())
    assert result.status is CancelStatus.CANCELLED
    assert result.confirmed is True
    assert result.runtime_status is RuntimeStatus.STOPPED

    final = manager.status(record.execution_id)
    assert final is not None and final.status is RuntimeStatus.STOPPED
    internal = impl.get_execution(record.execution_id)
    assert internal is not None and internal["status"] == "TERMINATED"
    pid = internal.get("runtime_pid")
    if pid:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    print("REAL_RUNTIME_PID_DEAD=", bool(pid))

    time.sleep(0.5)
    counts = session_log_counts(session_id or "")
    print("REAL_PROVIDER_EVIDENCE counts=", counts)
    # In-flight evidence is reported, not asserted: persistence flushing may
    # lag a kill. REAL_PROVIDER_CANCEL stays UNVERIFIED unless turn/start is
    # present without a completed turn/end.
    if counts.get("turn/start") and not counts.get("turn/end"):
        print("REAL_PROVIDER_CANCEL_EVIDENCE in-flight turn started, no turn/end")
    else:
        print("REAL_PROVIDER_CANCEL_EVIDENCE insufficient")
