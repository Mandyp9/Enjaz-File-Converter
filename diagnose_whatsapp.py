"""
DIAGNOSE_WHATSAPP.PY
====================
A standalone helper to figure out why whatsapp_sender.py can't find the
search box, your group, or the attach/send buttons.

WHAT IT DOES:
    1. Opens WhatsApp Web in Edge (using the same saved login as app.py).
    2. Waits for it to load.
    3. Searches for and opens your configured group.
    4. If a .csv file exists in the output/ folder, tries to attach and
       send it as a document, printing what it finds at each step.
    5. Saves screenshots + prints HTML snippets along the way.

HOW TO RUN:
    python diagnose_whatsapp.py

Read the printed output in the terminal - it will tell you what worked
and what didn't, and show HTML snippets for anything that failed.
"""

import logging
import time
from pathlib import Path

import config
import whatsapp_sender as ws

logging.basicConfig(level="INFO", format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def main():
    if not ws._SELENIUM_AVAILABLE:
        print("selenium is not installed. Run: pip install selenium")
        return

    print("Opening Edge + WhatsApp Web...")
    driver = ws._create_browser()
    if driver is None:
        print("Could not start Edge.")
        return

    try:
        print("Waiting for WhatsApp Web to load (scan QR if asked)...")
        loaded = ws._wait_for_whatsapp_loaded(driver)
        print(f"Loaded: {loaded}")

        if not loaded:
            ws._save_debug_screenshot(driver, "diagnose_after_load")
            return

        print(f"\nTrying to open group chat: '{config.WHATSAPP_GROUP_NAME}'")
        opened = ws._open_group_chat(driver, config.WHATSAPP_GROUP_NAME)
        print(f"Group chat opened: {opened}")

        if not opened:
            ws._save_debug_screenshot(driver, "diagnose_after_group_search")
            return

        ws._save_debug_screenshot(driver, "diagnose_group_opened")

        # Find a .csv file to test sending.
        csv_files = sorted(config.OUTPUT_DIR.glob("*.csv"))
        if not csv_files:
            print(f"\nNo .csv files found in {config.OUTPUT_DIR}.")
            print("Drop a .csv file there and re-run this script to test sending.")
        else:
            test_file = csv_files[0]
            print(f"\nTrying to attach and send: {test_file.name}")
            sent = ws._attach_and_send_document(driver, test_file, f"Test send: {test_file.name}")
            print(f"Attach + send result: {sent}")
            ws._save_debug_screenshot(driver, "diagnose_after_send")

        print("\nLeaving the browser open for 20 seconds so you can look at it yourself...")
        time.sleep(20)

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
