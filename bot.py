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
DEFAULT_STATE_PATH = Path(__file__).parent / "state" / "sent_alerts.json"

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
    """The calendar feed could not be fetched or parsed."""


class TelegramDeliveryError(BotError):
    """Telegram did not confirm delivery."""


class StateError(BotError):
    """The alert state file is unreadable or invalid."""


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

    @property
    def identity(self) -> str:
        stable_fields = (
            self.currency,
            self.name,
            self.event_time.astimezone(timezone.utc).isoformat(timespec="seconds"),
        )
        return hashlib.sha256("|".join(stable_fields).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunSummary:
    due: int
    sent: int
    failed: int
    already_sent: int


# ============================================================
# CALENDAR PARSING
# ============================================================

def parse_event(row: Any) -> CalendarEvent | None:
    """Convert one feed row to an event."""
    if not isinstance(row, dict):
        return None

    currency = _field_text(row.get("country")).upper()
    if currency not in SUPPORTED_CURRENCIES:
        return None

    name = _field_text(row.get("title"))
    raw_time = _field_text(row.get("date"))
    if not name or not raw_time:
        return None

    try:
        event_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except ValueError:
        return None

    if event_time.tzinfo is None or event_time.utcoffset() is None:
        return None

    return CalendarEvent(
        currency=currency,
        name=name,
        impact=_field_text(row.get("impact")) or "Not provided",
        forecast=_field_text(row.get("forecast")),
        previous=_field_text(row.get("previous")),
        event_time=event_time.astimezone(timezone.utc),
    )


def parse_calendar(payload: Any) -> list[CalendarEvent]:
    if not isinstance(payload, list):
        raise CalendarFeedError("The calendar feed did not return a JSON list.")

    events: list[CalendarEvent] = []
    invalid_rows = 0

    for row in payload:
        event = parse_event(row)
        if event is not None:
            events.append(event)
        elif isinstance(row, dict):
            currency = _field_text(row.get("country")).upper()
            if currency in SUPPORTED_CURRENCIES:
                invalid_rows += 1

    if invalid_rows:
        LOGGER.warning(
            "Skipped %d supported-currency row(s) with invalid event data.",
            invalid_rows,
        )

    return events


# ============================================================
# FETCH CALENDAR
# ============================================================

def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 8.0)
        except ValueError:
            pass
    return min(2**attempt, 8)


def fetch_calendar(
    opener: Callable[..., Any] = urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[CalendarEvent]:
    request = Request(
        CALENDAR_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "trading-journal-calendar-alerts/1.0",
        },
    )

    retryable_statuses = {429, 500, 502, 503, 504}

    for attempt in range(4):
        try:
            with opener(request, timeout=15) as response:
                body = response.read(MAX_CALENDAR_BYTES + 1)

            if len(body) > MAX_CALENDAR_BYTES:
                raise CalendarFeedError(
                    "The calendar feed response exceeded the size limit."
                )

            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise CalendarFeedError(
                    "The calendar feed returned invalid JSON."
                ) from None

            return parse_calendar(payload)

        except HTTPError as exc:
            if exc.code not in retryable_statuses or attempt == 3:
                raise CalendarFeedError(
                    f"The calendar feed returned HTTP {exc.code}."
                ) from None

            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            sleeper(_retry_delay(attempt, retry_after))

        except (URLError, TimeoutError, OSError):
            if attempt == 3:
                raise CalendarFeedError(
                    "The calendar feed could not be reached after retries."
                ) from None
            sleeper(_retry_delay(attempt))

    raise CalendarFeedError(
        "The calendar feed could not be reached after retries."
    )


# ============================================================
# REMINDER SYSTEM
# ============================================================

def normalize_impact(impact: str) -> str:
    value = impact.strip().lower()
    aliases = {
        "high impact": "high",
        "medium impact": "medium",
        "low impact": "low",
        "non-economic": "holiday",
        "non economic": "holiday",
    }
    return aliases.get(value, value)


def due_reminders(
    events: list[CalendarEvent],
    now: datetime,
) -> list[tuple[CalendarEvent, int]]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")

    now_utc = now.astimezone(timezone.utc)
    due: list[tuple[CalendarEvent, int]] = []

    for event in events:
        if normalize_impact(event.impact) not in ALLOWED_IMPACTS:
            continue

        seconds_until_event = (event.event_time - now_utc).total_seconds()

        for lead_minutes in REMINDER_MINUTES:
            target_seconds = lead_minutes * 60
            if abs(seconds_until_event - target_seconds) <= ALERT_TOLERANCE_SECONDS:
                due.append((event, lead_minutes))

    return due


# ============================================================
# MESSAGE FORMATTING
# ============================================================

def format_event_time(event_time: datetime) -> str:
    utc_time = event_time.astimezone(timezone.utc)
    return utc_time.strftime("%H:%M UTC")


def format_message(event: CalendarEvent, lead_minutes: int) -> str:
    impact_key = normalize_impact(event.impact)
    style = IMPACT_STYLES.get(
        impact_key,
        {
            "title": "📢 ECONOMIC CALENDAR ALERT 📢",
            "footer": "Stay informed. Follow the market carefully.",
        },
    )

    flag = CURRENCY_FLAGS.get(event.currency, "🌍")
    forecast = event.forecast or "Not available"
    previous = event.previous or "Not available"

    return (
        f"{style['title']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Currency: {flag} {event.currency}\n"
        f"📊 Event: {event.name}\n\n"
        f"⏳ Reminder: {lead_minutes} minutes\n"
        f"🕒 Event Time: {format_event_time(event.event_time)}\n\n"
        f"🟩 Forecast: {forecast}\n"
        f"🟥 Previous: {previous}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{style['footer']}"
    )


# ============================================================
# TEST MESSAGE
# ============================================================

def format_test_message(channel: str | None = None) -> str:
    target = (
        channel
        or os.environ.get("TELEGRAM_CHANNEL")
        or DEFAULT_CHANNEL
    ).strip()

    return (
        "Telegram bot test message\n"
        "This confirms the bot can publish to the configured channel.\n"
        f"Channel: {target}\n"
        "No economic event is being reported."
    )


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

def alert_key(event: CalendarEvent, lead_minutes: int) -> str:
    return f"{event.identity}:{lead_minutes}"


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        raise StateError(
            "The saved alert state could not be read; "
            "refusing to reset deduplication."
        ) from None

    sent = raw.get("sent") if isinstance(raw, dict) else None
    if not isinstance(sent, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in sent.items()
    ):
        raise StateError("The saved alert state has an invalid format.")

    return sent


