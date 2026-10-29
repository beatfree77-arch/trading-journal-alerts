import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import (
    BotError,
    CalendarEvent,
    StateError,
    alert_key,
    due_reminders,
    format_message,
    format_test_message,
    load_state,
    parse_calendar,
    parse_event,
    run_once,
    fetch_calendar,
    send_telegram_message,
    main,
)


class CalendarParsingTests(unittest.TestCase):
    def test_feed_fetch_retries_a_temporary_network_error(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return json.dumps(
                    [
                        {
                            "title": "US CPI",
                            "country": "USD",
                            "date": "2026-10-30T12:30:00Z",
                            "impact": "High",
                        }
                    ]
                ).encode("utf-8")

        attempts = []

        def flaky_opener(_request, timeout):
            attempts.append(timeout)
            if len(attempts) == 1:
                raise URLError("temporary outage")
            return FakeResponse()

        events = fetch_calendar(opener=flaky_opener, sleeper=lambda _seconds: None)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(events[0].currency, "USD")

    def test_parses_supported_currency_and_converts_time_to_utc(self):
        event = parse_event(
            {
                "title": "CPI m/m",
                "country": "USD",
                "date": "2026-10-30T08:30:00-04:00",
                "impact": "High",
                "forecast": "0.3%",
                "previous": "0.2%",
            }
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.event_time, datetime(2026, 10, 30, 12, 30, tzinfo=timezone.utc))

    def test_ignores_unsupported_currency_and_timezone_without_offset(self):
        self.assertIsNone(
            parse_event(
                {
                    "title": "China CPI",
                    "country": "CNY",
                    "date": "2026-10-30T08:30:00-04:00",
                }
            )
        )
        self.assertIsNone(
            parse_event(
                {
                    "title": "Unzoned event",
                    "country": "USD",
                    "date": "2026-10-30T08:30:00",
                }
            )
        )

    def test_parse_calendar_includes_all_eight_major_currencies(self):
        currencies = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"]
        rows = [
            {
                "title": f"Test {currency}",
                "country": currency,
                "date": "2026-10-30T12:30:00Z",
            }
            for currency in currencies
        ]
        events = parse_calendar(rows)
        self.assertEqual({event.currency for event in events}, set(currencies))

    def test_missing_values_are_shown_as_unavailable_and_source_is_linked(self):
        event = CalendarEvent(
            currency="EUR",
            name="ECB Presser",
            impact="High",
            forecast="",
            previous="",
            event_time=datetime(2026, 10, 30, 12, 30, tzinfo=timezone.utc),
        )
        message = format_message(event, 15)
        self.assertIn("Forecast: Not available", message)
        self.assertIn("Previous: Not available", message)
        self.assertIn("Impact: High", message)
        self.assertIn("Event time: 2026-10-30 12:30 UTC", message)
        self.assertIn("https://www.forexfactory.com/calendar", message)

    def test_test_message_is_labeled_and_uses_the_default_channel(self):
        message = format_test_message()
        self.assertIn("Telegram bot test message", message)
        self.assertIn("Channel: @trading_journal_alert", message)
        self.assertIn("No economic event is being reported.", message)

    def test_telegram_send_requires_token_and_accepts_success_response(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return b'{"ok": true, "result": {"message_id": 1}}'

        captured = []

        def fake_opener(request, timeout):
            captured.append((request, timeout))
            return FakeResponse()

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=True):
            send_telegram_message("test message", opener=fake_opener)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][1], 15)
        self.assertIn("test-token", captured[0][0].full_url)

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(BotError, "TELEGRAM_BOT_TOKEN is not set"):
                send_telegram_message("test message", opener=fake_opener)

    def test_cli_test_message_skips_calendar_fetch(self):
        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=True),
            patch("bot.send_telegram_message") as send_mock,
            patch("bot.fetch_calendar", side_effect=AssertionError("must not fetch")),
        ):
            self.assertEqual(main(["--test-message"]), 0)
        send_mock.assert_called_once()


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        self.event = CalendarEvent(
            currency="USD",
            name="CPI m/m",
            impact="High",
            forecast="0.3%",
            previous="0.2%",
            event_time=self.now + timedelta(minutes=15),
        )

    def test_matches_fifteen_and_five_minute_windows(self):
        due = due_reminders([self.event], self.now)
        self.assertEqual(due, [(self.event, 15)])
        five_minute_event = CalendarEvent(
            **{**self.event.__dict__, "event_time": self.now + timedelta(minutes=5)}
        )
        self.assertEqual(due_reminders([five_minute_event], self.now), [(five_minute_event, 5)])

    def test_does_not_match_outside_tolerance(self):
        late_event = CalendarEvent(
            **{**self.event.__dict__, "event_time": self.now + timedelta(minutes=11)}
        )
        self.assertEqual(due_reminders([late_event], self.now), [])

    def test_successful_alert_is_not_sent_twice(self):
        sent_messages = []
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            first = run_once(self.now, [self.event], state_path, sent_messages.append)
            second = run_once(self.now, [self.event], state_path, sent_messages.append)

            self.assertEqual(first.sent, 1)
            self.assertEqual(second.sent, 0)
            self.assertEqual(second.already_sent, 1)
            self.assertEqual(len(sent_messages), 1)
            self.assertIn(alert_key(self.event, 15), load_state(state_path))

    def test_failed_alert_is_not_marked_sent(self):
        def fail_send(_message):
            raise RuntimeError("offline")

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            result = run_once(self.now, [self.event], state_path, fail_send)
            self.assertEqual(result.failed, 1)
            self.assertEqual(load_state(state_path), {})

    def test_corrupt_state_fails_explicitly_instead_of_resetting(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text("{invalid", encoding="utf-8")
            with self.assertRaises(StateError):
                load_state(state_path)


if __name__ == "__main__":
    unittest.main()
