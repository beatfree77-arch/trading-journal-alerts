#!/usr/bin/env python3
"""Post Forex Factory economic-calendar reminders to a Telegram channel."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# ============================================================
# CONFIGURATION
# ============================================================

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SOURCE_URL = "https://www.forexfactory.com/calendar"

SUPPORTED_CURRENCIES = frozenset({
    "USD", "EUR", "GBP", "JPY",
    "AUD", "NZD", "CAD", "CHF",
})

# All four impact categories are enabled.
ALLOWED_IMPACTS = frozenset({
    "high", "medium", "low", "holiday"
})

REMINDER_MINUTES = (15, 5)

ALERT_TOLERANCE_SECONDS = 210

DEFAULT_CHANNEL = "@trading_journal_alert"

DEFAULT_STATE_PATH = (
    Path(__file__).parent / "state" / "sent_alerts.json"
)

MAX_CALENDAR_BYTES = 4_000_000
MAX_TELEGRAM_RESPONSE_BYTES = 64_000

LOGGER = logging.getLogger("calendar_bot")


# ============================================================
# CURRENCY FLAGS
# ============================================================

CURRENCY_FLAGS = {
    "USD": "🇺🇸",
    "EUR": "🇪🇺",
    "GBP": "🇬🇧",
    "JPY": "🇯🇵",
    "AUD": "🇦🇺",
    "NZD": "🇳🇿",
    "CAD": "🇨🇦",
    "CHF": "🇨🇭",
}


# ============================================================
# IMPACT STYLES
# ============================================================

IMPACT_STYLES = {
    "high": {
        "title": "🔴 HIGH IMPACT ECONOMIC ALERT 🔴",
        "footer": (
            "⚡ High-Impact Release • Markets on high alert\n"
            "Stay sharp. Volatility incoming. 🚀"
        ),
    },

    "medium": {
        "title": "🟢 MEDIUM IMPACT ECONOMIC ALERT 🟢",
        "footer": (
            "📊 Medium-Impact Release • Markets may react\n"
            "Stay focused. Watch for market movements. 📈"
        ),
    },

    "low": {
        "title": "🔵 LOW IMPACT ECONOMIC ALERT 🔵",
        "footer": (
            "📊 Low-Impact Release • Limited market reaction expected\n"
            "Stay informed. Keep an eye on the markets. 👀"
        ),
    },

    "holiday": {
        "title": "⚪ ECONOMIC HOLIDAY ALERT ⚪",
        "footer": (
            "📅 Market Holiday • Trading activity may be affected\n"
            "Stay aware. Check market conditions. ⚠️"
        ),
    },
}


def _field_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


# ============================================================
# ERRORS
# ============================================================

class BotError(Exception):
    """An expected configuration, feed, or delivery error."""


class CalendarFeedError(BotError):
    """The calendar
