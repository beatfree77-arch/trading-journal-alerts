import unittest
import tempfile

from pathlib import Path
from datetime import datetime, timedelta, timezone

from bot import CalendarEvent, due_reminders, run_once


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


if __name__ == "__main__":
    unittest.main()
