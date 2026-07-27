"""Local MAC OUI -> vendor lookup.

Resolves the first 3 bytes of a MAC (the IEEE-assigned vendor prefix) to a
manufacturer name using a bundled copy of the public IEEE OUI registry.
Runs entirely offline; no MAC ever leaves the network.
"""

from __future__ import annotations

import csv
import logging
import os

_LOGGER = logging.getLogger(__name__)

_OUI_FILE = os.path.join(os.path.dirname(__file__), "oui_db.csv")
_oui: dict[str, str] | None = None

# short, friendly names for common vendors so trackers read nicely
_FRIENDLY = {
    "Espressif": "Espressif",
    "TPVision": "Philips TV",
    "Tuya Smart": "Tuya",
    "CANON": "Canon",
    "NETGEAR": "Netgear",
    "Microsoft": "Microsoft",
    "Shenzhen HongRui Optical": "2.5G Switch",
}


def _load() -> dict[str, str]:
    global _oui
    if _oui is not None:
        return _oui
    table: dict[str, str] = {}
    try:
        with open(_OUI_FILE, encoding="utf-8", errors="replace") as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    table[row[0]] = row[1]
    except OSError as err:
        _LOGGER.warning("Could not load OUI database: %s", err)
    _oui = table
    return table


def preload() -> None:
    """Load the OUI table into memory (call from an executor thread)."""
    _load()


def vendor(mac: str) -> str | None:
    """Return the manufacturer name for a MAC, or None if unknown."""
    if not mac:
        return None
    prefix = mac.replace(":", "").replace("-", "").upper()[:6]
    if len(prefix) < 6:
        return None
    name = _load().get(prefix)
    if not name:
        return None
    for key, short in _FRIENDLY.items():
        if name.startswith(key):
            return short
    return name


def short_label(mac: str) -> str:
    """A compact display label like 'Canon 3f24' for an unnamed device."""
    v = vendor(mac)
    tail = mac.replace(":", "")[-4:] if mac else "????"
    if v:
        return f"{v} {tail}"
    return mac
