"""
CLEAR_DUPLICATE.PY
===================
Utility to clear duplicate-tracking memory so you can re-test with a file
you already processed before.

The app tracks duplicates two ways:
  1. By filename - checks if a file with the same name exists in
     data_archive/txt/
  2. By content hash - checks processed_files.json (and rebuilds from
     data_archive/txt/ on every startup)

To truly let a file be reprocessed, you need to remove it from
data_archive/txt/ (and optionally its CSV from data_archive/csv/) AND
remove its hash from processed_files.json.

USAGE:

  Clear by filename (recommended - clears exactly one file):
    python clear_duplicate.py "13994464.20260614.00001.TXT"

  Clear EVERYTHING (every file can be reprocessed again):
    python clear_duplicate.py --all

This only touches the archive copies and the hash log - it does NOT
delete anything from input/ or output/.
"""

import json
import sys

import config
import app


def clear_one(filename: str) -> None:
    """Remove one file's archive copies and hash entry."""
    # Find and remove matching archived TXT (handles the date-prefix and
    # duplicate_ prefix naming used by app.py).
    removed_any = False

    for pattern in (f"*_{filename}", f"*duplicate_{filename}"):
        for path in config.ARCHIVE_TXT_DIR.glob(pattern):
            print(f"Removing archived TXT: {path.name}")
            path.unlink()
            removed_any = True

    # Remove matching archived CSV (same stem, .csv extension).
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    for pattern in (f"*_{stem}.csv", f"*duplicate_{stem}.csv"):
        for path in config.ARCHIVE_CSV_DIR.glob(pattern):
            print(f"Removing archived CSV: {path.name}")
            path.unlink()
            removed_any = True

    # Remove from processed_files.json by recomputing without this file.
    # Since hashes don't store filenames, the safest approach is to just
    # rebuild processed_files.json fresh from whatever remains in archive.
    remaining_hashes = app.build_hashes_from_archive()
    app.save_processed_hashes(remaining_hashes)
    print(f"Rebuilt processed_files.json from remaining archive ({len(remaining_hashes)} hash(es)).")

    if not removed_any:
        print(f"No archived copies found matching '{filename}'. "
              f"It may not have been processed yet, or the name doesn't match exactly.")
    else:
        print(f"\nDone. '{filename}' can now be reprocessed.")


def clear_all() -> None:
    """Wipe all archived TXT/CSV files and the processed hash log."""
    count_txt = 0
    count_csv = 0

    for path in config.ARCHIVE_TXT_DIR.glob("*.txt"):
        path.unlink()
        count_txt += 1

    for path in config.ARCHIVE_CSV_DIR.glob("*.csv"):
        path.unlink()
        count_csv += 1

    app.save_processed_hashes(set())

    print(f"Removed {count_txt} archived TXT file(s) and {count_csv} archived CSV file(s).")
    print("processed_files.json cleared.")
    print("\nALL files can now be reprocessed from scratch.")


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    arg = sys.argv[1]

    if arg == "--all":
        confirm = input(
            "This will clear ALL processed file history (every TXT/CSV in "
            "data_archive/ will be deleted). Type 'yes' to confirm: "
        )
        if confirm.strip().lower() == "yes":
            clear_all()
        else:
            print("Cancelled.")
    else:
        clear_one(arg)


if __name__ == "__main__":
    main()