def save_state(path: Path, sent: dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(path.suffix + ".tmp")

        data = json.dumps({"sent": sent}, indent=2, sort_keys=True) + "\n"
        temporary_path.write_text(data, encoding="utf-8")
        temporary_path.replace(path)
    except OSError:
        raise StateError("The sent-alert state could not be saved.") from None


def prune_state(sent: dict[str, str], now: datetime) -> dict[str, str]:
    cutoff = now.astimezone(timezone.utc) - timedelta(days=14)
    pruned: dict[str, str] = {}

    for key, value in sent.items():
        try:
            recorded_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue

        if (
            recorded_at.tzinfo is not None
            and recorded_at.astimezone(timezone.utc) >= cutoff
        ):
            pruned[key] = value

    return pruned


# ============================================================
# TELEGRAM DELIVERY
# ============================================================

def send_telegram_message(
    text: str,
    channel: str | None = None,
    opener: Callable[..., Any] = urlopen,
) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise BotError("TELEGRAM_BOT_TOKEN is not set.")

    target = (
        channel
        or os.environ.get("TELEGRAM_CHANNEL")
        or DEFAULT_CHANNEL
    ).strip()
    if not target:
        raise BotError("TELEGRAM_CHANNEL cannot be empty.")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urlencode({
        "chat_id": target,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with opener(request, timeout=15) as response:
            response_body = response.read(MAX_TELEGRAM_RESPONSE_BYTES + 1)
            status = getattr(response, "status", 200)
    except HTTPError as exc:
        raise TelegramDeliveryError(
            f"Telegram returned HTTP {exc.code}."
        ) from None
    except (URLError, TimeoutError, OSError):
        raise TelegramDeliveryError(
            "Telegram could not be reached or timed out."
        ) from None
    except Exception as exc:
        raise TelegramDeliveryError(
            f"Telegram request failed ({type(exc).__name__})."
        ) from None

    if len(response_body) > MAX_TELEGRAM_RESPONSE_BYTES:
        raise TelegramDeliveryError("Telegram returned an oversized response.")

    if status < 200 or status >= 300:
        raise TelegramDeliveryError(f"Telegram returned HTTP {status}.")

    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TelegramDeliveryError(
            "Telegram returned an invalid response."
        ) from None

    if not isinstance(result, dict) or result.get("ok") is not True:
        raise TelegramDeliveryError("Telegram did not confirm the message.")


# ============================================================
# MAIN BOT EXECUTION
# ============================================================

def run_once(
    now: datetime | None = None,
    events: list[CalendarEvent] | None = None,
    state_path: Path = DEFAULT_STATE_PATH,
    sender: Callable[[str], None] = send_telegram_message,
) -> RunSummary:
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("now must include a timezone")

    run_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    calendar_events = events if events is not None else fetch_calendar()
    reminders = due_reminders(calendar_events, run_time)

    sent_state = prune_state(load_state(state_path), run_time)

    sent_count = 0
    failed_count = 0
    duplicate_count = 0

    for event, lead_minutes in reminders:
        key = alert_key(event, lead_minutes)

        if key in sent_state:
            duplicate_count += 1
            continue

        try:
            sender(format_message(event, lead_minutes))
        except BotError as exc:
            LOGGER.warning(
                "Alert delivery failed (%s): %s",
                type(exc).__name__,
                exc,
            )
            failed_count += 1
            continue
        except Exception as exc:
            LOGGER.warning(
                "Alert delivery failed (%s).",
                type(exc).__name__,
            )
            failed_count += 1
            continue

        sent_state[key] = run_time.isoformat()
        save_state(state_path, sent_state)
        sent_count += 1

    return RunSummary(
        due=len(reminders),
        sent=sent_count,
        failed=failed_count,
        already_sent=duplicate_count,
    )


# ============================================================
# COMMAND LINE
# ============================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)

    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Fetch the calendar and print any due alerts "
            "without sending or changing state."
        ),
    )
    modes.add_argument(
        "--test-message",
        action="store_true",
        help=(
            "Send one labeled test message without fetching "
            "the calendar or changing alert state."
        ),
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.dry_run and not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        LOGGER.error(
            "TELEGRAM_BOT_TOKEN is not set. "
            "Add it as a secret environment variable."
        )
        return 2

    if args.test_message:
        try:
            send_telegram_message(format_test_message())
        except BotError as exc:
            LOGGER.error("%s", exc)
            return 1

        LOGGER.info(
            "Telegram accepted the test message. "
            "Check the configured channel."
        )
        return 0

    try:
        events = fetch_calendar()
    except CalendarFeedError as exc:
        LOGGER.error("%s", exc)
        return 1

    current_time = datetime.now(timezone.utc)

    if args.dry_run:
        reminders = due_reminders(events, current_time)
        if not reminders:
            print(
                f"Calendar loaded ({len(events)} supported-currency events); "
                "no alerts are due now."
            )
        else:
            print(f"Dry run: {len(reminders)} alert(s) would be sent:")
            for event, lead_minutes in reminders:
                print("-" * 60)
                print(format_message(event, lead_minutes))
        return 0

    try:
        summary = run_once(now=current_time, events=events)
    except StateError as exc:
        LOGGER.error("%s", exc)
        return 1

    LOGGER.info(
        "Checked %d supported-currency events; "
        "due=%d, sent=%d, already sent=%d, failed=%d.",
        len(events),
        summary.due,
        summary.sent,
        summary.already_sent,
        summary.failed,
    )

    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
