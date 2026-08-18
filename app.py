"""
APP.PY
======
Run this file to start the converter. It does everything in this order:

    1. Watches the 'input' folder for new .txt files every few seconds.
    2. When a new .txt file is found:
        a. Check if this exact file was already processed (duplicate check)
        b. Convert it to CSV
        c. Archive both TXT and CSV to data_archive/
        d. Keep working copies of both in output/
        e. Open Chrome + WhatsApp Web (if not already open)
        f. Send the raw TXT file first, then the CSV (two separate
           messages) to your group
        g. Close browser when no more files are pending
    3. Duplicates (same file dropped again, or same content in a new file)
       are detected and skipped automatically.

HOW TO RUN:
    python app.py

Press Ctrl+C to stop.
"""

import hashlib
import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

import config
import converter
import whatsapp_sender
import alerts


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    logging.basicConfig(level=config.LOG_LEVEL, format=config.LOG_FORMAT)


# ---------------------------------------------------------------------------
# FOLDERS
# ---------------------------------------------------------------------------

def setup_folders() -> None:
    for folder in (
        config.INPUT_DIR,
        config.OUTPUT_DIR,
        config.ARCHIVE_TXT_DIR,
        config.ARCHIVE_CSV_DIR,
    ):
        folder.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# DUPLICATE TRACKING
# We store the MD5 hash of every successfully processed TXT file in
# processed_files.json (in the project root). This catches:
#   - Same file dropped into input/ again (same name, same content)
#   - Same content in a file with a different name
# ---------------------------------------------------------------------------

PROCESSED_LOG = config.BASE_DIR / "processed_files.json"


def load_processed_hashes() -> set:
    """Load the set of MD5 hashes of all previously processed TXT files."""
    if not PROCESSED_LOG.exists():
        return set()
    try:
        with PROCESSED_LOG.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("hashes", []))
    except Exception:
        logging.warning("Could not read %s; starting with empty duplicate tracker.", PROCESSED_LOG.name)
        return set()


def save_processed_hashes(hashes: set) -> None:
    """Save the current set of processed hashes to disk."""
    try:
        with PROCESSED_LOG.open("w", encoding="utf-8") as f:
            json.dump({"hashes": sorted(hashes)}, f, indent=2)
    except Exception:
        logging.warning("Could not save processed file log.")


def md5_of_file(path: Path) -> str:
    """Return the MD5 hex digest of a file's contents."""
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_duplicate(txt_path: Path, processed_hashes: set) -> tuple:
    """Check if a TXT file was already processed.

    Two checks:
      1. Filename match — if any file with the same name already exists
         in data_archive/txt/ it was processed before.
      2. Content hash match — same content even under a different filename.

    Returns:
        (is_dup: bool, file_hash: str)
    """
    # Check 1: same filename already in archive
    existing_in_archive = list(config.ARCHIVE_TXT_DIR.glob(f"*_{txt_path.name}"))
    if existing_in_archive:
        logging.warning(
            "Skipping %s — a file with this name was already processed (%s).",
            txt_path.name, existing_in_archive[0].name
        )
        return True, ""

    # Check 2: same content (MD5)
    file_hash = md5_of_file(txt_path)
    if file_hash in processed_hashes:
        logging.warning(
            "Skipping %s — identical content was already processed.",
            txt_path.name
        )
        return True, file_hash

    return False, file_hash


# ---------------------------------------------------------------------------
# CONVERSION + ARCHIVING
# ---------------------------------------------------------------------------

def build_hashes_from_archive() -> set:
    """Scan data_archive/txt/ and compute MD5 hashes of all archived TXT
    files. This is used on startup so the app remembers what was processed
    in previous runs, even if processed_files.json is missing or empty."""
    hashes = set()
    archived = list(config.ARCHIVE_TXT_DIR.glob("*.txt"))
    if not archived:
        return hashes
    logging.info("Building duplicate tracker from %d archived file(s)...", len(archived))
    for path in archived:
        try:
            hashes.add(md5_of_file(path))
        except Exception:
            pass
    return hashes


