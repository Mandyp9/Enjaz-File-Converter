"""
SCHEDULER.PY
============
Runs in the background alongside app.py. At each scheduled time slot
(defined in config.DOWNLOAD_SCHEDULE), it logs into the Bank Albilad
portal and downloads any new TXT files into the input/ folder.

app.py watches input/ and automatically converts + sends any file that
appears there, so the two scripts work together:

    scheduler.py  →  downloads TXT into input/
    app.py        →  converts TXT to CSV, sends via WhatsApp

HOW TO RUN BOTH TOGETHER:
    Open two terminal windows:
        Terminal 1:  python app.py
        Terminal 2:  python scheduler.py

Or run both in one terminal using:
    Windows:  start python app.py && python scheduler.py
    (or just run them in separate terminals — easier to read logs)

SCHEDULE:
    Files are expected at the times in config.DOWNLOAD_SCHEDULE.
    The scheduler checks every 30 seconds, and within each slot it
    keeps retrying for config.DOWNLOAD_WINDOW_MINUTES (5 min) until
    a file is found.
"""

import logging
import time
from datetime import datetime, timedelta

import config
import downloader
import alerts

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(level=config.LOG_LEVEL, format=config.LOG_FORMAT)


def parse_schedule() -> list:
    """Parse config.DOWNLOAD_SCHEDULE strings into (hour, minute) tuples."""
    slots = []
    for t in config.DOWNLOAD_SCHEDULE:
        try:
            h, m = map(int, t.strip().split(":"))
            slots.append((h, m))
        except Exception:
            logger.warning("Invalid schedule entry '%s' — skipping.", t)
    return slots


def is_in_download_window(now: datetime, schedule: list) -> bool:
    """Return True if `now` is within DOWNLOAD_WINDOW_MINUTES of any
    scheduled slot."""
    window = timedelta(minutes=config.DOWNLOAD_WINDOW_MINUTES)

    for hour, minute in schedule:
        # Build today's slot time (or yesterday's for overnight slots like 01:46).
        slot_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # Handle slots that cross midnight (e.g. 01:46 AM when it's still
        # evening — check yesterday's 01:46 is not relevant, but also check
        # if today's 01:46 is coming up or just passed).
        if slot_today <= now <= slot_today + window:
            return True

    return False


def next_slot_time(now: datetime, schedule: list) -> datetime | None:
    """Return the datetime of the next scheduled slot after `now`."""
    window = timedelta(minutes=config.DOWNLOAD_WINDOW_MINUTES)
    candidates = []

    for hour, minute in schedule:
        slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If this slot is in the past (or currently active), look at tomorrow's.
        if slot + window <= now:
            slot = slot + timedelta(days=1)

        candidates.append(slot)

    if not candidates:
        return None

    return min(candidates)


