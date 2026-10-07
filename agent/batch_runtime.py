"""Bound batch subprocess lifetimes without blocking on their stdout."""
from __future__ import annotations

import threading
import time


def check_scan_budget(job: dict) -> None:
    deadline = job.get("deadline_monotonic")
    if job.get("cancel_requested") or (deadline and time.monotonic() >= deadline):
        job["cancel_requested"] = True
        raise TimeoutError("scan time budget exhausted; completed evidence is retained for retry")


def watch_process(proc, job: dict, done: threading.Event) -> threading.Thread:
    def watch():
        while not done.wait(0.25):
            try:
                check_scan_budget(job)
            except TimeoutError:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                return

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread
