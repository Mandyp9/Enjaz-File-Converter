"""
TEST_CONVERTER.PY
==================
Run just the TXT -> CSV conversion logic, no WhatsApp/browser involved at
all — nothing in this script even imports Selenium. Reuses the exact same
convert_and_archive() logic app.py uses, so what you see here is exactly
what production conversion will do.

USAGE
-----
Convert every .txt file currently sitting in input/:

    python test_converter.py

Convert one specific file (doesn't have to be in input/):

    python test_converter.py C:/path/to/some_file.txt

WHAT IT DOES
------------
For each file: converts TXT -> CSV, copies both into output/ (same as
app.py), and archives copies into data_archive/txt and data_archive/csv
with the usual date-prefixed filename — so this is a real run, not a dry
run. The only thing skipped is opening WhatsApp/Chrome and sending
anything.

If you want to inspect a file WITHOUT touching input/ or archiving
anything (a true dry run), use --dry-run: the CSV is written next to
test_converter.py in test_output/ instead, and nothing in input/,
output/, or data_archive/ is touched.

    python test_converter.py C:/path/to/some_file.txt --dry-run
"""

import json
import sys
from pathlib import Path

import config
import converter
import app  # safe to import: app.py only opens Chrome inside watch_and_process(),
            # which we never call here. Importing it just gives us
            # convert_and_archive()/archive_filename() to reuse verbatim.


def load_bank_mapping() -> dict:
    with open(config.BANK_MAPPING_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def dry_run_convert(txt_path: Path, bank_mapping: dict) -> None:
    """Convert one file WITHOUT touching input/output/archive — just
    writes the resulting CSV to test_output/ so you can inspect it."""
    test_output_dir = config.BASE_DIR / "test_output"
    test_output_dir.mkdir(exist_ok=True)
    csv_path = test_output_dir / f"{txt_path.stem}.csv"

    print(f"Converting {txt_path.name} (dry run — nothing else touched) ...")
    try:
        row_count = converter.convert_txt_to_csv(txt_path, csv_path, bank_mapping)
    except Exception as e:
        print(f"  FAILED: {e}")
        return

    print(f"  OK -> {csv_path} ({row_count} row(s))")


def real_run_convert(txt_path: Path, bank_mapping: dict) -> None:
    """Convert + archive exactly like app.py does in production."""
    print(f"Converting {txt_path.name} ...")
    txt_out, csv_out = app.convert_and_archive(txt_path, bank_mapping)
    if txt_out is None:
        print("  FAILED — see the error above.")
        return
    print(f"  OK -> archived to data_archive/csv and data_archive/txt")
    print(f"     -> also copied to output/ (ready for app.py to send, if you run it)")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv

    bank_mapping = load_bank_mapping()

    if args:
        files = [Path(args[0])]
        if not files[0].exists():
            print(f"File not found: {files[0]}")
            return
    else:
        if dry_run:
            print("No file given — pass a path when using --dry-run.")
            return
        files = sorted(config.INPUT_DIR.glob("*.txt")) + sorted(config.INPUT_DIR.glob("*.TXT"))
        if not files:
            print(f"No .txt files found in {config.INPUT_DIR}")
            return
        print(f"Found {len(files)} file(s) in input/.\n")

    for txt_path in files:
        if dry_run:
            dry_run_convert(txt_path, bank_mapping)
        else:
            real_run_convert(txt_path, bank_mapping)
        print()


if __name__ == "__main__":
    main()
