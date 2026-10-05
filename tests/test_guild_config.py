import os
import tempfile
import unittest
import unittest.mock
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

_clip_dir = tempfile.TemporaryDirectory()
os.environ["DISCORD_TOKEN"] = "test-token"
os.environ["CLIP_DIR"] = _clip_dir.name

import bot
from game_poll import GamePollService
from guild_config import GuildSettings, GuildSettingsRegistry
from test_admin_web import MemoryStore

BANGKOK = ZoneInfo("Asia/Bangkok")


class GuildSettingsTests(unittest.TestCase):
    def test_new_server_defaults_are_safe(self):
        settings = GuildSettings(42)
        self.assertFalse(settings.setup_complete)
        self.assertEqual([], settings.poll_channel_ids)
        self.assertEqual("11:59", settings.poll_time)
        self.assertEqual("17:00", settings.report_time)

    def test_setup_complete_requires_a_poll_channel(self):
        settings = GuildSettings(42, poll_channel_ids=[123])
        self.assertTrue(settings.setup_complete)

    def test_registry_distinguishes_servers(self):
        registry = GuildSettingsRegistry()
        self.assertIsNone(registry.for_guild(1))
        registry.put(GuildSettings(1, poll_channel_ids=[10]))
        registry.put(GuildSettings(2))  # joined but never set up
        self.assertEqual([10], registry.for_guild(1).poll_channel_ids)
        self.assertEqual([1], [s.guild_id for s in registry.configured()])
        self.assertEqual({1, 2}, set(registry.known_guild_ids()))


class BotSettingsStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_per_guild_records_and_legacy_global_coexist(self):
        store = MemoryStore()
        from airtable_store import AirtablePollStore

        real = AirtablePollStore.__new__(AirtablePollStore)
        import asyncio

        real._settings_lock = asyncio.Lock()
        real._find_one = store._find_one
        real._create = store._create
        real._update = store._update
        real.list_records = store.list_records

        await real.save_bot_settings(
            poll_time="12:00",
            report_time="18:00",
            poll_channel_ids=[555],
            announcement_channel_id=None,
            updated_by="test",
            setting_key="99",
        )
        await real.save_bot_settings(
            poll_time="11:59",
            report_time="17:00",
            poll_channel_ids=[123],
            announcement_channel_id=124,
            updated_by="legacy",
            setting_key="global",
        )

        records = await real.list_bot_settings()
        self.assertEqual({"99", "global"}, set(records))
        self.assertEqual([555], records["99"]["poll_channel_ids"])
        self.assertEqual([123], records["global"]["poll_channel_ids"])
        per_guild = await real.get_bot_settings("99")
        self.assertEqual("12:00", per_guild["poll_time"])
        self.assertIsNone(await real.get_bot_settings("12345"))


class MultiGuildPollServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import discord

        self.channels = {}
        for guild_id, channel_id in ((99, 456), (88, 789)):
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = channel_id
            channel.guild = SimpleNamespace(id=guild_id)
            channel.send = AsyncMock(return_value=SimpleNamespace(id=channel_id + 1))
            channel.fetch_message = AsyncMock()
            self.channels[channel_id] = channel
        self.bot = MagicMock()
        self.bot.get_channel.side_effect = lambda cid: self.channels.get(cid)
        self.store = MemoryStore()
        self.service = GamePollService(self.bot, self.store, [], "Asia/Bangkok")
        self.registry = GuildSettingsRegistry()
        self.service.settings_provider = self.registry

    async def _create_poll(self, guild_id, channel_id):
        from airtable_store import AirtablePollStore

        return await AirtablePollStore.create_poll(
            self.service, guild_id, channel_id, date(2026, 9, 1)
        )

    async def test_posts_only_in_each_servers_own_channels(self):
        self.registry.put(GuildSettings(99, poll_channel_ids=[456]))
        self.registry.put(GuildSettings(88, poll_channel_ids=[789]))
        self.service.store.create_poll = AsyncMock(
            side_effect=lambda guild_id, channel_id, poll_date: {
                "id": f"{guild_id}:{channel_id}",
                "message_id": None,
                "status": "open",
            }
        )
        self.service.store.set_poll_message = AsyncMock()

        created = await self.service.post_daily_polls(date(2026, 9, 1))

        self.assertEqual(2, created)
        self.channels[456].send.assert_awaited_once()
        self.channels[789].send.assert_awaited_once()

    async def test_guild_filter_and_disabled_server(self):
        self.registry.put(
            GuildSettings(99, poll_channel_ids=[456], poll_enabled=False)
        )
        self.registry.put(GuildSettings(88, poll_channel_ids=[789]))
        self.service.store.create_poll = AsyncMock(
            side_effect=lambda guild_id, channel_id, poll_date: {
                "id": f"{guild_id}:{channel_id}",
                "message_id": None,
                "status": "open",
            }
        )
        self.service.store.set_poll_message = AsyncMock()

        created = await self.service.post_daily_polls(date(2026, 9, 1))

        self.assertEqual(1, created)
        self.channels[456].send.assert_not_awaited()
        self.channels[789].send.assert_awaited_once()

        created = await self.service.post_daily_polls(
            date(2026, 9, 1), guild_id=99
        )
        self.assertEqual(0, created)

    async def test_unconfigured_server_gets_no_poll(self):
        self.registry.put(GuildSettings(77))  # no channels: setup incomplete
        created = await self.service.post_daily_polls(date(2026, 9, 1))
        self.assertEqual(0, created)

    async def test_voice_join_checks_only_that_servers_poll_channels(self):
        self.registry.put(GuildSettings(99, poll_channel_ids=[456]))
        self.registry.put(GuildSettings(88, poll_channel_ids=[789]))
        self.service.store.get_poll = AsyncMock(return_value=None)
        member = SimpleNamespace(id=1, guild=SimpleNamespace(id=88))
        voice_channel = SimpleNamespace(id=555, name="Game Room")

        await self.service.track_voice_join(member, voice_channel, date(2026, 9, 2))

        checked = [c.args[0] for c in self.service.store.get_poll.await_args_list]
        self.assertEqual([789], checked)


class DailySchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_service = bot.game_poll_service
        self.old_registry = bot.settings_registry
        bot.game_poll_service = MagicMock()
        bot.game_poll_service.post_daily_polls = AsyncMock(return_value=0)
        bot.game_poll_service.generate_daily_reports = AsyncMock(return_value=0)
        bot.settings_registry = GuildSettingsRegistry()

    def tearDown(self):
        bot.game_poll_service = self.old_service
        bot.settings_registry = self.old_registry

    async def test_window_rules_per_server(self):
        bot.settings_registry.put(
            GuildSettings(99, poll_time="11:59", report_time="17:00",
                          poll_channel_ids=[456])
        )
        bot.settings_registry.put(
            GuildSettings(88, poll_time="20:00", report_time="23:00",
                          poll_channel_ids=[789], report_enabled=False)
        )
        noon = datetime(2026, 9, 1, 12, 0, tzinfo=BANGKOK)
        await bot.run_due_daily_tasks(noon)

        bot.game_poll_service.post_daily_polls.assert_awaited_once_with(
            date(2026, 9, 1), guild_id=99
        )
        bot.game_poll_service.generate_daily_reports.assert_not_awaited()

    async def test_after_report_time_posts_report_not_poll(self):
        bot.settings_registry.put(
            GuildSettings(99, poll_time="11:59", report_time="17:00",
                          poll_channel_ids=[456])
        )
        evening = datetime(2026, 9, 1, 18, 0, tzinfo=BANGKOK)
        await bot.run_due_daily_tasks(evening)

        bot.game_poll_service.post_daily_polls.assert_not_awaited()
        bot.game_poll_service.generate_daily_reports.assert_awaited_once_with(
            date(2026, 9, 1), guild_id=99
        )

    async def test_one_servers_failure_does_not_stop_others(self):
        bot.settings_registry.put(
            GuildSettings(99, poll_channel_ids=[456])
        )
        bot.settings_registry.put(
            GuildSettings(88, poll_channel_ids=[789])
        )
        bot.game_poll_service.post_daily_polls = AsyncMock(
            side_effect=[RuntimeError("airtable down"), 1]
        )
        with self.assertLogs("bot", level="ERROR"):
            await bot.run_due_daily_tasks(datetime(2026, 9, 1, 12, 0, tzinfo=BANGKOK))
        self.assertEqual(2, bot.game_poll_service.post_daily_polls.await_count)


class LegacyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_global_record_is_copied_not_deleted(self):
        old_service = bot.game_poll_service
        old_registry = bot.settings_registry
        old_bot_channel = bot.bot.get_channel
        try:
            store = MemoryStore()
            await store._create(
                "Bot Settings",
                {
                    "Setting Key": "global",
                    "Poll Time": "12:30",
                    "Report Time": "18:30",
                    "Poll Channel IDs": "456",
                    "Announcement Channel ID": "",
                },
            )
            service = MagicMock()
            service.store = store
            bot.game_poll_service = service
            bot.settings_registry = GuildSettingsRegistry()
            channel = SimpleNamespace(id=456, guild=SimpleNamespace(id=99))
            bot.bot.get_channel = lambda cid: channel if cid == 456 else None

            import asyncio
            from airtable_store import AirtablePollStore

            real = AirtablePollStore.__new__(AirtablePollStore)
            real._settings_lock = asyncio.Lock()
            real._find_one = store._find_one
            real._create = store._create
            real._update = store._update
            real.list_records = store.list_records

            async def save_bot_settings(**kwargs):
                await AirtablePollStore.save_bot_settings(real, **kwargs)

            service.store = SimpleNamespace(
                list_bot_settings=lambda: AirtablePollStore.list_bot_settings(real),
                save_bot_settings=save_bot_settings,
            )

            await bot._load_guild_settings()

            migrated = bot.settings_registry.for_guild(99)
            self.assertIsNotNone(migrated)
            self.assertEqual("12:30", migrated.poll_time)
            self.assertEqual("18:30", migrated.report_time)
            self.assertEqual([456], migrated.poll_channel_ids)
            keys = {
                r["fields"]["Setting Key"]
                for t, r in store.rows.values()
                if t == "Bot Settings"
            }
            self.assertEqual({"global", "99"}, keys)

            # Rerunning migration does not duplicate or overwrite.
            await store._create(
                "Bot Settings",
                {"Setting Key": "99", "Poll Channel IDs": "999"},
            )
            bot.settings_registry = GuildSettingsRegistry()
            await bot._load_guild_settings()
            self.assertEqual(
                [999], bot.settings_registry.for_guild(99).poll_channel_ids
            )
        finally:
            bot.game_poll_service = old_service
            bot.settings_registry = old_registry
            bot.bot.get_channel = old_bot_channel


class FakeCommunity:
    """Minimal in-memory stand-in for Community onboarding state."""

    def __init__(self):
        self.ready = True
        self.rows = {}

    def get(self, key, default=None):
        return self.rows.get(key, default)

    async def put(self, key, guild_id, kind, data):
        item = dict(data, key=key, guild_id=str(guild_id), kind=kind)
        self.rows[key] = item
        return item


