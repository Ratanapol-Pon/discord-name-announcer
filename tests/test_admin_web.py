import copy
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from admin_web import AdminWeb
from event_manager import EventManager, utcnow, validate_event


def event_input():
    return {
        "kind": "poll",
        "title": "Friday night",
        "body": "Pick a game!",
        "channel_id": "123",
        "options": ["Yes", "Maybe", "No"],
        "closes_at": (utcnow() + timedelta(hours=24)).isoformat(),
        "no_reason": True,
    }


class MemoryStore:
    """In-memory test double; never connects to Airtable or Discord."""

    def __init__(self):
        self.rows = {}

    def _formula_equals(self, field, value):
        return f"{{{field}}}='{value}'"

    async def _create(self, table, fields):
        row = {"id": f"rec{len(self.rows) + 1}", "fields": copy.deepcopy(fields)}
        self.rows[row["id"]] = (table, row)
        return copy.deepcopy(row)

    async def _update(self, table, row_id, fields):
        self.rows[row_id][1]["fields"].update(copy.deepcopy(fields))
        return copy.deepcopy(self.rows[row_id][1])

    async def _find_one(self, table, field, value):
        return next(
            (
                copy.deepcopy(r)
                for t, r in self.rows.values()
                if t == table and r["fields"].get(field) == value
            ),
            None,
        )

    async def list_records(self, table, formula=None):
        return [copy.deepcopy(r) for t, r in self.rows.values() if t == table]


def fixture():
    member = SimpleNamespace(
        id=1, display_name="Rz", guild_permissions=SimpleNamespace(administrator=True)
    )
    guild = SimpleNamespace(
        id=99,
        name="Teemo Test Server",
        text_channels=[],
        voice_channels=[],
        stage_channels=[],
        me=object(),
    )
    guild.get_member = lambda user_id: member if user_id == 1 else None
    member.guild = guild
    bot = MagicMock()
    bot.get_guild.side_effect = lambda guild_id: guild if guild_id == 99 else None
    bot.is_ready.return_value = True
    bot.user = SimpleNamespace(id=10)
    store = MemoryStore()
    manager = EventManager(bot, store)
    manager.ready = True
    channel = SimpleNamespace(
        id=123,
        send=AsyncMock(return_value=SimpleNamespace(id=500)),
        fetch_message=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
    )
    manager.channel = AsyncMock(return_value=channel)
    settings = {
        "poll_time": "11:59",
        "report_time": "17:00",
        "poll_channel_ids": ["123"],
        "announcement_channel_id": "123",
        "poll_enabled": True,
        "report_enabled": True,
        "timezone": "Asia/Bangkok",
    }
    admin = AdminWeb(
        bot,
        store,
        manager,
        lambda: settings,
        AsyncMock(),
        AsyncMock(return_value="Done"),
        "http://127.0.0.1",
    )
    return admin, manager, store, member, channel


class WebSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admin, self.manager, self.store, self.member, self.channel = fixture()
        self.client = TestClient(
            TestServer(self.admin.app), cookie_jar=CookieJar(unsafe=True)
        )
        await self.client.start_server()
        self.admin.public_url = str(self.client.make_url("")).rstrip("/")
        self.headers = {"Origin": self.admin.public_url}

    async def asyncTearDown(self):
        await self.client.close()

    async def sign_in(self):
        token = self.admin.issue_link(1, 99).split("login=")[1]
        response = await self.client.post(
            "/api/login", json={"token": token}, headers=self.headers
        )
        self.assertEqual(200, response.status)
        info = await (await self.client.get("/api/session")).json()
        self.headers["X-CSRF-Token"] = info["csrf"]
        return token

    async def test_private_data_requires_login_but_shell_is_public(self):
        self.assertEqual(401, (await self.client.get("/api/dashboard")).status)
        response = await self.client.get("/")
        self.assertEqual(200, response.status)
        self.assertIn(
            "frame-ancestors 'none'", response.headers["Content-Security-Policy"]
        )

    async def test_login_ticket_cannot_be_reused(self):
        token = await self.sign_in()
        response = await self.client.post(
            "/api/login", json={"token": token}, headers=self.headers
        )
        self.assertEqual(401, response.status)

    async def test_expired_ticket_is_rejected(self):
        token = self.admin.issue_link(1, 99).split("login=")[1]
        self.admin.tickets[token]["expires"] = time.monotonic() - 1
        self.assertEqual(
            401,
            (
                await self.client.post(
                    "/api/login", json={"token": token}, headers=self.headers
                )
            ).status,
        )

    async def test_cross_origin_and_missing_csrf_cannot_mutate(self):
        await self.sign_in()
        response = await self.client.post(
            "/api/events",
            json=event_input(),
            headers={**self.headers, "Origin": "https://attacker.example"},
        )
        self.assertEqual(403, response.status)
        response = await self.client.post(
            "/api/events", json=event_input(), headers={"Origin": self.admin.public_url}
        )
        self.assertEqual(403, response.status)
        self.assertEqual({}, self.manager.events)

    async def test_revoked_admin_permission_invalidates_access(self):
        await self.sign_in()
        self.member.guild_permissions.administrator = False
        self.assertEqual(403, (await self.client.get("/api/dashboard")).status)

    async def test_other_guild_event_is_not_exposed(self):
        await self.sign_in()
        event = await self.manager.create(event_input(), 77, 7)
        response = await self.client.get("/api/events/" + event["key"])
        self.assertEqual(400, response.status)

    async def test_draft_edit_publish_and_duplicate_publish_rejection(self):
        await self.sign_in()
        response = await self.client.post(
            "/api/events", json=event_input(), headers=self.headers
        )
        self.assertEqual(201, response.status)
        event = (await response.json())["event"]
        self.channel.send.assert_not_awaited()
        updated = dict(event_input(), title="Updated title")
        response = await self.client.post(
            f"/api/events/{event['key']}/edit", json=updated, headers=self.headers
        )
        self.assertEqual(200, response.status)
        response = await self.client.post(
            f"/api/events/{event['key']}/publish", json={}, headers=self.headers
        )
        self.assertEqual("open", (await response.json())["event"]["status"])
        response = await self.client.post(
            f"/api/events/{event['key']}/publish", json={}, headers=self.headers
        )
        self.assertEqual(400, response.status)
        self.channel.send.assert_awaited_once()

    async def test_logout_ends_session(self):
        await self.sign_in()
        await self.client.post("/api/logout", json={}, headers=self.headers)
        self.assertEqual(401, (await self.client.get("/api/session")).status)


class EventLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_record_does_not_block_other_events(self):
        _, manager, store, _, _ = fixture()
        event = await manager.create(event_input(), 99, 1)
        await store._create("Admin Events", {"Data": "not json"})
        restarted = EventManager(manager.bot, store)
        with self.assertLogs("event_manager", level="ERROR"):
            await restarted.restore()
        self.assertTrue(restarted.ready)
        self.assertIn(event["key"], restarted.events)
        self.assertIsNotNone(restarted.restore_error)

    async def test_expired_scheduled_poll_is_not_sent(self):
        _, manager, _, _, channel = fixture()
        event = await manager.create(event_input(), 99, 1)
        event.update(
            status="scheduled",
            publish_at=(utcnow() - timedelta(hours=2)).isoformat(),
            closes_at=(utcnow() - timedelta(hours=1)).isoformat(),
        )
        await manager.save(event)
        await manager.tick()
        self.assertEqual("cancelled", manager.events[event["key"]]["status"])
        channel.send.assert_not_awaited()

    async def test_failed_action_does_not_change_saved_audit(self):
        _, manager, _, _, _ = fixture()
        event = await manager.create(event_input(), 99, 1)
        with self.assertRaises(ValueError):
            await manager.action(event["key"], "close", 99, 1)
        self.assertEqual([], manager.events[event["key"]]["history"])

    def test_voice_totals_clip_to_reporting_window(self):
        admin, _, _, _, _ = fixture()
        now = utcnow()
        rows = [
            {
                "fields": {
                    "Joined At": (now - timedelta(days=40)).isoformat(),
                    "Left At": (now - timedelta(days=29)).isoformat(),
                    "User ID": "1",
                }
            }
        ]
        result = admin.voice_data(rows)
        self.assertAlmostEqual(86400, result[0]["seconds"], delta=2)

    def test_validation_rejects_duplicate_choices_and_bad_deadlines(self):
        for data in [
            dict(event_input(), options=["Yes", "yes"]),
            dict(event_input(), closes_at=utcnow().isoformat()),
            dict(event_input(), options=["A", "B"]),
        ]:
            with self.assertRaises(ValueError):
                validate_event(data)

    async def test_scheduled_poll_closes_after_restart_and_keeps_votes(self):
        _, manager, store, member, channel = fixture()
        future = (utcnow() + timedelta(hours=1)).isoformat()
        event = await manager.create(dict(event_input(), publish_at=future), 99, 1)
        event = await manager.action(event["key"], "publish", 99, 1)
        self.assertEqual("scheduled", event["status"])
        channel.send.assert_not_awaited()
        manager.events[event["key"]]["publish_at"] = (
            utcnow() - timedelta(seconds=1)
        ).isoformat()
        await manager.tick()
        self.assertEqual("open", manager.events[event["key"]]["status"])
        interaction = SimpleNamespace(
            guild_id=99,
            user=member,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await manager.vote(interaction, event["key"], 0)
        await manager.vote(interaction, event["key"], 1)
        votes = await manager.votes(event)
        self.assertEqual(1, len(votes))
        self.assertEqual("Maybe", votes[0]["answer"])
        restarted = EventManager(manager.bot, store)
        restarted.channel = AsyncMock(return_value=channel)
        await restarted.restore()
        self.assertEqual("open", restarted.events[event["key"]]["status"])
        restarted.events[event["key"]]["closes_at"] = (
            utcnow() - timedelta(seconds=1)
        ).isoformat()
        await restarted.tick()
        self.assertEqual("closed", restarted.events[event["key"]]["status"])
        await restarted.vote(interaction, event["key"], 0)
        self.assertIn("closed", interaction.followup.send.await_args.args[0])
        self.assertEqual("Maybe", (await restarted.votes(event))[0]["answer"])

    async def test_uncertain_delivery_is_not_retried_automatically(self):
        _, manager, _, _, channel = fixture()
        channel.send.side_effect = TimeoutError("Lost connection")
        event = await manager.create(event_input(), 99, 1)
        with self.assertLogs("event_manager", level="ERROR"):
            await manager.action(event["key"], "publish", 99, 1)
        self.assertEqual("review", manager.events[event["key"]]["status"])
        await manager.tick()
        channel.send.assert_awaited_once()

    async def test_no_reason_required_and_mentions_disabled(self):
        _, manager, _, member, channel = fixture()
        event = await manager.create(event_input(), 99, 1)
        await manager.action(event["key"], "publish", 99, 1)
        self.assertFalse(channel.send.await_args.kwargs["allowed_mentions"].everyone)
        interaction = SimpleNamespace(
            guild_id=99,
            user=member,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await manager.vote(interaction, event["key"], 2, "")
        self.assertEqual([], await manager.votes(event))
        await manager.vote(interaction, event["key"], 2, "Work")
        self.assertEqual("Work", (await manager.votes(event))[0]["reason"])
