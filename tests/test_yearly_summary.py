import asyncio
import unittest
from datetime import datetime, time, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from test_admin_web import fixture

from event_manager import EventManager
from yearly_summary import YearlySummary

BANGKOK = ZoneInfo("Asia/Bangkok")
DUE = datetime(2026, 12, 25, 17, tzinfo=BANGKOK)


class YearlySummaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _, self.events, self.store, member, self.channel = fixture()
        self.channel.guild = member.guild
        self.channel.permissions_for = lambda _: SimpleNamespace(attach_files=True)
        self.events.bot.get_channel.return_value = self.channel
        self.summary = YearlySummary(self.events, BANGKOK, time(17), lambda: 123)

    async def poll(self, when="2026-12-24", guild="99", delivered=True):
        return await self.store._create(
            "Polls",
            {
                "Guild ID": guild,
                "Poll Date": when,
                "Message ID": "500" if delivered else "",
            },
        )

    async def answer(self, poll, choice="yes", user="1"):
        await self.store._create(
            "Responses",
            {
                "Poll Key": poll["id"],
                "Choice": choice,
                "User ID": user,
                "Reason": "PRIVATE REASON",
                "Joined Voice Chat": "yes",
            },
        )

    async def voice(self, joined, left=None, user="1", name="Rz", guild="99"):
        await self.store._create(
            "Voice Sessions",
            {
                "Guild ID": guild,
                "User ID": user,
                "Display Name": name,
                "Joined At": joined,
                "Left At": left,
            },
        )

    async def test_calendar_timezone_and_disabled_boundaries(self):
        for now in [
            datetime(2026, 12, 24, 23, 59, tzinfo=BANGKOK),
            datetime(2026, 12, 25, 9, 59, tzinfo=timezone.utc),
            datetime(2027, 1, 1, tzinfo=BANGKOK),
        ]:
            self.assertIsNone(await self.summary.run(now))
        self.channel.send.assert_not_awaited()
        self.assertTrue(
            self.summary.is_due(datetime(2026, 12, 25, 10, tzinfo=timezone.utc))
        )
        self.summary.enabled = False
        self.assertIsNone(await self.summary.run(DUE))
        with self.assertRaises(ValueError):
            self.summary.is_due(datetime(2026, 12, 25, 17))  # noqa: DTZ001 -- intentional invalid input

    async def test_readiness_gate(self):
        self.events.ready = False
        self.assertIsNone(await self.summary.run(DUE))
        self.assertEqual({}, self.store.rows)

    async def test_scoped_poll_totals_and_no_private_reasons(self):
        poll = await self.poll()
        await self.answer(poll)
        await self.answer(poll, "maybe", "2")
        await self.answer(poll, "no", "3")
        for poll in [
            await self.poll("2025-12-24"),
            await self.poll("2026-12-25"),
            await self.poll(guild="88"),
            await self.poll(delivered=False),
        ]:
            await self.answer(poll)
        event = await self.summary.run(DUE)
        totals = event["annual_summary"]
        self.assertEqual(
            (1, 3, 3), (totals["polls"], totals["responses"], totals["participants"])
        )
        self.assertEqual((1, 1, 1), (totals["yes"], totals["maybe"], totals["no"]))
        self.assertNotIn("PRIVATE REASON", event["body"])
        self.assertNotIn("Joined Voice Chat", event["body"])
        self.assertEqual("published", event["status"])

    async def test_voice_sessions_clip_to_bangkok_period(self):
        await self.voice("2025-12-31T23:00:00+07:00", "2026-01-01T01:00:00+07:00")
        await self.voice("2026-12-24T23:00:00+07:00", "2026-12-25T02:00:00+07:00")
        await self.voice("2026-12-24T22:00:00+07:00", user="2", name="Ahri")
        await self.voice("2026-12-25T00:00:00+07:00", user="3")
        await self.voice("bad timestamp", user="4")
        await self.voice("2026-12-24T20:00:00+07:00", guild="88")
        people = await self.summary.voice_totals("99", 2026)
        self.assertEqual(2, len(people))
        self.assertEqual([7200, 7200], [p["seconds"] for p in people])
        self.assertEqual([2, 1], [p["sessions"] for p in people])

    async def test_csv_lists_all_members_and_neutralizes_formulas(self):
        for i in range(15):
            await self.voice(
                "2026-06-01T12:00:00+07:00",
                "2026-06-01T13:00:00+07:00",
                user=str(i + 1),
                name="=HYPERLINK(test)" if i == 0 else f"Member {i}",
            )
        event = await self.summary.run(DUE)
        sent = self.channel.send.await_args.kwargs
        csv_text = sent["file"].fp.getvalue().decode("utf-8-sig")
        self.assertEqual(16, len(csv_text.splitlines()))
        self.assertIn("'=HYPERLINK(test)", csv_text)
        self.assertEqual(15, len(event["annual_summary"]["voice_members"]))
        self.assertLess(len(event["body"]), 3500)
        self.assertFalse(sent["allowed_mentions"].everyone)

    async def test_repeated_concurrent_checks_and_restart_do_not_duplicate(self):
        await asyncio.gather(self.summary.run(DUE), self.summary.run(DUE))
        restarted = EventManager(self.events.bot, self.store)
        restarted.channel = AsyncMock(return_value=self.channel)
        await restarted.restore()
        await YearlySummary(restarted, BANGKOK, time(17), lambda: 123).run(DUE)
        self.channel.send.assert_awaited_once()

    async def test_catchup_and_next_year(self):
        event = await self.summary.run(datetime(2026, 12, 31, 22, tzinfo=BANGKOK))
        self.assertEqual(2026, event["annual_summary"]["year"])
        next_event = await self.summary.run(datetime(2027, 12, 25, 17, tzinfo=BANGKOK))
        self.assertNotEqual(event["key"], next_event["key"])
        self.assertEqual(2, self.channel.send.await_count)

    async def test_channel_change_does_not_repost_same_server_year(self):
        await self.summary.run(DUE)
        self.summary.get_channel_id = lambda: 456
        await self.summary.run(DUE)
        self.channel.send.assert_awaited_once()

    async def test_missing_attachment_permission_retries_before_delivery(self):
        await self.voice("2026-12-24T22:00:00+07:00")
        self.channel.permissions_for = lambda _: SimpleNamespace(attach_files=False)
        with self.assertRaisesRegex(ValueError, "Attach Files"):
            await self.summary.run(DUE)
        self.channel.send.assert_not_awaited()
        self.assertEqual([], await self.store.list_records("Admin Events"))
        self.channel.permissions_for = lambda _: SimpleNamespace(attach_files=True)
        await self.summary.run(DUE)
        self.channel.send.assert_awaited_once()

    async def test_uncertain_send_is_not_repeated(self):
        self.channel.send.side_effect = TimeoutError("uncertain delivery")
        with self.assertLogs("event_manager", level="ERROR"):
            event = await self.summary.run(DUE)
        self.assertEqual("review", event["status"])
        await self.summary.run(DUE)
        self.channel.send.assert_awaited_once()

    async def test_lost_create_response_is_recovered_by_stable_key(self):
        original = self.store._create

        async def uncertain_create(table, fields):
            await original(table, fields)
            raise TimeoutError("saved, but response lost")

        self.store._create = uncertain_create
        with self.assertRaises(TimeoutError):
            await self.summary.run(DUE)
        self.store._create = original
        event = await self.summary.run(DUE)
        self.assertEqual("published", event["status"])
        self.assertEqual(1, len(await self.store.list_records("Admin Events")))
        self.channel.send.assert_awaited_once()
