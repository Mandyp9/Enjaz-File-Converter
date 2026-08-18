"""
TEST_EXCHANGE_RATE.PY
======================
Standalone check for the GetEXRate SOAP call, completely separate from
the file-watching/conversion pipeline (app.py only calls
exchange_rate.get_exchange_rate() when it's actually converting a TXT
file — so if nothing has gone through conversion yet, there's simply
nothing to log, which looks identical to "not working" from the outside).

Run this any time you want to check the exchange rate config/API call in
isolation:

    python test_exchange_rate.py

It will:
  1. Print which config values are set (masking the signature).
  2. Call get_exchange_rate() directly.
  3. Print the result, and confirm whether the .xlsx history and .json
     call log were written.
"""

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

import config
import exchange_rate

print("=" * 70)
print("CONFIG CHECK")
print("=" * 70)
fields = [
    "EXRATE_SOAP_URL", "EXRATE_SOAP_ACTION", "EXRATE_AGENT_CODE",
    "EXRATE_USER_ID", "EXRATE_AGENT_SESSION_ID", "EXRATE_TRANSFER_AMOUNT",
    "EXRATE_PAYMENT_MODE", "EXRATE_CALC_BY", "EXRATE_LOCATION_ID",
]
for f in fields:
    value = getattr(config, f, "<NOT SET>")
    print(f"  {f}: {value!r}")
signature = getattr(config, "EXRATE_SIGNATURE", "")
print(f"  EXRATE_SIGNATURE: {'<set, ' + str(len(signature)) + ' chars>' if signature else '<EMPTY>'}")
print(f"  EXRATE_REFRESH_TIMES: {config.EXRATE_REFRESH_TIMES}")
print(f"  EXRATE_HISTORY_FILE: {config.EXRATE_HISTORY_FILE}")
print(f"  EXRATE_JSON_LOG_FILE: {config.EXRATE_JSON_LOG_FILE}")

try:
    print(f"  STATIC_VALUES['PayoutCountry']: {config.STATIC_VALUES['PayoutCountry']!r}")
except Exception as e:
    print(f"  STATIC_VALUES['PayoutCountry']: <ERROR: {e}>")

print()
print("=" * 70)
print("CALLING get_exchange_rate() DIRECTLY")
print("=" * 70)
rate = exchange_rate.get_exchange_rate()
print(f"\nResult: {rate}")

print()
print("=" * 70)
print("OUTPUT FILES")
print("=" * 70)
print(f"  History xlsx exists: {config.EXRATE_HISTORY_FILE.exists()} -> {config.EXRATE_HISTORY_FILE}")
print(f"  JSON log exists:     {config.EXRATE_JSON_LOG_FILE.exists()} -> {config.EXRATE_JSON_LOG_FILE}")

if config.EXRATE_JSON_LOG_FILE.exists():
    import json
    with config.EXRATE_JSON_LOG_FILE.open("r", encoding="utf-8") as f:
        entries = json.load(f)
    print(f"\nLast JSON log entry:")
    print(json.dumps(entries[-1], indent=2))
