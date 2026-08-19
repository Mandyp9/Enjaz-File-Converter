# Remittance Converter - Setup & Usage Guide

This app has two parts that run together:

1. **`scheduler.py`** — logs into the Bank Albilad portal at scheduled times
   and downloads any new TXT file into `input/`.
2. **`app.py`** — watches `input/`, converts each TXT file to CSV, archives
   both, and sends the CSV to a WhatsApp group. It also sends operational
   alerts to that same group (LAN down, portal unreachable, no file found
   for a time slot, etc.).

Both need to be running (in two separate terminals) for the full pipeline
to work.

## Files in this project

| File                  | What it does                                                          |
|------------------------|------------------------------------------------------------------------|
| `config.py`            | All settings — folders, mappings, static values, WhatsApp group name, exchange-rate service credentials, schedule, network-check settings |
| `converter.py`         | Turns one TXT file into one CSV file (all the conversion rules)      |
| `exchange_rate.py`     | Gets the live exchange rate from the GetEXRate SOAP service           |
| `downloader.py`        | Logs into the Bank Albilad portal and downloads new TXT files         |
| `scheduler.py`         | **Run this.** Triggers `downloader.py` on a schedule, runs periodic LAN/portal health checks |
| `whatsapp_sender.py`   | Sends files and text alerts to your WhatsApp group via WhatsApp Web   |
| `network_check.py`     | Detects LAN vs Wi-Fi connectivity, your LAN IP, and portal reachability |
| `alerts.py`            | Cross-process queue so `scheduler.py`/`downloader.py` can ask `app.py` (which owns the live WhatsApp session) to send an alert |
| `app.py`               | **Run this too.** Watches `input/`, converts, archives, sends to WhatsApp, and dispatches queued alerts |
| `bank_mapping.json`    | Bank name -> bank code lookup table                                   |
| `wallet_formats.json`  | Wallet ID number formats (prefix + length) used to tell wallet IDs apart from bank account numbers — edit freely, no code changes needed |
| `clear_duplicate.py`   | Utility: clear duplicate-tracking for a specific file so it can be reprocessed |
| `test_downloader.py`   | Utility: manually trigger a portal login/download without waiting for the schedule |
| `test_specific_files.py` | Utility: manually download specific known files by date + sequence range |
| `diagnose_whatsapp.py` | Utility: standalone WhatsApp Web session troubleshooting              |

## Folders

| Folder                 | Purpose                                                   |
|-------------------------|------------------------------------------------------------|
| `input/`                 | New TXT files land here (via `downloader.py`, or drop one manually) |
| `output/`                | Temporary - holds CSVs briefly before they're sent. Cleared automatically after a successful send |
| `data_archive/txt/`      | Permanent copy of every TXT file ever processed (dated)   |
| `data_archive/csv/`      | Permanent copy of every CSV file ever generated (dated)   |
| `edge_profile/`          | Created automatically - stores your WhatsApp Web login (Microsoft Edge) |
| `chrome_profile_downloader/` | Created automatically - stores your Bank Albilad portal login (Chrome) |

## One-time setup

1. Install Python 3.11+ if you don't already have it.
2. Install the required packages:
   ```
   pip install -r requirements.txt
   ```
3. Make sure both Microsoft Edge (used for WhatsApp Web) and Google
   Chrome (used for the Bank Albilad portal) are installed. Edge comes
   preinstalled on Windows.
4. Open `config.py` and set:
   - `WHATSAPP_GROUP_NAME` — the **exact** name of your WhatsApp group
     (capitalization and spelling must match exactly).
   - `DOWNLOAD_USERNAME` / `DOWNLOAD_PASSWORD` — your Bank Albilad portal
     credentials.
   - `EXRATE_SOAP_URL`, `EXRATE_AGENT_CODE`, `EXRATE_USER_ID`,
     `EXRATE_AGENT_SESSION_ID`, `EXRATE_TRANSFER_AMOUNT`,
     `EXRATE_PAYMENT_MODE`, `EXRATE_CALC_BY`, `EXRATE_LOCATION_ID`,
     `EXRATE_SIGNATURE` — your GetEXRate SOAP service credentials/params.
   - `EXPECTED_LAN_IP` — the LAN IP your machine should have when the
     portal connection is working (leave blank to skip this specific
     check; LAN-connected/disconnected is still monitored either way).
   - `DOWNLOAD_SCHEDULE` — the list of `"HH:MM"` times a new file is
     expected on the portal.
