#!/usr/bin/env python3
"""Post Biquote economic-calendar reminders to a Telegram channel."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# ============================================================
# CONFIGURATION
# ============================================================

CALENDAR_URL = (
    "https://biquote.io/api/calendar"
    "?countries=US%2CEU%2CGB%2CJP%2CAU%2CNZ%2CCA%2CCH"
    "&limit=500"
)

SUPPORTED_CURRENCIES = frozenset({
    "USD", "EUR", "GBP", "JPY",
    "AUD", "NZD", "CAD", "CHF",
})

ALLOWED_IMPACTS = frozenset({
    "high", "medium", "low", "holiday"
})

REMINDER_MINUTES = (15, 5)

ALERT_TOLERANCE_SECONDS = 300

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
# COUNTRY TO CURRENCY
# ============================================================

COUNTRY_CURRENCIES = {
    "US": "USD",
    "EU": "EUR",
    "GB": "GBP",
    "JP": "JPY",
    "AU": "AUD",
    "NZ": "NZD",
    "CA": "CAD",
    "CH": "CHF",
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
    """Expected bot error."""


class CalendarFeedError(BotError):
    """Calendar feed error."""


class TelegramDeliveryError(BotError):
    """Telegram delivery error."""


class StateError(BotError):
    """State file error."""


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass(frozen=True)
class CalendarEvent:

    currency: str
    name: str
    impact: str
    forecast: str
    previous: str
    event_time: datetime
    source_id: str
    time_mode: str

    @property
    def identity(self) -> str:

        stable_fields = (
            self.source_id
            or (
                f"{self.currency}|"
                f"{self.name}|"
                f"{self.event_time.isoformat()}"
            )
        )

        return hashlib.sha256(
            stable_fields.encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class RunSummary:

    due: int
    sent: int
    failed: int
    already_sent: int


# ============================================================
# IMPACT NORMALIZATION
# ============================================================

def normalize_impact(impact: str) -> str:

    value = impact.strip().lower()

    aliases = {
        "high impact": "high",
        "medium impact": "medium",
        "low impact": "low",

        "élevée": "high",
        "elevee": "high",
        "élevé": "high",
        "eleve": "high",

        "moyenne": "medium",
        "moyen": "medium",

        "faible": "low",

        "non-economic": "holiday",
        "non economic": "holiday",
    }

    return aliases.get(value, value)


# ============================================================
# CALENDAR PARSING
# ============================================================

def parse_event(row: Any) -> CalendarEvent | None:

    if not isinstance(row, dict):
        return None

    currency = _field_text(
        row.get("currency")
    ).upper()

    if not currency:

        country_code = _field_text(
            row.get("countryCode")
        ).upper()

        currency = COUNTRY_CURRENCIES.get(
            country_code,
            ""
        )

    if currency not in SUPPORTED_CURRENCIES:
        return None

    name = _field_text(row.get("name"))

    raw_time = _field_text(row.get("time"))

    if not name or not raw_time:
        return None

    try:

        event_time = datetime.fromisoformat(
            raw_time.replace("Z", "+00:00")
        )

    except ValueError:
        return None

    if (
        event_time.tzinfo is None
        or event_time.utcoffset() is None
    ):
        return None

    source_id = _field_text(
        row.get("id")
    )

    time_mode = _field_text(
        row.get("timeMode")
    ).lower()

    return CalendarEvent(

        currency=currency,

        name=name,

        impact=normalize_impact(
            _field_text(row.get("importance"))
        ),

        forecast=_field_text(
            row.get("forecast")
        ),

        previous=_field_text(
            row.get("previous")
        ),

        event_time=event_time.astimezone(
            timezone.utc
        ),

        source_id=source_id,

        time_mode=time_mode,
    )


def parse_calendar(payload: Any) -> list[CalendarEvent]:

    if isinstance(payload, dict):

        payload = (
            payload.get("events")
            or payload.get("data")
            or payload.get("results")
        )

    if not isinstance(payload, list):

        raise CalendarFeedError(
            "Biquote did not return a valid JSON event list."
        )

    events = []

    invalid_rows = 0

    for row in payload:

        event = parse_event(row)

        if event is not None:

            events.append(event)

        elif isinstance(row, dict):

            currency = _field_text(
                row.get("currency")
            ).upper()

            country = _field_text(
                row.get("countryCode")
            ).upper()

            if (
                currency in SUPPORTED_CURRENCIES
                or country in COUNTRY_CURRENCIES
            ):

                invalid_rows += 1

    if invalid_rows:

        LOGGER.warning(
            "Skipped %d invalid supported-currency event(s).",
            invalid_rows,
        )

    return events


# ============================================================
# FETCH CALENDAR
# ============================================================

def _retry_delay(
    attempt: int,
    retry_after: str | None = None,
) -> float:

    if retry_after:

        try:

            return min(
                max(float(retry_after), 0.0),
                8.0
            )

        except ValueError:
            pass

    return min(2 ** attempt, 8)


def fetch_calendar(
    opener: Callable[..., Any] = urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[CalendarEvent]:

    request = Request(

        CALENDAR_URL,

        headers={
            "Accept": "application/json",
            "User-Agent": "FOREX-FACTORY-JOURNAL/1.0",
        },
    )

    retryable_statuses = {
        429, 500, 502, 503, 504
    }

    for attempt in range(4):

        try:

            with opener(
                request,
                timeout=15
            ) as response:

                body = response.read(
                    MAX_CALENDAR_BYTES + 1
                )

            if len(body) > MAX_CALENDAR_BYTES:

                raise CalendarFeedError(
                    "Biquote response exceeded the size limit."
                )

            try:

                payload = json.loads(
                    body.decode("utf-8")
                )

            except (
                UnicodeDecodeError,
                json.JSONDecodeError
            ):

                raise CalendarFeedError(
                    "Biquote returned invalid JSON."
                ) from None

            return parse_calendar(payload)

        except HTTPError as exc:

            if (
                exc.code not in retryable_statuses
                or attempt == 3
            ):

                raise CalendarFeedError(
                    f"Biquote returned HTTP {exc.code}."
                ) from None

            retry_after = (
                exc.headers.get("Retry-After")
                if exc.headers else None
            )

            sleeper(
                _retry_delay(
                    attempt,
                    retry_after
                )
            )

        except (
            URLError,
            TimeoutError,
            OSError
        ):

            if attempt == 3:

                raise CalendarFeedError(
                    "Biquote could not be reached after retries."
                ) from None

            sleeper(
                _retry_delay(attempt)
            )

    raise CalendarFeedError(
        "Biquote could not be reached after retries."
    )


# ============================================================
# REMINDER SYSTEM
# ============================================================

def due_reminders(
    events: list[CalendarEvent],
    now: datetime,
) -> list[tuple[CalendarEvent, int]]:

    if (
        now.tzinfo is None
        or now.utcoffset() is None
    ):

        raise ValueError(
            "now must include a timezone"
        )

    now_utc = now.astimezone(timezone.utc)

    due = []

    for event in events:

        if event.time_mode != "exact":
            continue

        if event.impact not in ALLOWED_IMPACTS:
            continue

        seconds_until_event = (
            event.event_time - now_utc
        ).total_seconds()

        for lead_minutes in REMINDER_MINUTES:

            target_seconds = lead_minutes * 60

            if (
                target_seconds - ALERT_TOLERANCE_SECONDS
                <= seconds_until_event
                < target_seconds + ALERT_TOLERANCE_SECONDS
            ):

                due.append(
                    (event, lead_minutes)
                )

    return due


# ============================================================
# MESSAGE FORMATTING
# ============================================================

def format_event_time(
    event_time: datetime
) -> str:

    utc_time = event_time.astimezone(
        timezone.utc
    )

    return utc_time.strftime(
        "%H:%M UTC"
    )


def format_message(
    event: CalendarEvent,
    lead_minutes: int,
) -> str:

    style = IMPACT_STYLES.get(
        event.impact,

        {
            "title": "📢 ECONOMIC CALENDAR ALERT 📢",

            "footer": (
                "Stay informed. Follow the market carefully."
            ),
        },
    )

    flag = CURRENCY_FLAGS.get(
        event.currency,
        "🌍"
    )

    forecast = (
        event.forecast
        or "Not available"
    )

    previous = (
        event.previous
        or "Not available"
    )

    return (

        f"{style['title']}\n\n"

        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

        f"💱 Currency: {flag} {event.currency}\n"

        f"📊 Event: {event.name}\n\n"

       
