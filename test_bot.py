import unittest
import tempfile

from pathlib import Path
from datetime import datetime, timedelta, timezone

from bot import (
    CalendarEvent,
    due_reminders,
    run_once,
    format_message,
)


class ReminderTests(unittest.TestCase):

    def setUp(self):
        self.now = datetime(
            2026, 11, 2, 14, 0,
            tzinfo=timezone.utc
        )

    def make_event(self, minutes, time_mode="exact"):
        return CalendarEvent(
            currency="USD",
            name="Test Economic Event",
            impact="high",
            forecast="3.0%",
            previous="2.5%",
            event_time=self.now + timedelta(minutes=minutes),
            source_id="test-event-001",
            time_mode=time_mode,
        )

    def test_t15_reminder(self):
        event = self.make_event(15)
        reminders = due_reminders([event], self.now)

        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0][1], 15)

    def test_t5_reminder(self):
        event = self.make_event(5)
        reminders = due_reminders([event], self.now)

        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0][1], 5)

    def test_tentative_event_is_ignored(self):
        event = self.make_event(15, "tentative")
        reminders = due_reminders([event], self.now)

        self.assertEqual(len(reminders), 0)

    def test_date_only_event_is_ignored(self):
        event = self.make_event(15, "date")
        reminders = due_reminders([event], self.now)

        self.assertEqual(len(reminders), 0)

    def test_duplicate_alert_is_not_sent_twice(self):
        event = self.make_event(15)

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "sent_alerts.json"
            messages = []

            first = run_once(
                now=self.now,
                events=[event],
                state_path=state_path,
                sender=messages.append,
            )

            second = run_once(
                now=self.now,
                events=[event],
                state_path=state_path,
                sender=messages.append,
            )

            self.assertEqual(first.sent, 1)
            self.assertEqual(second.sent, 0)
            self.assertEqual(second.already_sent, 1)
            self.assertEqual(len(messages), 1)


class MessageFormattingTests(unittest.TestCase):

    def setUp(self):
        self.event = CalendarEvent(
            currency="USD",
            name="JOLTS Job Openings",
            impact="high",
            forecast="7.2M",
            previous="7.1M",
            event_time=datetime(
                2026, 11, 3, 15, 0,
                tzinfo=timezone.utc
            ),
            source_id="jolts-test-001",
            time_mode="exact",
        )

    def test_high_impact_title(self):
        message = format_message(self.event, 15)

        self.assertIn("HIGH IMPACT", message)

    def test_currency_and_flag(self):
        message = format_message(self.event, 15)

        self.assertIn("🇺🇸 USD", message)

    def test_event_name(self):
        message = format_message(self.event, 15)

        self.assertIn("JOLTS Job Openings", message)

    def test_forecast_and_previous(self):
        message = format_message(self.event, 15)

        self.assertIn("7.2M", message)
        self.assertIn("7.1M", message)

    def test_countdown(self):
        message = format_message(self.event, 15)

        self.assertIn("15 minutes", message)

    def test_event_time(self):
        message = format_message(self.event, 15)

        self.assertIn("15:00 UTC", message)

    def test_missing_values(self):
        event = CalendarEvent(
            currency="USD",
            name="Test Event",
            impact="high",
            forecast="",
            previous="",
            event_time=self.event.event_time,
            source_id="missing-test-001",
            time_mode="exact",
        )

        message = format_message(event, 5)

        self.assertIn("Not available", message)


if __name__ == "__main__":
    unittest.main()
