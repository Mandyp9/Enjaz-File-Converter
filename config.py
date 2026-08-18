"""
CONFIG.PY
=========
All settings for the Remittance Converter live here.
If you need to change a folder path, a static value, or a mapping,
this is the only file you usually need to touch.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# FOLDERS
# ---------------------------------------------------------------------------
# Base folder = the folder this config.py file lives in.
BASE_DIR = Path(__file__).resolve().parent

INPUT_DIR = BASE_DIR / "input"          # Drop new TXT files here
OUTPUT_DIR = BASE_DIR / "output"        # CSV files wait here briefly before WhatsApp send
ARCHIVE_TXT_DIR = BASE_DIR / "data_archive" / "txt"   # All processed TXT files (permanent history)
ARCHIVE_CSV_DIR = BASE_DIR / "data_archive" / "csv"   # All generated CSV files (permanent history)

BANK_MAPPING_FILE = BASE_DIR / "bank_mapping.json"

# JSON file where YOU list every wallet ID number format (prefix + total
# digit length) — used to tell wallet IDs (eSewa/Khalti/IME Pay/etc, which
# are always phone numbers) apart from bank account numbers. Edit this
# file directly; no code changes needed to add a new prefix/length.
WALLET_FORMATS_FILE = BASE_DIR / "wallet_formats.json"

# ---------------------------------------------------------------------------
# EXCHANGE RATE (GetEXRate SOAP service — see exchange_rate.py)
# ---------------------------------------------------------------------------
# Fill these in with your real agent/session credentials.
EXRATE_SOAP_URL = "https://online.esewaremit.com/intlsendV5/txnservice.asmx"
EXRATE_SOAP_ACTION = "ClientWebService/GetEXRate"
EXRATE_AGENT_CODE = "test11"
EXRATE_USER_ID = "test"
EXRATE_AGENT_SESSION_ID = "18391101"
EXRATE_TRANSFER_AMOUNT = "5"
EXRATE_PAYMENT_MODE = "B"
EXRATE_CALC_BY = "P"
EXRATE_LOCATION_ID = ""
EXRATE_PAYOUT_COUNTRY = "NPL"
EXRATE_SIGNATURE = "15ba5ac97d9d0de61c65df1004cea4a37d5660339aba24c623cba169b858f509"

# Seconds to wait for the SOAP service to respond before giving up.
EXRATE_REQUEST_TIMEOUT_SECONDS = 15

# Used only if the SOAP call fails AND no rate has succeeded yet this run
# (exchange_rate.py otherwise falls back to the last rate that worked).
EXRATE_DEFAULT_FALLBACK_RATE = "1"

# The rate only actually changes twice a day, so exchange_rate.py only
# calls the SOAP service when the clock crosses one of these times (24h
# HH:MM, local time) since the last successful call. Every other request
# for the rate — no matter how many files are converted in between —
# reuses the cached value instead of hitting the service again. Add/remove
# times here if the bank's refresh schedule changes.
EXRATE_REFRESH_TIMES = ["10:30", "14:30"]

# Every fetched rate (scheduled or fallback) is logged here as a running
# history, and this file also doubles as the persisted cache — so a
# restart of app.py picks up the last fetched rate instead of immediately
# re-calling the SOAP service.
EXRATE_HISTORY_FILE = BASE_DIR / "exchange_rate_history.xlsx"

# Lightweight JSON log of every SOAP call ATTEMPT (success or failure),
# written independently of the .xlsx history above so you always have a
# record even if, say, openpyxl/the xlsx write has a problem. Useful for
# quickly checking "did today's 10am/2pm refresh actually work?" without
# opening Excel. Keeps only the most recent EXRATE_JSON_LOG_MAX_ENTRIES.
EXRATE_JSON_LOG_FILE = BASE_DIR / "exchange_rate_log.json"
EXRATE_JSON_LOG_MAX_ENTRIES = 500

# ---------------------------------------------------------------------------
# NETWORK / PORTAL MONITORING
# ---------------------------------------------------------------------------
# The Bank Albilad portal is only reachable over your LAN (Ethernet)
# connection. Set this to the LAN IP address your machine should have
# when the connection is working correctly — leave it as "" to skip the
# exact-IP check (LAN-connected/disconnected is still checked either way).
EXPECTED_LAN_IP = "10.13.143.98"

# URL used for a quick "is the portal even reachable" check before trying
# to log in.
PORTAL_BASE_URL = "https://remittance.bankalbilad.com/_forms/default.aspx"

# Seconds to wait for the portal to respond before considering it "down".
PORTAL_REACHABILITY_TIMEOUT_SECONDS = 15

# How often (seconds) LAN connectivity/IP and portal reachability are
# checked. This runs on its own timer in scheduler.py's main loop,
# independent of download windows, so a problem is caught even during
# long gaps between scheduled slots — not just while a window happens to
# be open.
NETWORK_CHECK_INTERVAL_SECONDS = 15 * 60  # 15 minutes

# Minimum minutes between repeat WhatsApp alerts for the SAME ongoing
# issue (e.g. LAN still down), so it reminds you periodically without
# spamming the group every 30 seconds while a problem persists.
ALERT_COOLDOWN_MINUTES = 30

# Where scheduler.py/downloader.py queue alert messages for app.py (which
# owns the live WhatsApp session) to actually send. Internal use only.
ALERTS_QUEUE_FILE = BASE_DIR / "pending_alerts.json"
ALERTS_STATE_FILE = BASE_DIR / "alert_cooldowns.json"

# ---------------------------------------------------------------------------
# FOLDER WATCHER SETTINGS
# ---------------------------------------------------------------------------
# How often (in seconds) to check the input folder for new TXT files.
WATCH_INTERVAL_SECONDS = 3

# ---------------------------------------------------------------------------
# WHATSAPP SETTINGS
# ---------------------------------------------------------------------------
# Exact name of the WhatsApp group to send converted CSV files to.
# Must match the group name exactly as it appears in WhatsApp.
WHATSAPP_GROUP_NAME = "Enjaz File"

# Caption/message sent along with each file.
WHATSAPP_CAPTION = "Converted Enjaz file"

# Extra seconds to wait for WhatsApp Web to load before sending (pywhatkit
# default is 15-20s). Increase this if your internet/browser is slow.
WHATSAPP_WAIT_TIME_SECONDS = 25

# Seconds to wait between sending multiple files in the same run.
WHATSAPP_CLOSE_DELAY_SECONDS = 1.5

# ---------------------------------------------------------------------------
# FIXED-WIDTH FIELD POSITIONS (start position and length for each field)
# Start is 1-based, matching the original file specification.
# ---------------------------------------------------------------------------
FIELD_POSITIONS = {
    "seq_number":                 (2, 6),
    "transaction_reference":      (8, 16),
    "transaction_date":           (24, 8),
    "transaction_amount":         (32, 18),
    "transfer_currency":          (50, 3),
    "our_branch_id":               (53, 3),
    "remitter_name":               (56, 35),
    "remitter_code":                (91, 35),
    "remitter_id":                   (126, 35),
    "remittance_option":            (161, 35),
    "corresponding_branch_city":    (196, 35),
    "corresponding_branch_name":    (231, 35),
    "corresponding_branch_code":    (266, 35),
    "beneficiary_bank":              (301, 35),
    "beneficiary_bank_city":         (336, 35),
    "beneficiary_bank_branch":       (371, 70),
    "beneficiary_name":              (441, 105),
    "beneficiary_account_no":        (546, 35),
    "beneficiary_address":           (581, 105),
    "beneficiary_phone_no":          (686, 35),
    "beneficiary_id":                 (721, 35),
    "remitter_dob":                   (756, 35),
    "remitter_nationality":           (791, 35),
    "payment_details":                (826, 105),
}

# ---------------------------------------------------------------------------
# OUTPUT CSV COLUMN ORDER
# ---------------------------------------------------------------------------
CSV_HEADERS = [
    "User ID", "PINNO", "RemitterName", "RemitterAddress", "RemitterContact",
    "RemitterCity", "RemitterState", "RemitterCountry", "RemitterIDType",
    "RemitterID", "RemitterIDExpireDate", "RemitterDOB", "RemitterNationality",
    "RemitterOccupation", "SourceOfIncome", "PurposeOfRemittance",
    "BeneficiaryName", "BeneficiaryAddress", "BeneficiaryCity",
    "BeneficiaryContact", "BeneficiaryIDType", "BeneficiaryID",
    "RelationshipToRemitter", "PayoutCountry", "PayoutAMT", "PayoutCCY",
    "TransactionDate", "PayoutLocationName", "PaymentMode",
    "BeneficiaryBankCode", "BeneficiaryBankName", "BeneficiaryBankBranch",
    "BankAccountNo", "USDValueAmount", "TransactionExchangeRate",
    "TransactionFeeToCustomer", "SendCurrency",
]

# ---------------------------------------------------------------------------
# STATIC VALUES (same for every record)
# ---------------------------------------------------------------------------
STATIC_VALUES = {
    "User ID": "10009674",
    "RemitterCountry": "SAU",
    "RemitterNationality": "NPL",
    "RemitterIDType": "'08",
    "RemitterOccupation": "'02",
    "SourceOfIncome": "18",
    "PurposeOfRemittance": "6",
    "RelationshipToRemitter": "27",
    "PayoutCountry": "NPL",
    "TransactionFeeToCustomer": "0",
    "SendCurrency": "NPR",
}

# ---------------------------------------------------------------------------
# BANK NAME SUFFIX NORMALIZATION
# Used so "LTD" and "LIMITED" (etc.) are treated as the same word when
# matching bank names.
# ---------------------------------------------------------------------------
BANK_SUFFIX_NORMALIZATION = {
    "LIMITED": "LTD",
    "PRIVATE": "PVT",
    "COMPANY": "CO",
}

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"

# ---------------------------------------------------------------------------
# BANK ALBILAD PORTAL - FILE DOWNLOAD
# ---------------------------------------------------------------------------
DOWNLOAD_USERNAME = "13994464"
DOWNLOAD_PASSWORD = "EMT@1234#"

# Schedule: times (24h HH:MM) when a new TXT file is expected on the portal.
# The downloader checks for +5 minutes around each of these times.
DOWNLOAD_SCHEDULE = [
    "11:59", "12:46", "13:15", "13:46", "14:15", "14:46",
    "15:15", "15:46", "16:15", "16:45", "17:15",
    "17:45", "18:15", "18:46", "19:15", "19:46",
    "20:15", "20:46", "01:46"
]

# How often (seconds) the scheduler checks if it's inside a download window.
SCHEDULER_TICK_SECONDS = 30

# How many minutes after each scheduled time to keep checking for the file.
DOWNLOAD_WINDOW_MINUTES = 5

# How often (seconds) scheduler.py prints a "still running, next slot is
# at X" heartbeat during idle periods between windows — just enough to
# confirm it's alive without spamming the log every 30s tick.
HEARTBEAT_INTERVAL_SECONDS = 300
