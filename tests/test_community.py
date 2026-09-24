import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from test_admin_web import MemoryStore, event_input, fixture

from backups import Backups
from community import Community
from event_manager import EventManager, utcnow
from game_poll import GamePlanView, GamePollService, report_embed
from planning import BANGKOK, report_period, suggest, validate_plan


def setup():
    store = MemoryStore()
    bot = MagicMock()
    bot.user = SimpleNamespace(id=10)
    bot.guilds = []
    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.guild = 123, SimpleNamespace(id=99)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=500))
    channel.fetch_message = AsyncMock(
        return_value=SimpleNamespace(
            author=bot.user, edit=AsyncMock(return_value=SimpleNamespace(id=400))
        )
    )
    bot.get_channel.return_value = channel
    polls = GamePollService(bot, store, [123], "Asia/Bangkok")
    events = EventManager(bot, store)
    events.ready = True
    events.channel = AsyncMock(return_value=channel)
    community = Community(bot, store, polls, events)
    community.ready = True
    polls.community = community
    return community, store, channel


def response(user_id, games=None, start="19:00", end="22:00", choice="yes"):
    return {
        "user_id": user_id,
        "choice": choice,
        "display_name": f"Member {user_id}",
        "play_time": start,
        "plan": {
            "games": games or ["League of Legends"],
            "until": end,
            "reminder": False,
        },
    }


class PlanningTests(unittest.TestCase):
    def test_suggestion_uses_overlap_and_distinguishes_games(self):
        result = suggest(
            [response(1), response(2, ["Any game"], "20:00"), response(3, ["Valorant"])]
        )
        self.assertEqual(
            ("League of Legends", "20:00", 2),
            (result["game"], result["time"], result["count"]),
        )
        self.assertEqual(["1", "2"], result["user_ids"])

    def test_no_guesses_from_legacy_maybe_or_short_overlap(self):
        self.assertIsNone(
            suggest(
                [
                    response(1),
                    response(2, choice="maybe"),
                    {"choice": "yes", "play_time": "Flexible"},
                ]
            )
        )
        self.assertIsNone(suggest([response(1, end="19:20"), response(2)]))
        self.assertIsNone(
            suggest([response(1, start="21:00"), response(2, end="21:00")])
        )

    def test_validation_rejects_bad_window_and_games(self):
        for p in (
            {"games": ["Any game", "Valorant"], "until": "22:00"},
            {"games": ["Hacked"], "until": "22:00"},
            {"games": ["Valorant"], "until": "18:00"},
        ):
            with self.assertRaises(ValueError):
                validate_plan(p, "19:00")

    def test_period_uses_bangkok_midnight_and_inclusive_end_date(self):
        now = datetime(2026, 9, 24, 1, tzinfo=BANGKOK)
        start, end = report_period({"period": "today"}, now)
        self.assertEqual("2026-09-24T00:00:00+07:00", start.isoformat())
        self.assertEqual(86400, (end - start).total_seconds())
        self.assertEqual(
            date(2026, 9, 21), report_period({"period": "week"}, now)[0].date()
        )
        self.assertEqual(
            date(2026, 1, 1), report_period({"period": "year"}, now)[0].date()
        )
        for query in (
            {"period": "custom", "from": "2026-09-25", "to": "2026-09-25"},
            {"period": "custom", "from": "2026-09-20", "to": "2026-09-19"},
            {"period": "oops"},
        ):
            with self.assertRaises(ValueError):
                report_period(query, now)

    def test_summary_privacy_and_suggestion(self):
        report = {
            "yes_count": 2,
            "no_count": 1,
            "responses": [
                response(1),
                response(2),
                {
                    "user_id": 3,
                    "choice": "no",
                    "display_name": "No voter",
                    "reason": "private reason",
                },
            ],
            "public_reasons": False,
            "suggestion": suggest([response(1), response(2)]),
        }
        embed = report_embed(date(2026, 9, 24), "Asia/Bangkok", report)
        self.assertNotIn("private reason", str(embed.to_dict()))
        self.assertIn("not a confirmed booking", embed.fields[-1].value)


class CommunityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.community, self.store, self.channel = setup()

    async def test_state_preferences_survive_restart_and_fail_closed_before_load(self):
        await self.community.put(
            "prefs:99:1", 99, "prefs", {"tracking": False, "reminders": False}
        )
        restarted = Community(
            self.community.bot, self.store, self.community.polls, self.community.events
        )
        self.assertFalse(restarted.tracking(99, 2))
        await restarted.restore()
        self.assertFalse(restarted.tracking(99, 1))
        self.assertTrue(restarted.tracking(99, 2))

    async def test_preview_does_not_send_and_manual_resend_is_idempotent(self):
        self.store.get_poll = AsyncMock(
            return_value={"id": "p1", "status": "closed", "guild_id": 99}
        )
        self.store.get_report = AsyncMock(
            return_value={"message_id": 400, "responses": [], "yes_count": 0}
        )
        data = {"channel_id": "123", "date": "2026-09-24", "action": "preview"}
        self.assertIn("embed", await self.community.report_action(99, 1, data))
        self.channel.send.assert_not_awaited()
        data.update(action="resend", confirmed=True, request_id="a" * 32)
        results = await asyncio.gather(
            *(self.community.report_action(99, 1, data) for _ in range(2))
        )
        self.assertEqual("sent", results[0]["task"]["status"])
        self.assertEqual("sent", results[1]["task"]["status"])
        self.channel.send.assert_awaited_once()

    async def test_update_edits_original_and_requires_confirmation_and_own_guild(self):
        self.store.get_poll = AsyncMock(
            return_value={"id": "p1", "status": "closed", "guild_id": 99}
        )
        self.store.get_report = AsyncMock(
            return_value={"message_id": 400, "responses": []}
        )
        data = {
            "channel_id": "123",
            "date": "2026-09-24",
            "action": "update",
            "request_id": "b" * 32,
        }
        with self.assertRaises(ValueError):
            await self.community.report_action(99, 1, data)
        with self.assertRaises(ValueError):
            await self.community.report_action(77, 1, dict(data, confirmed=True))
        result = await self.community.report_action(99, 1, dict(data, confirmed=True))
        self.assertEqual("400", result["task"]["message_id"])
        self.channel.send.assert_not_awaited()

    async def test_uncertain_send_is_not_retried(self):
        self.store.get_poll = AsyncMock(
            return_value={"id": "p1", "status": "closed", "guild_id": 99}
        )
        self.store.get_report = AsyncMock(
            return_value={"message_id": 400, "responses": []}
        )
        self.channel.send.side_effect = TimeoutError()
        data = {
            "channel_id": "123",
            "date": "2026-09-24",
            "action": "resend",
            "request_id": "c" * 32,
            "confirmed": True,
        }
        with self.assertRaises(TimeoutError):
            await self.community.report_action(99, 1, data)
        self.assertEqual(
            "review",
            (await self.community.report_action(99, 1, data))["task"]["status"],
        )
        self.channel.send.assert_awaited_once()

    async def test_opted_out_member_is_not_tracked_or_counted_as_solo(self):
        await self.community.put("prefs:99:1", 99, "prefs", {"tracking": False})
        self.store.start_voice_session = AsyncMock()
        self.store.sync_solo_voice_channel = AsyncMock()
        member = SimpleNamespace(
            id=1, display_name="Rz", bot=False, guild=SimpleNamespace(id=99)
        )
        self.channel.members = [member]
        await self.community.polls.track_voice_session(
            member, None, self.channel, date(2026, 9, 24)
        )
        await self.community.polls.track_solo_voice_channels(
            (self.channel,), date(2026, 9, 24)
        )
        self.store.start_voice_session.assert_not_awaited()
        self.assertIsNone(self.store.sync_solo_voice_channel.await_args.args[3])

    async def test_daily_uncertain_delivery_recovers_without_resending(self):
        self.store.set_poll_message = AsyncMock(side_effect=TimeoutError())
        poll_date = date(2026, 9, 24)
        embed = discord.Embed(title="Daily poll")
        embed.set_footer(text="Teemo")
        with self.assertRaises(TimeoutError):
            await self.community.polls.deliver_daily(
                self.channel, {"id": "p1"}, poll_date, "poll", embed
            )
        self.assertFalse(
            await self.community.polls.deliver_daily(
                self.channel, {"id": "p1"}, poll_date, "poll", embed
            )
        )
        key = "daily:poll:123:2026-09-24"
        self.assertEqual("review", self.community.get(key)["status"])

        async def history(**kwargs):
            yield SimpleNamespace(id=499, author=SimpleNamespace(id=55), embeds=[embed])
            yield SimpleNamespace(
                id=500, author=self.community.bot.user, embeds=[embed]
            )

        self.channel.history = history
        self.store.set_poll_message.side_effect = None
        with self.assertRaises(ValueError):
            await self.community.recover_task(77, key)
        result = await self.community.recover_task(99, key)
        self.assertEqual("sent", result["status"])
        self.assertEqual("500", result["message_id"])
        self.store.set_poll_message.assert_awaited_with("p1", 500)
        self.channel.send.assert_awaited_once()

    async def test_pending_plan_is_excluded_until_vote_confirmed(self):
        poll = {"id": "p1", "guild_id": 99, "poll_date": "2026-09-24"}
        report = {"responses": [response(1), response(2)]}
        for user_id in (1, 2):
            await self.community.save_plan(
                poll,
                user_id,
                {"games": ["League of Legends"], "until": "22:00", "reminder": False},
                "19:00",
            )
        self.assertIsNone(self.community.enrich(poll, report)["suggestion"])
        for user_id in (1, 2):
            await self.community.confirm_plan("p1", user_id)
        self.assertEqual(2, self.community.enrich(poll, report)["suggestion"]["count"])

    async def test_plan_view_has_selectors_and_explicit_save(self):
        view = GamePlanView(self.community.polls, 123, "19:00")
        self.assertEqual(4, len(view.children))
        self.assertFalse(view.reminder)

    async def seed_reminders(self):
        poll = {"id": "p1", "guild_id": 99, "poll_date": "2026-09-24"}
        for user_id in (1, 2):
            await self.community.save_plan(
                poll,
                user_id,
                {"games": ["Valorant"], "until": "22:00", "reminder": True},
                "19:00",
            )
            await self.community.confirm_plan("p1", user_id)
        self.store.get_report = AsyncMock(
            return_value={
                "responses": [response(1, ["Valorant"]), response(2, ["Valorant"])]
            }
        )
        user = SimpleNamespace(send=AsyncMock())
        self.community.bot.get_user.return_value = user
        return user

    async def test_reminders_are_once_opt_in_and_no_late_send(self):
        user = await self.seed_reminders()
        now = datetime(2026, 9, 24, 18, 45, tzinfo=BANGKOK)
        await self.community.reminder_tick(now)
        await self.community.reminder_tick(now + timedelta(minutes=1))
        self.assertEqual(2, user.send.await_count)
        self.store.get_report.assert_awaited_once()
        await self.community.reminder_tick(now + timedelta(hours=1))
        self.assertEqual(2, user.send.await_count)

    async def test_quiet_hours_and_unsubscribe_suppress_reminders(self):
        user = await self.seed_reminders()
        await self.community.put("prefs:99:1", 99, "prefs", {"reminders": False})
        await self.community.put(
            "prefs:99:2",
            99,
            "prefs",
            {"reminders": True, "quiet_start": "18:00", "quiet_end": "20:00"},
        )
        await self.community.reminder_tick(
            datetime(2026, 9, 24, 18, 45, tzinfo=BANGKOK)
        )
        user.send.assert_not_awaited()
        self.assertEqual("skipped", self.community.get("reminder:p1:2")["status"])

    async def test_template_is_paused_until_enabled_and_occurrence_not_duplicated(self):
        source = await self.community.events.create(event_input(), 99, 1)
        due = utcnow() + timedelta(hours=1)
        template = await self.community.create_template(
            99,
            1,
            {"event_key": source["key"], "name": "Friday", "first_at": due.isoformat()},
        )
        self.assertFalse(template["enabled"])
        await self.community.template_tick(due)
        self.channel.send.assert_not_awaited()
        await self.community.template_action(
            99, template["key"], "toggle", {"enabled": True}, 1
        )
        await self.community.template_tick(due)
        await self.community.template_tick(due)
        self.channel.send.assert_awaited_once()
        self.assertEqual(
            due + timedelta(days=7),
            datetime.fromisoformat(self.community.get(template["key"])["next_at"]),
        )

    async def test_missed_template_does_not_flood_and_guild_scope_is_checked(self):
        source = await self.community.events.create(event_input(), 99, 1)
        due = utcnow() + timedelta(hours=1)
        template = await self.community.create_template(
            99,
            1,
            {"event_key": source["key"], "name": "Friday", "first_at": due.isoformat()},
        )
        with self.assertRaises(ValueError):
            await self.community.template_action(
                77, template["key"], "toggle", {"enabled": True}, 1
            )
        await self.community.template_action(
            99, template["key"], "toggle", {"enabled": True}, 1
        )
        await self.community.template_tick(due + timedelta(days=30))
        self.channel.send.assert_not_awaited()


class BackupTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_remaps_poll_ids_and_never_overwrites_or_reopens(self):
        community, store, _ = setup()
        with tempfile.TemporaryDirectory() as directory:
            backups = Backups(community, directory)
            data = {
                "version": 1,
                "guild_id": "99",
                "created_at": utcnow().isoformat(),
                "tables": {
                    "Polls": [
                        {
                            "id": "oldPoll",
                            "fields": {
                                "Poll Key": "123:2026-09-24",
                                "Guild ID": "99",
                                "Status": "open",
                                "Message ID": "123",
                            },
                        }
                    ],
                    "Responses": [
                        {
                            "id": "oldResponse",
                            "fields": {
                                "Response Key": "oldPoll:1",
                                "Poll Key": "oldPoll",
                                "User ID": "1",
                                "Choice": "yes",
                            },
                        }
                    ],
                    "Reports": [
                        {
                            "id": "oldReport",
                            "fields": {"Poll Key": "oldPoll", "Yes Count": 1},
                        }
                    ],
                    "Voice Sessions": [
                        {
                            "id": "oldVoice",
                            "fields": {
                                "Session Key": "v1",
                                "Guild ID": "99",
                                "User ID": "1",
                                "Joined At": (
                                    utcnow() - timedelta(hours=1)
                                ).isoformat(),
                                "Active Key": "99:1",
                                "Status": "active",
                            },
                        }
                    ],
                },
            }
            backup = backups.write(99, data)
            self.assertEqual(4, (await backups.restore(99, backup["id"]))["missing"])
            self.assertFalse(store.rows)
            self.assertEqual(
                4, (await backups.restore(99, backup["id"], preview=False))["restored"]
            )
            poll = (await store.list_records("Polls"))[0]
            answer = (await store.list_records("Responses"))[0]
            self.assertEqual("closed", poll["fields"]["Status"])
            self.assertEqual("", poll["fields"]["Message ID"])
            self.assertEqual(poll["id"], answer["fields"]["Poll Key"])
            self.assertEqual(
                "",
                (await store.list_records("Voice Sessions"))[0]["fields"]["Active Key"],
            )
            self.assertEqual(
                0, (await backups.restore(99, backup["id"], preview=False))["restored"]
            )
            with self.assertRaises(ValueError):
                backups.read(77, backup["id"])
            with self.assertRaises(ValueError):
                backups.path(99, "../../secrets")


class PeriodTests(unittest.TestCase):
    def test_sessions_clipped_to_custom_dates_and_quality_visible(self):
        admin, *_ = fixture()
        start = datetime(2026, 9, 1, tzinfo=BANGKOK)
        end = start + timedelta(days=1)
        rows = [
            {
                "fields": {
                    "Joined At": (start - timedelta(hours=1)).isoformat(),
                    "Left At": (end + timedelta(hours=1)).isoformat(),
                    "Data Quality": "estimated: spans bot downtime",
                }
            }
        ]
        data = admin.voice_data(rows, start=start, finish=end)
        self.assertEqual(86400, data[0]["seconds"])
        self.assertIn("estimated", data[0]["quality"])
