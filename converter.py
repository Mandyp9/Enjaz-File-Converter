"""
CONVERTER.PY
============
This file contains the "engine" that turns one TXT file into one CSV file.

Flow for each TXT file:
    1. read_txt_file()       -> reads the file and pulls out 'D' lines
    2. parse_line()          -> chops each D line into named fields
    3. transform_record()    -> applies all business rules / mappings
    4. write_csv()           -> writes the final CSV file

Everything is plain functions (no classes) so the flow is easy to follow
top to bottom.
"""

import csv
import json
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

import config
import exchange_rate

try:
    import phonenumbers
    _PHONENUMBERS_AVAILABLE = True
except ImportError:
    _PHONENUMBERS_AVAILABLE = False

logger = logging.getLogger(__name__)

# Matches a phrase containing the word BANK, e.g. "SIDDHARTHA BANK LIMITED"
_BANK_NAME_PATTERN = re.compile(
    r"([A-Za-z][A-Za-z.&\-]*(?:\s+[A-Za-z][A-Za-z.&\-]*)*\s*BANK\s*(?:[A-Za-z][A-Za-z.&\-]*\s*)*)",
    re.IGNORECASE,
)

# Wallet-vs-bank detection: wallet IDs (eSewa/Khalti/IME Pay/etc.) are
# always keyed by mobile number, so an account number shaped like a
# phone number is a strong signal it's a wallet, not a bank account.
# The actual prefix/length rules live in wallet_formats.json (user-
# editable, no code changes needed) — see load_wallet_formats() below.
_wallet_formats_cache = None
_wallet_formats_file_mtime = None


def load_wallet_formats() -> list:
    """Load (and cache) the wallet-ID prefix/length rules from
    config.WALLET_FORMATS_FILE. Re-reads the file automatically if it
    changes on disk, so you can edit it without restarting the app.

    Returns a list of {"prefix": str, "length": int} dicts. Returns an
    empty list (with a warning logged once) if the file is missing or
    invalid — wallet detection then falls back to the phonenumbers
    library alone (see looks_like_wallet_number()).
    """
    global _wallet_formats_cache, _wallet_formats_file_mtime

    path = config.WALLET_FORMATS_FILE
    if not path.exists():
        if _wallet_formats_cache is None:
            logger.warning(
                "%s not found — wallet-ID prefix/length rules are empty. "
                "Create this file to identify wallet IDs reliably.", path.name
            )
        _wallet_formats_cache = []
        return _wallet_formats_cache

    mtime = path.stat().st_mtime
    if _wallet_formats_cache is not None and mtime == _wallet_formats_file_mtime:
        return _wallet_formats_cache

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        formats = []
        for entry in data.get("wallet_number_formats", []):
            prefix = str(entry.get("prefix", "")).strip()
            length = entry.get("length")
            if not isinstance(length, int) or length <= 0:
                logger.warning("Skipping invalid wallet_formats.json entry: %s", entry)
                continue
            formats.append({"prefix": prefix, "length": length})
        _wallet_formats_cache = formats
        _wallet_formats_file_mtime = mtime
        logger.info("Loaded %d wallet number format(s) from %s.", len(formats), path.name)
    except Exception:
        logger.exception("Could not read %s — wallet-ID rules unchanged.", path.name)
        if _wallet_formats_cache is None:
            _wallet_formats_cache = []

    return _wallet_formats_cache


# ---------------------------------------------------------------------------
# STEP 1: READ THE TXT FILE
# ---------------------------------------------------------------------------
def read_data_lines(txt_path: Path) -> list:
    """Read a TXT file and return only the lines that start with 'D'.

    Lines starting with 'H' (header) and 'F' (footer) are ignored.

    Args:
        txt_path: Path to the input TXT file.

    Returns:
        A list of raw text lines (strings), one per 'D' record.
    """
    data_lines = []

    with txt_path.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\r\n")
            if not line:
                continue
            if line[0] == "D":
                data_lines.append(line)

    logger.info("Found %d data line(s) in %s", len(data_lines), txt_path.name)
    return data_lines


