import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from game_poll import (
    GamePollService,
    GamePollView,
    NoReasonModal,
    YesTimeView,
    poll_embed,
    report_embed,
)


class GamePollUiTests(unittest.TestCase):
    def test_poll_view_is_persistent_and_no_button_exists(self):
        view = GamePollView(service=object())
        buttons = {item.custom_id: item for item in view.children}

        self.assertIsNone(view.timeout)
        self.assertEqual(
            {"game_poll:yes", "game_poll:maybe", "game_poll:no"}, set(buttons)
        )
        self.assertEqual("No", buttons["game_poll:no"].label)

    def test_closed_view_disables_every_button(self):
        view = GamePollView(service=object(), disabled=True)
        self.assertTrue(all(item.disabled for item in view.children))

    def test_no_reason_modal_requires_a_reason(self):
        modal = NoReasonModal(service=object(), message_id=123)
        self.assertTrue(modal.reason.required)
        self.assertEqual(500, modal.reason.max_length)

    def test_yes_time_view_has_evening_time_picker(self):
        view = YesTimeView(service=object(), message_id=123)
        select = view.children[0]

        self.assertEqual("game_poll:yes_time", select.custom_id)
        self.assertEqual("Flexible", select.options[0].value)
        self.assertEqual("18:00", select.options[1].value)
        self.assertEqual("23:30", select.options[-1].value)

    def test_poll_embed_shows_schedule_and_reason_behavior(self):
        embed = poll_embed(date(2026, 9, 1), "Asia/Bangkok")
        self.assertIn("play any game tonight", embed.description)
        self.assertIn("required reason", embed.fields[0].value)
        self.assertIn("17:00 (Asia/Bangkok)", embed.footer.text)

    def test_report_embed_contains_counts_names_and_no_reasons(self):
        report = {
            "yes_count": 1,
            "maybe_count": 1,
            "no_count": 1,
            "responses": [
                {
                    "user_id": 1,
                    "display_name": "Rz",
                    "choice": "yes",
                    "reason": None,
                    "play_time": "20:00",
                    "joined_voice_chat": True,
                },
                {
                    "user_id": 2,
                    "display_name": "Teemo",
                    "choice": "maybe",
                    "reason": None,
                },
                {
                    "user_id": 3,
                    "display_name": "Ahri",
                    "choice": "no",
                    "reason": "Working late",
                },
            ],
            "no_reasons": [
                {"user_id": 3, "display_name": "Ahri", "reason": "Working late"}
            ],
        }

        embed = report_embed(date(2026, 9, 1), "Asia/Bangkok", report)
        fields = {field.name: field.value for field in embed.fields}

        self.assertEqual("Rz — 20:00", fields["✅ Yes (1)"])
        self.assertEqual("Teemo", fields["🤔 Maybe (1)"])
        self.assertEqual("Ahri", fields["❌ No (1)"])
        self.assertIn("Working late", fields["Reasons from No votes"])
        self.assertNotIn("🎧 Yes-voter voice attendance", fields)
        self.assertIn("Saved to Airtable", embed.footer.text)


class GamePollServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.guild = SimpleNamespace(id=99)
        self.channel.id = 456
        self.channel.send = AsyncMock()
        self.channel.fetch_message = AsyncMock()

        self.bot = MagicMock()
        self.bot.get_channel.return_value = self.channel
        self.store = MagicMock()
        self.service = GamePollService(self.bot, self.store, [456], "Asia/Bangkok")

    async def test_post_poll_creates_one_message_and_links_it_to_airtable(self):
        self.store.create_poll = AsyncMock(
            return_value={"id": 7, "message_id": None, "status": "open"}
        )
        self.store.set_poll_message = AsyncMock()
        self.channel.send.return_value = SimpleNamespace(id=800)

        created = await self.service.post_poll(456, date(2026, 9, 1))

        self.assertTrue(created)
        self.store.create_poll.assert_awaited_once_with(99, 456, date(2026, 9, 1))
        self.store.set_poll_message.assert_awaited_once_with(7, 800)
        sent_view = self.channel.send.await_args.kwargs["view"]
        self.assertIsInstance(sent_view, GamePollView)

    async def test_existing_poll_is_not_posted_twice(self):
        self.store.create_poll = AsyncMock(
            return_value={"id": 7, "message_id": 800, "status": "open"}
        )

        created = await self.service.post_poll(456, date(2026, 9, 1))

        self.assertFalse(created)
        self.channel.send.assert_not_awaited()

    async def test_restore_open_poll_view_binds_message_id(self):
        self.store.get_poll = AsyncMock(
            return_value={"id": 7, "message_id": 800, "status": "open"}
        )

        restored = await self.service.restore_open_poll_views(date(2026, 9, 2))

        self.assertEqual(1, restored)
        self.bot.add_view.assert_called_once()
        view = self.bot.add_view.call_args.args[0]
        self.assertIsInstance(view, GamePollView)
        self.assertEqual(800, self.bot.add_view.call_args.kwargs["message_id"])

    async def test_restore_skips_closed_poll(self):
        self.store.get_poll = AsyncMock(
            return_value={"id": 7, "message_id": 800, "status": "closed"}
        )

        restored = await self.service.restore_open_poll_views(date(2026, 9, 2))

        self.assertEqual(0, restored)
        self.bot.add_view.assert_not_called()

    async def test_voice_join_marks_yes_voter_for_same_guild_poll(self):
        self.store.get_poll = AsyncMock(
            return_value={"id": "recPoll", "guild_id": 99, "status": "open"}
        )
        self.store.mark_yes_voice_join = AsyncMock(return_value=True)
        member = SimpleNamespace(id=1, guild=SimpleNamespace(id=99))
        voice_channel = SimpleNamespace(id=555, name="Game Room")

        changed = await self.service.track_voice_join(
            member, voice_channel, date(2026, 9, 2)
        )

        self.assertTrue(changed)
        self.store.mark_yes_voice_join.assert_awaited_once_with(
            "recPoll", 1, 555, "Game Room"
        )

    async def test_voice_channel_move_closes_old_session_and_starts_new_one(self):
        self.store.stop_voice_session = AsyncMock(return_value=600)
        self.store.start_voice_session = AsyncMock(return_value="recVoice")
        member = SimpleNamespace(id=1, display_name="Rz", guild=SimpleNamespace(id=99))
        old_channel = SimpleNamespace(id=555, name="Lobby")
        new_channel = SimpleNamespace(id=777, name="Game Room")

        await self.service.track_voice_session(
            member, old_channel, new_channel, date(2026, 9, 3)
        )

        self.store.stop_voice_session.assert_awaited_once_with(99, 1, left_at=None)
        self.store.start_voice_session.assert_awaited_once_with(
            99,
            1,
            "Rz",
            777,
            "Game Room",
            date(2026, 9, 3),
            joined_at=None,
        )

    async def test_solo_channel_state_uses_only_human_members(self):
        self.store.sync_solo_voice_channel = AsyncMock(return_value="started")
        solo_member = SimpleNamespace(id=1, display_name="Rz", bot=False)
        bot_member = SimpleNamespace(id=2, display_name="Teemo", bot=True)
        channel = SimpleNamespace(
            id=555,
            name="Game Room",
            guild=SimpleNamespace(id=99),
            members=[solo_member, bot_member],
        )

        await self.service.track_solo_voice_channels(
            (channel,), date(2026, 9, 3)
        )

        self.store.sync_solo_voice_channel.assert_awaited_once_with(
            99,
            555,
            "Game Room",
            {"user_id": 1, "display_name": "Rz"},
            date(2026, 9, 3),
            changed_at=None,
        )

    async def test_non_solo_channel_closes_active_solo_period(self):
        self.store.sync_solo_voice_channel = AsyncMock(return_value="closed")
        channel = SimpleNamespace(
            id=555,
            name="Game Room",
            guild=SimpleNamespace(id=99),
            members=[
                SimpleNamespace(id=1, display_name="Rz", bot=False),
                SimpleNamespace(id=2, display_name="Ahri", bot=False),
            ],
        )

        await self.service.track_solo_voice_channels(
            (channel,), date(2026, 9, 3)
        )

        self.store.sync_solo_voice_channel.assert_awaited_once_with(
            99,
            555,
            "Game Room",
            None,
            date(2026, 9, 3),
            changed_at=None,
        )

    async def test_report_closes_before_snapshot_and_disables_poll(self):
        order = []
        poll = {"id": 7, "message_id": 800, "status": "open"}
        responses = [
            {"user_id": 1, "display_name": "Rz", "choice": "yes", "reason": None}
        ]
        report = {
            "poll_id": 7,
            "message_id": None,
            "yes_count": 1,
            "maybe_count": 0,
            "no_count": 0,
            "no_reasons": [],
            "responses": responses,
        }

        self.store.get_poll = AsyncMock(return_value=poll)
        self.store.get_report = AsyncMock(return_value=None)
        self.store.close_poll = AsyncMock(
            side_effect=lambda _poll_id: order.append("close")
        )
        self.store.get_responses = AsyncMock(
            side_effect=lambda _poll_id: order.append("read") or responses
        )
        self.store.save_report = AsyncMock(
            side_effect=lambda _poll_id, _responses: order.append("save") or report
        )
        self.store.set_report_message = AsyncMock()
        poll_message = SimpleNamespace(edit=AsyncMock())
        self.channel.fetch_message.return_value = poll_message
        self.channel.send.return_value = SimpleNamespace(id=801)

        generated = await self.service.generate_report(456, date(2026, 9, 1))

        self.assertTrue(generated)
        self.assertEqual(["close", "read", "save"], order)
        edited_view = poll_message.edit.await_args.kwargs["view"]
        self.assertTrue(all(item.disabled for item in edited_view.children))
        self.store.set_report_message.assert_awaited_once_with(7, 801)


if __name__ == "__main__":
    unittest.main()
