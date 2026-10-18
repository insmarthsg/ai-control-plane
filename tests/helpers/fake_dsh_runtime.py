"""A fake DSH SDK JSON-RPC stdio peer for protocol tests.

This is NOT a DSH implementation and must never be presented as one: it only
speaks the SDK wire shapes so the bridge client can be tested without a real
provider. Cross-process continuation is simulated with a JSON store directory
passed via FAKE_STORE_DIR.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def notify(method, params):
    emit({"jsonrpc": "2.0", "method": method, "params": params})


def reply(rid, result):
    emit({"jsonrpc": "2.0", "id": rid, "result": result})


def error(rid, message, code=-32000):
    emit({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def store_path(session_id):
    root = Path(os.environ["FAKE_STORE_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{session_id}.json"


def load(session_id):
    path = store_path(session_id)
    if path.exists():
        return json.loads(path.read_text())
    return None


def save(session_id, state):
    store_path(session_id).write_text(json.dumps(state))


def prompt_text(params):
    blocks = params.get("contentBlocks") or []
    return "".join(block.get("text", "") for block in blocks if isinstance(block, dict))


def assistant_event(session_id, text):
    return {
        "type": "assistant/message",
        "data": {
            "message": {
                "id": f"assistant_{session_id}_{int(time.time() * 1000)}",
                "content": [{"type": "text", "text": text}],
                "source": {"replayState": {"response": {"responseId": "resp_fake_0001"}}},
            },
            "usage": {"inputTokens": 12, "outputTokens": 6, "totalTokens": 18},
        },
    }


def main():
    counter = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = frame.get("id")
        method = frame.get("method")
        params = frame.get("params") or {}
        counter += 1

        if method == "initialize":
            reply(rid, {"serverInfo": {"name": "fake-dsh-runtime", "version": "0.0.0-test"}})
        elif method == "session/prompt":
            session_id = str(params.get("sessionId"))
            text = prompt_text(params)
            state = load(session_id) or {"turns": 0, "resumed": False}
            turns_before = state["turns"]
            state["turns"] = turns_before + 1
            save(session_id, state)

            notify("session.status", {"sessionId": session_id, "status": "running"})
            if "SLOW_WAIT" in text:
                # Never emits turn/end: the caller must time out or terminate.
                reply(rid, {"messageId": f"msg_{counter}"})
                continue
            if "TRIGGER_PROVIDER_FAILURE" in text:
                state["failed"] = True
                save(session_id, state)
                notify("session.event", {"sessionId": session_id, "event": {
                    "type": "llm/retry",
                    "data": {"provider": "fake", "failure": {
                        "code": "RATE_LIMIT", "message": "simulated rate limit",
                        "providerRetryAfterMs": 30000}}},
                })
                notify("session.event", {"sessionId": session_id, "event": {
                    "type": "turn/end",
                    "data": {"reason": {"kind": "error", "error": {
                        "code": "RATE_LIMIT", "message": "simulated rate limit"}}}},
                })
                reply(rid, {"messageId": f"msg_{counter}"})
                continue

            if "MARK_COMPLETED" in text:
                body = "demo output\nACP_EXECUTION_STATUS=COMPLETED"
            else:
                body = "progress output"
            if turns_before > 0 or state["resumed"]:
                body += f"\nPREV_TURNS:{turns_before}"
            if state["resumed"]:
                body += "\nRESUMED:true"
            body += f"\nSESSION_ECHO:{session_id}"
            body += f"\nCWD:{os.getcwd()}"

            notify("session.event", {"sessionId": session_id, "event": assistant_event(session_id, body)})
            notify("session.event", {"sessionId": session_id, "event": {
                "type": "turn/end", "data": {"reason": {"kind": "completed"}}}})
            notify("session.status", {"sessionId": session_id, "status": "idle"})
            reply(rid, {"messageId": f"msg_{counter}"})
        elif method == "session/resume":
            session_id = str(params.get("sessionId"))
            state = load(session_id)
            if state is None:
                error(rid, f"unknown session: {session_id}")
            else:
                state["resumed"] = True
                save(session_id, state)
                reply(rid, {})
        elif method == "session/wait":
            session_id = str(params.get("sessionId"))
            state = load(session_id)
            if state and state.get("failed"):
                error(rid, "agent runtime error: simulated rate limit")
            elif state and state.get("slow"):
                time.sleep(120)
                reply(rid, {})
            else:
                reply(rid, {})
        elif method == "shutdown":
            reply(rid, {})
            sys.stdout.flush()
            return
        else:
            error(rid, f"method not found: {method}", -32601)


if __name__ == "__main__":
    main()
