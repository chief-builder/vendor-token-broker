"""Audit events (design §11): one JSON line per event on stdout.

Token material MUST never be passed to audit() — callers log ids, states,
and generations only. Event names are wire contract (SIEM joins on them).
"""
import json
import time


def audit(event: str, **fields) -> None:
    print(json.dumps({"audit": event, "ts": time.time(), **fields}), flush=True)