# ---------------------------------------------------------------------------
# STEP 2: PARSE ONE LINE INTO NAMED FIELDS
# ---------------------------------------------------------------------------
def parse_line(line: str) -> dict:
    """Chop a fixed-width 'D' line into a dictionary of named fields.

    Field positions come from config.FIELD_POSITIONS.

    Args:
        line: One raw 'D' line from the TXT file.

    Returns:
        A dictionary like {"transaction_reference": "ABC123", ...} with
        every value stripped of surrounding whitespace.
    """
    fields = {}

    for field_name, (start, length) in config.FIELD_POSITIONS.items():
        start_index = start - 1
        end_index = start_index + length

        if start_index >= len(line):
            value = ""
        else:
            value = line[start_index:end_index]

        fields[field_name] = value.strip()

    # Kept for fields that need the RAW (unstripped) slice rather than the
    # trimmed value above — see the RemitterAddress/CIF logic in
    # transform_record().
    fields["_raw_line"] = line

    return fields


# ---------------------------------------------------------------------------
# SMALL HELPER FUNCTIONS USED DURING TRANSFORMATION
# ---------------------------------------------------------------------------
def build_remitter_address_from_cif(raw_line: str) -> str:
    """RemitterAddress = "CIF:" + the raw 10-char CIF slice, ported from:

        set "RemitterAddress=!line:~90,10!"
        set "RemitterAddress=!RemitterAddress: =!"
        if defined RemitterAddress (
            set "RemitterAddress=CIF:!line:~90,10!"
        ) else (
            set "RemitterAddress="
        )

    line:~90,10 is a 0-based slice (start=90, length=10), which is the
    same as 1-based start=91, length=10 in FIELD_POSITIONS — the first
    10 characters of the "remitter_code" field. The raw (unstripped)
    slice is used for the final value so it matches the batch version
    exactly; only the "is it blank" check is done against a trimmed copy.
    """
    start, _length = config.FIELD_POSITIONS["remitter_code"]
    cif_length = 10
    start_index = start - 1
    end_index = start_index + cif_length

    raw_slice = raw_line[start_index:end_index] if start_index < len(raw_line) else ""

    if raw_slice.strip():
        return f"CIF:{raw_slice}"
    return ""


def normalize_spaces(value: str) -> str:
    """Collapse multiple spaces/tabs into a single space.

    Example: "JOHN   DOE" -> "JOHN DOE"
    """
    if not value:
        return value
    return re.sub(r"\s+", " ", value).strip()


def format_date(raw_date: str) -> str:
    """Convert YYYYMMDD -> M/D/YYYY (no leading zeros).

    Example: "20260612" -> "6/12/2026"
    Returns "" if the input is empty or not a valid date.
    """
    if not raw_date:
        return ""

    try:
        parsed = datetime.strptime(raw_date, "%Y%m%d")
    except ValueError:
        logger.warning("Invalid date '%s'; leaving blank", raw_date)
        return ""

    return f"{parsed.month}/{parsed.day}/{parsed.year}"


def csv_safe_numeric(value: str) -> str:
    """Add a leading apostrophe (') to numbers Excel/CSV would mangle.

    - Numbers with a leading zero (e.g. "0123") would lose the zero.
    - Numbers with 12+ digits would be shown in scientific notation.

    Adding ' tells Excel to treat the value as plain text.
    """
    if not value:
        return value

    stripped = value.strip()

    if not stripped.isdigit():
        return value

    has_leading_zero = len(stripped) > 1 and stripped[0] == "0"
    is_long = len(stripped) >= 12

    if has_leading_zero or is_long:
        return f"'{value}"

    return value


def canonicalize_bank_name(value: str) -> str:
    """Normalize a bank name for comparison (uppercase, no extra spaces,
    common suffixes like LIMITED/LTD treated as the same word).
    """
    normalized = normalize_spaces(value).upper().replace(".", "")

    for variant, canonical in config.BANK_SUFFIX_NORMALIZATION.items():
        normalized = re.sub(r"\b" + re.escape(variant) + r"\b", canonical, normalized)

    return normalize_spaces(normalized)


def extract_bank_from_address(address: str) -> tuple:
    """If the address text contains a bank name (a phrase with 'BANK' in
    it), pull it out.

    Returns:
        (remaining_address, extracted_bank_name)
        extracted_bank_name is "" if no bank name was found.
    """
    if not address:
        return address, ""

    match = _BANK_NAME_PATTERN.search(address)
    if not match:
        return address, ""

    extracted_bank = normalize_spaces(match.group(1))

    remaining = address[:match.start()] + address[match.end():]
    remaining = re.sub(r"^[\s,.\-]+|[\s,.\-]+$", "", remaining)
    remaining = normalize_spaces(remaining)

    return remaining, extracted_bank


def extract_account_from_address(address: str) -> tuple:
    """If the address is purely digits (a wallet/account number), pull it
    out.

    Returns:
        (remaining_address, extracted_account_no)
        extracted_account_no is "" if the address is not purely numeric.
    """
    if not address:
        return address, ""

    stripped = address.strip()

    if stripped.isdigit():
        return "", stripped

    return address, ""


