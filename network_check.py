"""
NETWORK_CHECK.PY
=================
Checks the machine's network connectivity before trying to reach the
Bank Albilad portal:
  - Is there an active LAN (Ethernet) connection at all?
  - Does its IP address match the one you expect (config.EXPECTED_LAN_IP)?
  - Is the portal itself actually reachable (vs. genuinely down)?

Used by downloader.py before every portal login attempt. Results feed
into alerts.py so problems get flagged in WhatsApp instead of the
scheduler silently retrying forever with no visibility.

SETUP:
    pip install psutil
"""

import logging
import socket
import urllib.error
import urllib.request

import config

logger = logging.getLogger(__name__)

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

# Interface name hints used to tell LAN (Ethernet) apart from Wi-Fi on
# Windows/macOS/Linux — adapter friendly names normally contain one of
# these (e.g. "Ethernet", "Ethernet 2", "Wi-Fi", "wlan0").
LAN_NAME_HINTS = ("ethernet", "eth", "lan", "local area connection")
WIFI_NAME_HINTS = ("wi-fi", "wifi", "wlan", "wireless")


def _is_real_ipv4(addr) -> bool:
    """Filter out loopback and link-local/APIPA addresses (169.254.x.x),
    which Windows assigns when a cable is plugged in but not actually
    getting a network — i.e. "connected" but not really connected."""
    ip = addr.address
    if not ip or ip.startswith("127.") or ip.startswith("169.254."):
        return False
    return True


def get_network_status() -> dict:
    """Returns:
        {
            "psutil_available": bool,
            "lan_connected": bool,
            "lan_ip": str | None,
            "wifi_connected": bool,
            "wifi_ip": str | None,
            "ip_matches_expected": bool | None,  # None if EXPECTED_LAN_IP not set in config.py
        }
    """
    status = {
        "psutil_available": _PSUTIL_AVAILABLE,
        "lan_connected": False,
        "lan_ip": None,
        "wifi_connected": False,
        "wifi_ip": None,
        "ip_matches_expected": None,
    }

    if not _PSUTIL_AVAILABLE:
        logger.warning("psutil not installed — cannot check LAN/Wi-Fi status. Run: pip install psutil")
        return status

    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
    except Exception:
        logger.exception("Could not read network interface info.")
        return status

    for name, if_addrs in addrs.items():
        iface_stats = stats.get(name)
        if iface_stats is None or not iface_stats.isup:
            continue

        ipv4 = None
        for addr in if_addrs:
            if addr.family == socket.AF_INET and _is_real_ipv4(addr):
                ipv4 = addr.address
                break
        if ipv4 is None:
            continue

        name_lower = name.lower()
        if any(hint in name_lower for hint in LAN_NAME_HINTS):
            status["lan_connected"] = True
            status["lan_ip"] = ipv4
        elif any(hint in name_lower for hint in WIFI_NAME_HINTS):
            status["wifi_connected"] = True
            status["wifi_ip"] = ipv4

    if config.EXPECTED_LAN_IP:
        status["ip_matches_expected"] = (status["lan_ip"] == config.EXPECTED_LAN_IP)

    return status


def is_portal_reachable() -> bool:
    """Quick plain HTTP(S) check that the portal responds at all — this
    is distinct from LAN connectivity: the LAN can be perfectly fine
    while the portal itself is down for maintenance or outage."""
    try:
        req = urllib.request.Request(config.PORTAL_BASE_URL, method="GET")
        with urllib.request.urlopen(req, timeout=config.PORTAL_REACHABILITY_TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 500
    except Exception as e:
        logger.warning("Portal reachability check failed: %s", e)
        return False
