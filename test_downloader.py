"""
TEST_DOWNLOADER.PY
==================
Run this to test the Bank Albilad login and file download
WITHOUT waiting for a scheduled time slot.

    python test_downloader.py

Watch the Chrome window that opens — it will:
  1. Open the login page
  2. Fill in credentials and log in
  3. Navigate to the Esewa folder
  4. Download any new TXT files to input/

If it fails at any step, the error is printed here so it can be fixed.
"""

import logging
logging.basicConfig(level="INFO", format="%(asctime)s - %(levelname)s - %(message)s")

import downloader

print("Testing Bank Albilad downloader...")
print("A Chrome window will open — watch what happens.\n")

count = downloader.check_and_download()

print(f"\nResult: {count} file(s) downloaded to input/")
if count == 0:
    print("If login failed, check DOWNLOAD_USERNAME and DOWNLOAD_PASSWORD in config.py.")
    print("If login worked but no files found, there may be no new files today yet.")