def archive_filename(original_name: str) -> str:
    """Prefix a filename with today's date: 'file.txt' -> '20260614_file.txt'"""
    return f"{datetime.now().strftime('%Y%m%d')}_{original_name}"


def convert_and_archive(txt_path: Path, bank_mapping: dict) -> tuple:
    """Convert TXT -> CSV, archive both, and keep copies in output/ for
    sending via WhatsApp.

    Returns:
        (txt_output_path, csv_output_path) - both Path objects in output/,
        or (None, None) on failure.
    """
    logging.info("Converting %s ...", txt_path.name)

    csv_path = config.OUTPUT_DIR / f"{txt_path.stem}.csv"

    try:
        row_count = converter.convert_txt_to_csv(txt_path, csv_path, bank_mapping)
        logging.info("Converted -> %s (%d row(s))", csv_path.name, row_count)
    except Exception:
        logging.exception("Conversion failed for %s", txt_path.name)
        return None, None

    # Keep a copy of the original TXT in output/ too, so it can be sent
    # alongside the CSV. The original in input/ gets archived (moved) below.
    txt_output_path = config.OUTPUT_DIR / txt_path.name
    try:
        shutil.copy2(txt_path, txt_output_path)
    except Exception:
        logging.exception("Could not copy %s to output/", txt_path.name)
        return None, None

    try:
        shutil.copy2(csv_path, config.ARCHIVE_CSV_DIR / archive_filename(csv_path.name))
        shutil.move(str(txt_path), str(config.ARCHIVE_TXT_DIR / archive_filename(txt_path.name)))
        logging.info("Archived TXT and CSV copies.")
    except Exception:
        logging.exception("Archiving failed for %s", txt_path.name)
        return None, None

    return txt_output_path, csv_path


# ---------------------------------------------------------------------------
# OPERATIONAL ALERTS (queued by scheduler.py / downloader.py — separate
# process — and sent here since this process owns the live WhatsApp
# session). See alerts.py.
# ---------------------------------------------------------------------------

def send_pending_alerts(session: whatsapp_sender.WhatsAppSession) -> None:
    pending = alerts.dequeue_alerts()
    if not pending:
        return

    if not session.is_ready():
        logging.info("Opening WhatsApp Web to send %d alert(s)...", len(pending))
        if not session.start():
            logging.warning("WhatsApp not available — re-queuing alert(s) for retry.")
            alerts.requeue_alerts(pending)
            return

    failed = []
    for item in pending:
        if not session.send_text(item["message"]):
            failed.append(item)

    if failed:
        alerts.requeue_alerts(failed)


# ---------------------------------------------------------------------------
# WHATSAPP SENDING
# ---------------------------------------------------------------------------

def send_pending_files(session: whatsapp_sender.WhatsAppSession) -> None:
    """Send all TXT+CSV pairs waiting in output/ and delete each file after
    it's successfully sent.

    For each CSV in output/, the matching TXT (same stem) is sent FIRST,
    then the CSV — two separate messages. If a TXT has no matching CSV
    (or vice versa), it's sent on its own.
    """
    pending_csvs = sorted(config.OUTPUT_DIR.glob("*.csv"))
    pending_txts = sorted(config.OUTPUT_DIR.glob("*.txt"))

    if not pending_csvs and not pending_txts:
        return

    total = len(pending_csvs) + len(pending_txts)

    # Open the browser only now that files are ready.
    if not session.is_ready():
        logging.info("Opening WhatsApp Web to send %d file(s)...", total)
        if not session.start():
            logging.warning("WhatsApp not available — files stay in output/ for retry.")
            return

    # Build pairs by matching filename stem (e.g. "remit001.txt" <-> "remit001.csv").
    csv_by_stem = {p.stem: p for p in pending_csvs}
    txt_by_stem = {p.stem: p for p in pending_txts}
    all_stems = sorted(set(csv_by_stem) | set(txt_by_stem))

    for stem in all_stems:
        txt_path = txt_by_stem.get(stem)
        csv_path = csv_by_stem.get(stem)

        # Send TXT first, if present.
        if txt_path is not None:
            if session.send(txt_path):
                try:
                    txt_path.unlink()
                except OSError:
                    logging.exception("Could not remove %s from output/", txt_path.name)
            else:
                logging.warning("%s not sent — will retry next cycle.", txt_path.name)

        # Then send CSV, if present.
        if csv_path is not None:
            if session.send(csv_path):
                try:
                    csv_path.unlink()
                except OSError:
                    logging.exception("Could not remove %s from output/", csv_path.name)
            else:
                logging.warning("%s not sent — will retry next cycle.", csv_path.name)