def address_contains_ssf(address: str) -> bool:
    """Return True if the BeneficiaryAddress text contains 'SSF' anywhere
    (case-insensitive). This is a special payout-type flag: when present,
    PaymentMode is forced to 'B' and BeneficiaryBankCode is forced to 125,
    overriding all other PaymentMode/bank rules.
    """
    if not address:
        return False
    return "SSF" in address.upper()


def lookup_bank(bank_mapping: dict, bank_name: str) -> tuple:
    """Find the bank code + full bank name for a given (possibly short or
    differently-formatted) bank name.

    Matching is tried in this order:
        1. Exact match (ignoring case / LTD vs LIMITED differences)
        2. The mapping's full name appears inside bank_name, or vice versa
        3. A configured alias (e.g. "CITIZEN") appears inside bank_name

    Args:
        bank_mapping: The loaded bank_mapping.json data.
        bank_name: The bank name as found in the TXT file.

    Returns:
        (bank_code, resolved_bank_name)
        If nothing matches, returns ("", bank_name) unchanged.
    """
    if not bank_name:
        return "", bank_name

    canonical_name = canonicalize_bank_name(bank_name)

    # 1. Exact match
    for full_name, entry in bank_mapping.items():
        if canonicalize_bank_name(full_name) == canonical_name:
            return entry.get("code", ""), full_name

    # 2. Substring match against full names
    best_full_name = None
    best_canonical = None
    best_code = ""

    for full_name, entry in bank_mapping.items():
        canonical_full_name = canonicalize_bank_name(full_name)

        if canonical_full_name in canonical_name or canonical_name in canonical_full_name:
            if best_canonical is None or len(canonical_full_name) > len(best_canonical):
                best_canonical = canonical_full_name
                best_full_name = full_name
                best_code = entry.get("code", "")

    if best_full_name:
        logger.info("Matched bank '%s' to '%s' (substring match)", bank_name, best_full_name)
        return best_code, best_full_name

    # 3. Alias match (e.g. "Citizen" -> "CITIZENS BANK INTERNATIONAL LTD")
    for full_name, entry in bank_mapping.items():
        for alias in entry.get("aliases", []):
            canonical_alias = canonicalize_bank_name(alias)
            if not canonical_alias:
                continue
            if canonical_alias in canonical_name or canonical_name in canonical_alias:
                logger.info("Matched bank '%s' to '%s' (alias '%s')", bank_name, full_name, alias)
                return entry.get("code", ""), full_name

    return "", bank_name


def looks_like_wallet_number(account_no: str) -> bool:
    """True if account_no is shaped like a mobile number, which means it's
    almost certainly a wallet ID (eSewa/Khalti/IME Pay/etc. wallets are
    always keyed by phone number) rather than a bank account number.

    Bank account numbers are also numeric, so "is it all digits" alone
    isn't enough to tell them apart — this checks the actual shape of
    the number instead, in two layers:
        1. Your own prefix/length rules from wallet_formats.json (the
           authoritative source — edit that file to add/fix formats).
        2. Fallback: any other valid international MOBILE number,
           detected via the phonenumbers library (if installed), for
           numbers not covered by an explicit rule yet.
    """
    if not account_no or not account_no.isdigit():
        return False

    for fmt in load_wallet_formats():
        prefix = fmt["prefix"]
        length = fmt["length"]
        if len(account_no) == length and account_no.startswith(prefix):
            return True

    if _PHONENUMBERS_AVAILABLE:
        try:
            parsed = phonenumbers.parse("+" + account_no, None)
            if phonenumbers.is_valid_number(parsed) and phonenumbers.number_type(parsed) in (
                phonenumbers.PhoneNumberType.MOBILE,
                phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE,
            ):
                return True
        except phonenumbers.NumberParseException:
            pass

    return False


def is_cash_remittance(remittance_option: str) -> bool:
    """Check the file's own transaction-type identifier (the
    RemittanceOption field) for the cash marker.

    This is checked BEFORE any bank/wallet extraction logic runs, and
    real sample data confirms it's reliable: cash transactions always
    carry "Cash Remittance" here. (Bank and wallet transactions both use
    "Remittance To Account" for this same field, so it can tell us
    definitively "this is cash" but can't by itself distinguish bank
    from wallet — that still comes from the shape-based rule below.)
    """
    return bool(remittance_option) and "cash remittance" in remittance_option.strip().lower()


