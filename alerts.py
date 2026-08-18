"""
ALERTS.PY
=========
Lightweight file-based queue so scheduler.py / downloader.py (which run
in a SEPARATE process from app.py) can ask for a WhatsApp text alert to
be sent, without needing their own second WhatsApp Web login.

app.py owns the one live WhatsApp browser session and periodically calls
dequeue_alerts() to pick up and actually send anything queued here.

Each alert has a "key" identifying the *kind* of problem (e.g.
"lan_disconnected", "portal_down", "no_file:16:45"). queue_alert() only
actually queues a new message for a given key once every
config.ALERT_COOLDOWN_MINUTES, so an ongoing problem doesn't spam the
group every 30 seconds — but it will remind you periodically for as
long as it persists.

Since scheduler.py (polls every ~30s) and app.py (polls every ~3s) both
read/write these same JSON files from separate processes, reads/writes
use a small retry loop plus an atomic write (temp file + os.replace) so
a read never sees a half-written file, and a transient Windows file-lock
collision doesn't silently drop data.
"""

import json
import logging
import os
import time
import uuid

import config

logger = logging.getLogger(__name__)

_IO_RETRIES = 5
_IO_RETRY_DELAY_SECONDS = 0.15


def _load_json(path, default):
    if not path.exists():
        return default

    last_error = None
    for _ in range(_IO_RETRIES):
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            last_error = e
            time.sleep(_IO_RETRY_DELAY_SECONDS)

    logger.warning("Could not read %s after retries (%s) — using default.", path.name, last_error)
    return default


def _save_json(path, data) -> bool:
    """Atomic write (temp file + os.replace) so a concurrent reader
    never sees a partially-written file, with a few retries in case the
    other process has the file open at the exact same instant. Returns
    True on success, False if it never got through."""
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")

    last_error = None
    for _ in range(_IO_RETRIES):
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, path)  # atomic on both Windows and POSIX
            return True
        except Exception as e:
            last_error = e
            time.sleep(_IO_RETRY_DELAY_SECONDS)

    try:
        if tmp_path.exists():
            tmp_path.unlink()
    except Exception:
        pass

    logger.exception("Could not write %s after retries: %s", path.name, last_error)
    return False


def queue_alert(key: str, message: str) -> None:
    """Queue a WhatsApp text alert for `key`, unless one was already
    queued/sent for the same key within the cooldown window.

    The cooldown is only marked as "sent" if the message was actually
    written to the queue file — if the write fails, nothing is marked,
    so the very next call (e.g. the next scheduler tick) retries instead
    of the alert being silently lost for the whole cooldown window.
    """
    cooldowns = _load_json(config.ALERTS_STATE_FILE, {})
    last_sent = cooldowns.get(key, 0)
    cooldown_seconds = config.ALERT_COOLDOWN_MINUTES * 60

    if time.time() - last_sent < cooldown_seconds:
        return  # still within cooldown for this issue - don't spam

    queue = _load_json(config.ALERTS_QUEUE_FILE, [])
    queue.append({"key": key, "message": message, "queued_at": time.time()})
    wrote_ok = _save_json(config.ALERTS_QUEUE_FILE, queue)

    if not wrote_ok:
        logger.warning(
            "Alert [%s] could not be queued (write failed) — will retry next check, "
            "not marking cooldown.", key
        )
        return

    cooldowns[key] = time.time()
    _save_json(config.ALERTS_STATE_FILE, cooldowns)

    logger.info("Queued WhatsApp alert [%s]: %s", key, message)


def clear_cooldown(key: str) -> None:
    """Call when an issue is resolved, so if it happens again soon after
    it alerts right away instead of waiting out the previous cooldown."""
    cooldowns = _load_json(config.ALERTS_STATE_FILE, {})
    if key in cooldowns:
        del cooldowns[key]
        _save_json(config.ALERTS_STATE_FILE, cooldowns)


def requeue_alerts(items: list) -> None:
    """Put previously-dequeued alert items back on the queue as-is (no
    cooldown check — they already passed it once). Used when a send
    attempt fails so the alert isn't silently lost."""
    if not items:
        return
    queue = _load_json(config.ALERTS_QUEUE_FILE, [])
    queue.extend(items)
    if not _save_json(config.ALERTS_QUEUE_FILE, queue):
        logger.error("Could not re-queue %d alert(s) after a failed send — they may be lost: %s", len(items), items)


def has_pending() -> bool:
    """True if there are alerts waiting to be sent."""
    return bool(_load_json(config.ALERTS_QUEUE_FILE, []))


def dequeue_alerts() -> list:
    """Pop and return all pending alert messages (clearing the queue).
    Used by app.py, which actually has a live WhatsApp session.

    If clearing the queue fails, the alerts are NOT returned (so the
    caller doesn't send them and then fail to clear them, which would
    send the same message twice on the next poll) — they stay queued
    and get picked up on the next call instead.
    """
    queue = _load_json(config.ALERTS_QUEUE_FILE, [])
    if not queue:
        return []
    if not _save_json(config.ALERTS_QUEUE_FILE, []):
        logger.warning("Could not clear alert queue after reading it — will retry next check.")
        return []
    return queue