# ---------------------------------------------------------------------------
# MAIN WATCH LOOP
# ---------------------------------------------------------------------------

def watch_and_process() -> None:
    bank_mapping = converter.load_bank_mapping()

    # Load saved hashes AND rebuild from archive — whichever has more data wins.
    # This ensures duplicate detection survives app restarts and
    # processed_files.json being deleted.
    saved_hashes = load_processed_hashes()
    archive_hashes = build_hashes_from_archive()
    processed_hashes = saved_hashes | archive_hashes

    if len(processed_hashes) > len(saved_hashes):
        # Persist the merged set so future startups are faster.
        save_processed_hashes(processed_hashes)

    session = whatsapp_sender.WhatsAppSession()

    logging.info(
        "Watching '%s' for new .txt files every %d seconds ...",
        config.INPUT_DIR, config.WATCH_INTERVAL_SECONDS
    )
    logging.info("Tracking %d previously processed file(s).", len(processed_hashes))

    # Open WhatsApp Web once, right away, and keep it open for the whole
    # run — rather than opening/closing it around each send. If this
    # fails at startup, send_pending_alerts()/send_pending_files() still
    # try to open it lazily later, so it's not fatal.
    logging.info("Opening WhatsApp Web...")
    if not session.start():
        logging.warning(
            "Could not open WhatsApp Web at startup — will keep retrying "
            "whenever there's something to send."
        )

    logging.info("Press Ctrl+C to stop.\n")

    try:
        while True:
            txt_files = sorted(config.INPUT_DIR.glob("*.txt"))

            for txt_path in txt_files:
                dup, file_hash = is_duplicate(txt_path, processed_hashes)

                if dup:
                    # Move duplicate out of input so it doesn't keep triggering.
                    try:
                        date_prefix = datetime.now().strftime('%Y%m%d')
                        dest = config.ARCHIVE_TXT_DIR / f"{date_prefix}_duplicate_{txt_path.name}"
                        shutil.move(str(txt_path), str(dest))
                        logging.info("Moved duplicate to archive: %s", dest.name)
                    except Exception:
                        logging.exception("Could not move duplicate %s", txt_path.name)
                    continue

                txt_output_path, csv_path = convert_and_archive(txt_path, bank_mapping)
                if csv_path is None:
                    continue  # conversion failed — leave in input for retry

                # Mark as processed only after successful conversion + archive.
                processed_hashes.add(file_hash)
                save_processed_hashes(processed_hashes)

            # Send any operational alerts queued by scheduler.py/downloader.py
            # (LAN down, portal down, no file found, etc.)
            send_pending_alerts(session)

            # Send everything waiting in output/ (new + retry failures).
            send_pending_files(session)

            # WhatsApp Web is kept open for the whole run (see startup
            # above) rather than being closed here when idle, so it
            # doesn't need to be re-logged-in (QR scan) every cycle.

            time.sleep(config.WATCH_INTERVAL_SECONDS)

    finally:
        session.close()


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    setup_logging()
    setup_folders()
    try:
        watch_and_process()
    except KeyboardInterrupt:
        logging.info("Stopped.")