def _fake_guild(guild_id=99, with_channel=True):
    channel = SimpleNamespace(
        id=456,
        send=AsyncMock(),
        permissions_for=lambda _me: SimpleNamespace(send_messages=True),
    )
    guild = SimpleNamespace(
        id=guild_id,
        name="Test Server",
        owner_id=7,
        me=SimpleNamespace(),
        system_channel=channel if with_channel else None,
        text_channels=[channel] if with_channel else [],
    )
    return guild, channel


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_registry = bot.settings_registry
        self.old_web_admin = bot.web_admin
        self.old_service = bot.game_poll_service
        bot.settings_registry = GuildSettingsRegistry()
        self.community = FakeCommunity()
        bot.web_admin = SimpleNamespace(community=self.community)
        bot.game_poll_service = MagicMock()

    def tearDown(self):
        bot.settings_registry = self.old_registry
        bot.web_admin = self.old_web_admin
        bot.game_poll_service = self.old_service

    async def test_new_server_gets_welcome_with_setup_button(self):
        guild, channel = _fake_guild()
        await bot.on_guild_join(guild)

        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertIsInstance(kwargs["view"], bot.WelcomeView)
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        record = self.community.get("onboard:99")
        self.assertIsNotNone(record)
        self.assertFalse(record["reminded"])

    async def test_configured_server_rejoin_stays_silent(self):
        bot.settings_registry.put(GuildSettings(99, poll_channel_ids=[456]))
        guild, channel = _fake_guild()
        await bot.on_guild_join(guild)
        channel.send.assert_not_awaited()

    async def test_setup_save_persists_per_guild_settings(self):
        guild, channel = _fake_guild()
        poll_channel = SimpleNamespace(id=555)
        view = bot.SetupChannelsView(99, 7)
        view.poll_channel = poll_channel
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=7, guild_permissions=SimpleNamespace(administrator=True)
            ),
            response=SimpleNamespace(
                defer=AsyncMock(),
                send_message=AsyncMock(),
                is_done=lambda: True,
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with unittest.mock.patch.object(
            bot, "_save_guild_settings", new=AsyncMock()
        ) as save:
            await view.save.callback(interaction)

        settings = save.await_args.args[0]
        self.assertEqual(99, settings.guild_id)
        self.assertEqual([555], settings.poll_channel_ids)
        self.assertTrue(settings.setup_complete)

    async def test_setup_requires_poll_channel_first(self):
        view = bot.SetupChannelsView(99, 7)
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await view.save.callback(interaction)
        text = interaction.response.send_message.await_args.args[0]
        self.assertIn("daily game poll", text)

    async def test_reminder_goes_once_after_24_hours(self):
        from unittest.mock import PropertyMock, patch

        guild, channel = _fake_guild()
        joined = datetime(2026, 9, 1, 12, 0, tzinfo=BANGKOK)
        await self.community.put(
            "onboard:99", 99, "onboard",
            {"joined_at": joined.isoformat(), "reminded": False},
        )
        with patch.object(
            type(bot.bot), "guilds", new_callable=PropertyMock
        ) as guilds:
            guilds.return_value = [guild]
            # 12 hours later: too early.
            await bot._onboarding_tick(joined + timedelta(hours=12))
            channel.send.assert_not_awaited()
            # 25 hours later: one reminder, then never again.
            await bot._onboarding_tick(joined + timedelta(hours=25))
            channel.send.assert_awaited_once()
            self.assertTrue(self.community.get("onboard:99")["reminded"])
            channel.send.reset_mock()
            await bot._onboarding_tick(joined + timedelta(hours=49))
            channel.send.assert_not_awaited()

    async def test_configured_server_gets_no_reminder(self):
        from unittest.mock import PropertyMock, patch

        bot.settings_registry.put(GuildSettings(99, poll_channel_ids=[456]))
        guild, channel = _fake_guild()
        await self.community.put(
            "onboard:99", 99, "onboard",
            {"joined_at": "2026-09-01T12:00:00+07:00", "reminded": False},
        )
        with patch.object(
            type(bot.bot), "guilds", new_callable=PropertyMock
        ) as guilds:
            guilds.return_value = [guild]
            await bot._onboarding_tick(
                datetime(2026, 9, 5, 12, 0, tzinfo=BANGKOK)
            )
        channel.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