5. Review `wallet_formats.json` and `bank_mapping.json` and add any
   formats/banks you need (see sections below).

## Running the app

Start **both** in separate terminals:

```
python scheduler.py
```
```
python app.py
```

- The first time `app.py` runs, an Edge window will open showing a
  WhatsApp Web QR code. Open WhatsApp on your phone, go to
  **Settings > Linked Devices > Link a Device**, and scan it. After that
  the login is remembered (`edge_profile/`), so you shouldn't need to
  scan again. `app.py` now keeps this window open for the whole run
  instead of closing it between sends.
- The first time `scheduler.py` triggers a download, a separate Chrome
  window logs into the Bank Albilad portal; that login is remembered too
  (`chrome_profile_downloader/`).
- `scheduler.py` only actively checks the portal within 5 minutes
  (`DOWNLOAD_WINDOW_MINUTES`) of each time in `DOWNLOAD_SCHEDULE`,
  polling every 30 seconds (`SCHEDULER_TICK_SECONDS`) during that window.
  Outside those windows it's idle and prints a "still running" heartbeat
  every 5 minutes so you can tell it hasn't frozen.
- Independently of the schedule, it also checks LAN connectivity, your
  LAN IP, and whether the portal is reachable every 15 minutes
  (`NETWORK_CHECK_INTERVAL_SECONDS`), so a problem is caught even during
  long gaps between download windows.
- Once a file lands in `input/`, `app.py` (checking every
  `WATCH_INTERVAL_SECONDS`, default 3) will:
  1. Convert it to CSV (fetching the exchange rate once per file from
     `exchange_rate.py`)
  2. Save dated copies in `data_archive/txt/` and `data_archive/csv/`
  3. Send the TXT and CSV to your WhatsApp group
  4. Remove both from `output/` once sent successfully

To stop either process, press `Ctrl+C` in its terminal.

## WhatsApp alerts

`app.py` sends operational alerts to the same WhatsApp group as the
files, when:

- **LAN disconnected** — no Ethernet connection detected (Wi-Fi-only or
  no connection at all). The portal needs the LAN connection.
- **LAN IP mismatch** — connected via LAN, but the IP doesn't match
  `EXPECTED_LAN_IP`.
- **Portal unreachable** — the LAN/IP look fine, but the Bank Albilad
  portal itself isn't responding (down or under maintenance).
- **No file found for a time slot** — a scheduled window closed without
  ever finding a new file, but ONLY if the portal was actually reachable
  and checked at some point during that window (if it was blocked the
  whole time by a LAN/portal issue, you already got that alert instead —
  reporting "no file found" on top would be misleading).

Repeat alerts for the same ongoing issue are throttled to once every
`ALERT_COOLDOWN_MINUTES` (default 30) so it reminds you periodically
without spamming the group.

## If a WhatsApp send fails

- The CSV (and TXT) stay in `output/` (NOT deleted).
- Both are still archived in `data_archive/` either way, so nothing is
  lost.
- On the next check, the app automatically retries sending any leftover
  files in `output/`.
- You can also just open the file in `output/` and send it manually if
  needed.

## Adding new banks to bank_mapping.json

Each bank entry looks like this:

```json
"SIDDHARTHA BANK LIMITED": {
    "code": "79",
    "aliases": ["SIDDHARTHA"]
}
```

- The key (`"SIDDHARTHA BANK LIMITED"`) is the full official bank name.
- `"code"` is the bank code to put in the CSV.
- `"aliases"` is a list of short names that should also match this bank
  (e.g. if the TXT file just says "Siddhartha" or "SBL").

The app also automatically treats "LTD" and "LIMITED" (and a few other
common variations) as the same word, so you don't need both forms.

## Adding wallet ID formats to wallet_formats.json

Wallet IDs (eSewa/Khalti/IME Pay/etc.) are always phone numbers, so the
app tells them apart from bank account numbers by checking the account
number's shape against the formats you list here:

```json
{ "label": "Nepal mobile (local, no country code)", "prefix": "9", "length": 10 }
```

- `prefix` — the digits the number must start with.
- `length` — the TOTAL number of digits (prefix included).
- An account number matching ANY entry (right prefix AND right length)
  is treated as a wallet ID; anything else numeric is treated as a bank
  account number.

Add/edit/remove entries freely — no code changes needed, the file is
re-read automatically whenever it changes.
