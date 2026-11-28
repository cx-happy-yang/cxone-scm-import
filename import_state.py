"""Persistent per-unit (e.g. per GitLab group) import state, and a retry-until-deadline runner.

The state file survives crashes and Ctrl-C: it is rewritten atomically after every unit, so a restart skips the units
that are already done. Units run strictly one after another (CxOne imports must not run in parallel).
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional

logger = logging.getLogger("CheckmarxPythonSDK")

PENDING, IN_PROGRESS, DONE, FAILED = "pending", "in_progress", "done", "failed"


class PermanentImportError(Exception):
    """An error that retrying cannot fix (for example an expired auth code). The run stops; state is kept."""


class ImportState:
    def __init__(self, path: str):
        self.path = path
        self.units: Dict[str, dict] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.units = json.load(f).get("units", {})

    def save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"units": self.units}, f, indent=2)
        os.replace(tmp, self.path)

    def add_pending(self, keys: Iterable[str]) -> None:
        for key in keys:
            self.units.setdefault(key, {"status": PENDING, "attempts": 0})
        self.save()

    def status(self, key: str) -> str:
        return self.units[key]["status"]

    def mark_in_progress(self, key: str) -> None:
        """Written before a unit starts. If the program dies mid-unit, the next run finds this marker."""
        self.units[key].update(status=IN_PROGRESS, started_at=_iso())
        self.save()

    def mark_done(self, key: str, **info) -> None:
        unit = self.units[key]
        unit.update(status=DONE, next_retry_at=None, last_error=None, finished_at=_iso(), **info)
        self.save()

    def mark_failed(self, key: str, error: str, retry_in: Optional[float], now: float) -> None:
        unit = self.units[key]
        unit.update(
            status=FAILED, attempts=unit["attempts"] + 1, last_error=error[:2000], failed_at=_iso(),
            next_retry_at=None if retry_in is None else now + retry_in,
        )
        self.save()

    def unfinished(self, keys: Iterable[str]) -> List[str]:
        return [k for k in keys if self.units[k]["status"] != DONE]

    def due(self, keys: Iterable[str], now: float) -> List[str]:
        return [
            k for k in keys
            if self.units[k]["status"] in (PENDING, IN_PROGRESS)
            or (self.units[k]["status"] == FAILED and (self.units[k].get("next_retry_at") or 0) <= now)
        ]

    def next_retry_at(self, keys: Iterable[str]) -> Optional[float]:
        times = [self.units[k].get("next_retry_at") for k in keys if self.units[k]["status"] == FAILED]
        times = [t for t in times if t is not None]
        return min(times) if times else None

    def summary(self, keys: Iterable[str]) -> Dict[str, int]:
        counts = {PENDING: 0, IN_PROGRESS: 0, DONE: 0, FAILED: 0}
        for k in keys:
            counts[self.units[k]["status"]] += 1
        return counts


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def backoff_seconds(attempts: int, initial: float = 300, maximum: float = 3600) -> float:
    """5 min, 10 min, 20 min, ... capped at 1 hour (attempts = failures so far, starting at 1)."""
    return min(maximum, initial * 2 ** max(attempts - 1, 0))


def run_until_done(
    keys: List[str],
    process: Callable[[str], Optional[dict]],
    state: ImportState,
    deadline_seconds: float,
    is_permanent: Callable[[Exception], bool] = lambda e: False,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    retry_initial: float = 300,
    retry_max: float = 3600,
    on_interrupted: Callable[[str], None] = lambda key: None,
) -> bool:
    """Process the units one after another; a failed unit is retried with growing backoff once its
    retry time is due, until all are done or the deadline passes.

    process(key) returns an optional dict stored with the unit; it raises on failure.
    A unit found in_progress at the start was cut off by a crash or kill of an earlier run; on_interrupted(key) is
    called before it is processed again, e.g. to wait until the server has finished the cut-off job.
    Returns True when every unit is done. Raises PermanentImportError if is_permanent(exc) holds.
    """
    deadline = now() + deadline_seconds
    state.add_pending(keys)
    total = len(keys)
    while state.unfinished(keys) and now() < deadline:
        due = state.due(keys, now())
        if not due:
            wake = state.next_retry_at(keys)
            wait = max(1.0, min((wake or deadline) - now(), deadline - now()))
            logger.info(f"waiting {wait:.0f}s before retrying {len(state.unfinished(keys))} failed unit(s)")
            sleep(wait)
            continue
        key = due[0]
        done_so_far = state.summary(keys)[DONE]
        logger.info(f"[{done_so_far + 1}/{total}] {key}")
        if state.status(key) == IN_PROGRESS:
            logger.warning(f"{key} was interrupted by an earlier run, checking before starting it again")
            on_interrupted(key)
        state.mark_in_progress(key)
        try:
            info = process(key) or {}
        except Exception as e:
            permanent = is_permanent(e)
            attempts = state.units[key]["attempts"] + 1
            retry_in = None if permanent else backoff_seconds(attempts, retry_initial, retry_max)
            state.mark_failed(key, f"{type(e).__name__}: {e}", retry_in, now())
            if permanent:
                raise PermanentImportError(f"{key}: {e}") from e
            logger.exception(f"{key} failed (attempt {attempts}), retry in {retry_in:.0f}s")
        else:
            state.mark_done(key, **info)
    return not state.unfinished(keys)
