"""
TEST_SPECIFIC_FILES.PY
=======================
Tests the downloader against KNOWN files that should already exist, so
you can verify the login + download logic works before relying on the
auto-detection of new files.

USAGE:
    python test_specific_files.py YYYYMMDD START_SEQ END_SEQ

EXAMPLE (test yesterday's last 2 files, if you know they were sequence
18 and 19):
    python test_specific_files.py 20260613 18 19

If you don't know the exact sequence numbers, just guess a wide range
like 1 to 25 — the script will show which ones exist and which don't.
"""

import logging
import sys

logging.basicConfig(level="INFO", format="%(asctime)s - %(levelname)s - %(message)s")

import downloader
import config


def main():
    if len(sys.argv) != 4:
        print("Usage: python test_specific_files.py YYYYMMDD START_SEQ END_SEQ")
        print("Example: python test_specific_files.py 20260613 18 19")
        sys.exit(1)

    date_str = sys.argv[1]
    start_seq = int(sys.argv[2])
    end_seq = int(sys.argv[3])

    print(f"\nTesting download for date={date_str}, sequences {start_seq} to {end_seq}")
    print(f"Files will be saved to: {config.INPUT_DIR}\n")

    driver = downloader._create_browser()
    if driver is None:
        print("Could not start Chrome.")
        return

    try:
        if not downloader._login(driver):
            print("Login failed. Check credentials in config.py.")
            return

        print("Login successful.\n")

        session = downloader._build_session_from_driver(driver)

        for seq in range(start_seq, end_seq + 1):
            result = downloader._download_file(driver, date_str, seq, session=session)
            print(f"  Sequence {seq:05d} -> {result}")

        print("\nDone. Check the input/ folder for downloaded files.")
        print(f"If any result was 'failed' or unexpected, check "
              f"{downloader.DEBUG_LOG_DIR} for the raw HTTP response saved "
              f"for that check.")
        print("Leaving browser open for 15 seconds so you can inspect it...")

        import time
        time.sleep(15)

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