def run_scheduler() -> None:
    """Main scheduler loop. Checks every SCHEDULER_TICK_SECONDS whether
    we're inside a download window, and triggers a download if so."""
    schedule = parse_schedule()
    if not schedule:
        logger.error("No valid schedule entries found in config.py. Exiting.")
        return

    logger.info("Scheduler started with %d time slot(s).", len(schedule))
    logger.info("Download window: %d minutes after each slot.", config.DOWNLOAD_WINDOW_MINUTES)
    logger.info("Press Ctrl+C to stop.\n")

    # Track which slots have already been checked in their window, so we
    # don't re-download every 30 seconds during the 5-minute window.
    # Key: (date, hour, minute) -> True if we already got a file this slot.
    completed_slots: set = set()
    # Slots where at least one REAL portal check happened this window
    # (as opposed to being blocked before ever reaching the portal, e.g.
    # by a LAN outage) — only these are eligible for a "no file found"
    # alert; a LAN/portal-down alert already explains the other case.
    ever_checked_slots: set = set()
    # Slots we've already sent a "no file found" alert for, so we only
    # alert once per slot (not every tick after the window closes).
    alerted_no_file_slots: set = set()

    # Don't fire false "no file found" alerts for windows that already
    # closed BEFORE this process even started (e.g. restarting the
    # scheduler at 11am shouldn't claim the 01:46 slot found no file —
    # we never actually had a chance to check the portal during that
    # window at all). Pre-seed those as already-alerted (i.e. suppressed).
    startup_now = datetime.now()
    skipped_at_startup = []
    for hour, minute in schedule:
        slot_time = startup_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        window_end = slot_time + timedelta(minutes=config.DOWNLOAD_WINDOW_MINUTES)
        if startup_now > window_end:
            alerted_no_file_slots.add((startup_now.date(), hour, minute))
            skipped_at_startup.append(f"{hour:02d}:{minute:02d}")

    if skipped_at_startup:
        logger.info(
            "Started after today's window already closed for: %s — these "
            "will NOT be checked today (their window passed before this "
            "process started). All other slots will be checked normally.",
            ", ".join(skipped_at_startup)
        )

    last_heartbeat = datetime.now() - timedelta(minutes=999)  # force one on first tick

    # Run the LAN/IP/portal-reachability check once immediately, then
    # every config.NETWORK_CHECK_INTERVAL_SECONDS from then on — this is
    # independent of download windows, so problems are caught even during
    # long gaps between scheduled slots.
    downloader.run_periodic_network_check()
    last_network_check = datetime.now()

    while True:
        now = datetime.now()

        if (now - last_network_check).total_seconds() >= config.NETWORK_CHECK_INTERVAL_SECONDS:
            downloader.run_periodic_network_check()
            last_network_check = now

        for hour, minute in schedule:
            slot_key = (now.date(), hour, minute)
            slot_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            window_end = slot_time + timedelta(minutes=config.DOWNLOAD_WINDOW_MINUTES)

            # Window just closed with no file ever found for this slot?
            # Alert once — but only if the portal was actually reachable
            # and checked at some point this window. If it was blocked
            # the whole window (LAN down, portal down), the LAN/portal
            # alert already explains why — reporting "no file found" on
            # top of that would be misleading (we never actually looked).
            if (
                now > window_end
                and slot_key not in completed_slots
                and slot_key not in alerted_no_file_slots
            ):
                alerted_no_file_slots.add(slot_key)
                if slot_key in ever_checked_slots:
                    logger.warning("No file found for %02d:%02d slot (window closed).", hour, minute)
                    alerts.queue_alert(
                        f"no_file:{hour:02d}:{minute:02d}",
                        f"⚠️ No file was found for the {hour:02d}:{minute:02d} time slot "
                        f"(checked every 30s for {config.DOWNLOAD_WINDOW_MINUTES} minutes)."
                    )
                else:
                    logger.warning(
                        "%02d:%02d window closed without ever successfully reaching the "
                        "portal (LAN/portal issue) — skipping 'no file found' alert.",
                        hour, minute
                    )
                continue

            # Are we inside this slot's window?
            if not (slot_time <= now <= window_end):
                continue

            # Already successfully downloaded for this slot?
            if slot_key in completed_slots:
                continue

            logger.info(
                "Inside download window for %02d:%02d — checking portal...",
                hour, minute
            )

            count = downloader.check_and_download()

            if count is None:
                # Blocked before ever reaching the portal (LAN down, IP
                # mismatch, or the check was interrupted partway through).
                # downloader.py already queued the relevant alert — just
                # retry on the next tick without touching ever_checked_slots.
                logger.info(
                    "Could not check slot %02d:%02d this tick (LAN/portal issue) — retrying in %ds.",
                    hour, minute, config.SCHEDULER_TICK_SECONDS
                )
                continue

            ever_checked_slots.add(slot_key)

            if count > 0:
                # Got at least one file — mark slot done.
                completed_slots.add(slot_key)
                logger.info(
                    "Slot %02d:%02d complete — %d file(s) downloaded.",
                    hour, minute, count
                )
            else:
                # No file yet — will retry on next tick (within the window).
                logger.info(
                    "No file yet for slot %02d:%02d — retrying in %ds.",
                    hour, minute, config.SCHEDULER_TICK_SECONDS
                )

        # Heartbeat: confirms the scheduler is alive and shows what it's
        # waiting for during idle periods (previously this was DEBUG-only,
        # so a quiet stretch between slots looked identical to a frozen
        # process). Throttled so it doesn't spam every 30s tick.
        if (now - last_heartbeat).total_seconds() >= config.HEARTBEAT_INTERVAL_SECONDS:
            next_slot = next_slot_time(now, schedule)
            if next_slot:
                minutes_away = max(0, int((next_slot - now).total_seconds() / 60))
                logger.info(
                    "Still running — next slot at %s (in %d min).",
                    next_slot.strftime("%H:%M"), minutes_away
                )
            last_heartbeat = now

        time.sleep(config.SCHEDULER_TICK_SECONDS)


if __name__ == "__main__":
    setup_logging()
    try:
        run_scheduler()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped.")