def determine_payment_mode(bank_name: str, account_no: str) -> str:
    """Work out PaymentMode as 'B' or 'W' for a transaction ALREADY known
    not to be cash (see is_cash_remittance, checked earlier and
    separately in transform_record — cash never reaches this function).

    Shape-based rule:
        - bank_name is filled in                          -> "B"
        - account_no is shaped like a mobile number        -> "W"
        - account_no is numeric but NOT phone-shaped       -> "B"
          (an unlabeled bank account number, not a wallet)
        - otherwise                                        -> "C"
          (safety net only — shouldn't normally happen once cash is
          already ruled out by remittance_option, but guards against an
          address with neither a bank name nor any usable digits)
    """
    if bank_name:
        return "B"

    if looks_like_wallet_number(account_no):
        return "W"

    if account_no and account_no.isdigit():
        # Numeric but not shaped like a mobile number — treat as an
        # unlabeled bank account number rather than guessing wallet.
        return "B"

    return "C"


# ---------------------------------------------------------------------------
# STEP 3: TRANSFORM ONE PARSED RECORD INTO A CSV ROW
# ---------------------------------------------------------------------------
def transform_record(fields: dict, bank_mapping: dict, exchange_rate_value: Decimal) -> dict:
    """Apply all business rules and return one CSV row as a dictionary
    (column name -> value).

    Args:
        fields: The dictionary returned by parse_line().
        bank_mapping: The loaded bank_mapping.json data.

    Returns:
        A dictionary with one key per entry in config.CSV_HEADERS.
    """
    beneficiary_bank_name = fields["beneficiary_bank"]
    account_no = fields["beneficiary_account_no"]
    address = fields["beneficiary_address"]
    amount = fields["transaction_amount"]
    remittance_option = fields["remittance_option"]

    # Rule (highest priority): if BeneficiaryAddress contains "SSF" anywhere,
    # PaymentMode is forced to 'B' and BeneficiaryBankCode is forced to 125,
    # overriding both the cash identifier and every other rule below.
    is_ssf = address_contains_ssf(address)

    # Rule (checked FIRST, before any bank/wallet extraction runs): the
    # file's own RemittanceOption field tells us definitively whether this
    # is a cash payout. Checking this up front — and skipping the address
    # extraction entirely when it's cash — matters: a cash transaction's
    # BeneficiaryAddress can just be the beneficiary's bare phone number
    # (there's no real account to put there), and running the bank/wallet
    # extraction on it anyway used to misread that phone number as a
    # wallet ID. Not reached at all if is_ssf already forced 'B' above.
    is_cash = (not is_ssf) and is_cash_remittance(remittance_option)

    if is_cash:
        payment_mode = "C"
    else:
        # Rule: pull a bank name out of the address if one isn't already given.
        if not beneficiary_bank_name:
            address, extracted_bank = extract_bank_from_address(address)
            if extracted_bank:
                beneficiary_bank_name = extracted_bank

        # Rule: pull a numeric account/wallet number out of the address if
        # the account number isn't already given.
        if not account_no:
            address, extracted_account = extract_account_from_address(address)
            if extracted_account:
                account_no = extracted_account

        if is_ssf:
            payment_mode = "B"
        else:
            # Rule: PaymentMode (B/W) via shape, now that cash is ruled out.
            payment_mode = determine_payment_mode(beneficiary_bank_name, account_no)

    # Rule: clear account number when paying out by cash (C).
    if payment_mode == "C":
        account_no = ""

    if is_ssf:
        bank_code = "125"
        beneficiary_bank_name = "SSF Bank"
    elif payment_mode == "B":
        # Rule: resolve bank name -> (code, full official name).
        bank_code, beneficiary_bank_name = lookup_bank(bank_mapping, beneficiary_bank_name)

        # Rule: if no bank code could be mapped, use static fallback values
        # so these two fields are never left empty. This fallback only
        # applies to PaymentMode 'B' (bank transfer) - C (cash) and
        # W (wallet) payouts don't involve a bank, so they stay blank.
        if not bank_code:
            bank_code = "99"
            beneficiary_bank_name = "eSewa Bank Account"
    else:
        # PaymentMode C or W - no bank involved, leave blank.
        bank_code = ""
        beneficiary_bank_name = ""

    # Rule: exchange rate via the GetEXRate SOAP service (exchange_rate.py)
    # - used ONLY to calculate USDValueAmount. TransactionExchangeRate in
    # the CSV is always "1".
    # NOTE: exchange_rate_value is fetched ONCE per file by the caller
    # (convert_txt_to_csv) and passed in here — every row in the same
    # file must use the exact same rate, so a single file's rows can't
    # end up computed against different rates if the service happens to
    # return a different value between two calls.

    try:
        amount_decimal = Decimal(amount) if amount else Decimal("0")
    except InvalidOperation:
        logger.warning("Invalid amount '%s'; using 0", amount)
        amount_decimal = Decimal("0")

    if exchange_rate_value == 0:
        usd_value = Decimal("0.00")
    else:
        # Truncate to 2 decimal places rather than rounding — just cut
        # off anything past the hundredths place, don't round the third
        # decimal up/down.
        usd_value = (amount_decimal / exchange_rate_value).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )

    # Build the row using the static values + mapped values.
    row = dict(config.STATIC_VALUES)  # start with all static values

    row.update({
        "PINNO": csv_safe_numeric(fields["transaction_reference"]),
        "RemitterName": normalize_spaces(fields["remitter_name"]),
        "RemitterAddress": build_remitter_address_from_cif(fields.get("_raw_line", "")),
        "RemitterContact": "",
        "RemitterCity": fields["our_branch_id"],
        "RemitterState": "",
        "RemitterID": csv_safe_numeric(fields["remitter_id"]),
        "RemitterIDExpireDate": "",
        "RemitterDOB": "",
        "BeneficiaryName": normalize_spaces(fields["beneficiary_name"]),
        "BeneficiaryAddress": address,
        "BeneficiaryCity": "",
        "BeneficiaryContact": fields["beneficiary_phone_no"],
        "BeneficiaryIDType": "",
        "BeneficiaryID": "",
        "PayoutAMT": csv_safe_numeric(amount),
        "PayoutCCY": fields["transfer_currency"],
        "TransactionDate": format_date(fields["transaction_date"]),
        "PayoutLocationName": "",
        "PaymentMode": payment_mode,
        "BeneficiaryBankCode": bank_code,
        "BeneficiaryBankName": beneficiary_bank_name,
        "BeneficiaryBankBranch": fields["beneficiary_bank_branch"],
        "BankAccountNo": csv_safe_numeric(account_no),
        "USDValueAmount": csv_safe_numeric(str(usd_value)),
        "TransactionExchangeRate": "1",
    })

    return row


# ---------------------------------------------------------------------------
# STEP 4: WRITE THE CSV FILE
# ---------------------------------------------------------------------------
def write_csv(rows: list, csv_path: Path) -> None:
    """Write a list of row-dictionaries to a CSV file with the configured
    headers.

    Args:
        rows: List of dictionaries, each as returned by transform_record().
        csv_path: Where to save the CSV file.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(config.CSV_HEADERS)

        for row in rows:
            writer.writerow([row.get(header, "") for header in config.CSV_HEADERS])

    logger.info("Wrote %d row(s) to %s", len(rows), csv_path)


# ---------------------------------------------------------------------------
# PUT IT ALL TOGETHER: ONE TXT FILE -> ONE CSV FILE
# ---------------------------------------------------------------------------
def load_bank_mapping() -> dict:
    """Load bank_mapping.json from disk."""
    try:
        with config.BANK_MAPPING_FILE.open("r", encoding="utf-8") as f:
            mapping = json.load(f)
        logger.info("Loaded %d bank mapping entries", len(mapping))
        return mapping
    except FileNotFoundError:
        logger.error("Bank mapping file not found: %s", config.BANK_MAPPING_FILE)
        return {}
    except json.JSONDecodeError:
        logger.exception("Could not read bank mapping file (invalid JSON)")
        return {}


def convert_txt_to_csv(txt_path: Path, csv_path: Path, bank_mapping: dict) -> int:
    """Convert one TXT file into one CSV file.

    Args:
        txt_path: The source TXT file.
        csv_path: Where to write the resulting CSV file.
        bank_mapping: The loaded bank_mapping.json data.

    Returns:
        The number of data rows written.
    """
    data_lines = read_data_lines(txt_path)

    # Fetch the exchange rate ONCE for the whole file (not once per row).
    # See the note in transform_record() for why per-row fetching was the
    # root cause of inconsistent rates within a single CSV.
    exchange_rate_value = exchange_rate.get_exchange_rate()
    logger.info(
        "Converting %s using exchange rate %s for all %d row(s).",
        txt_path.name, exchange_rate_value, len(data_lines)
    )

    rows = []
    for line in data_lines:
        fields = parse_line(line)
        row = transform_record(fields, bank_mapping, exchange_rate_value)
        rows.append(row)

    write_csv(rows, csv_path)
    return len(rows)
