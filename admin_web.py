"""Same-process admin web app. Discord grants one-use sign-in links to admins."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import secrets
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from aiohttp import web

from event_manager import timestamp, utcnow

LOGGER = logging.getLogger(__name__)
ASSETS = Path(__file__).parent / "web"
COOKIE = "teemo_session"


class AdminWeb:
    def __init__(
        self, bot, store, events, get_settings, save_settings, daily_action, public_url
    ):
        self.bot, self.store, self.events = bot, store, events
        self.get_settings, self.save_settings = get_settings, save_settings
        self.daily_action = daily_action
        self.public_url = public_url.rstrip("/")
        self.tickets, self.sessions, self.cache = {}, {}, {}
        self.cache_lock = asyncio.Lock()
        self.action_lock = asyncio.Lock()
        self.started = time.monotonic()
        self.worker = None
        self.app = web.Application(middlewares=[self.boundary], client_max_size=24000)
        self.app.add_routes(
            [
                web.get("/", self.index),
                web.get("/healthz", self.health),
                web.get("/assets/app.js", self.javascript),
                web.get("/assets/style.css", self.styles),
                web.post("/api/login", self.login),
                web.post("/api/logout", self.logout),
                web.get("/api/session", self.session_info),
                web.get("/api/dashboard", self.dashboard),
                web.get("/api/export", self.export),
                web.post("/api/settings", self.settings),
                web.post("/api/daily/{action}", self.daily),
                web.get("/api/events", self.events_list),
                web.post("/api/events", self.create_event),
                web.get("/api/events/{key}", self.event_detail),
                web.post("/api/events/{key}/{action}", self.event_action),
            ]
        )
        self.app.on_startup.append(self.start_worker)
        self.app.on_cleanup.append(self.stop_worker)

    def prune(self):
        now = time.monotonic()
        for records in (self.tickets, self.sessions):
            for key in [k for k, v in records.items() if v["expires"] <= now]:
                records.pop(key, None)

    def issue_link(self, user_id, guild_id):
        self.prune()
        if not self.public_url:
            raise ValueError("The web address is not configured yet.")
        key = secrets.token_urlsafe(32)
        self.tickets[key] = {
            "user_id": int(user_id),
            "guild_id": int(guild_id),
            "expires": time.monotonic() + 300,
        }
        # Fragment is not sent to proxy/access logs, and is removed by the page.
        return f"{self.public_url}/#login={key}"

    def member(self, session):
        guild = self.bot.get_guild(session["guild_id"])
        member = guild.get_member(session["user_id"]) if guild else None
        if not member or not member.guild_permissions.administrator:
            raise web.HTTPForbidden(reason="Server administrator access is required.")
        return member

    @web.middleware
    async def boundary(self, request, handler):
        try:
            if request.path.startswith("/api/"):
                if not self.bot.is_ready():
                    raise web.HTTPServiceUnavailable(
                        reason="Teemo is reconnecting. Please try again shortly."
                    )
                if request.method != "GET":
                    origin = request.headers.get("Origin", "")
                    if not self.public_url or origin != self.public_url:
                        raise web.HTTPForbidden(
                            reason="Open this form from the Teemo web app."
                        )
                    if request.content_type != "application/json":
                        raise web.HTTPUnsupportedMediaType(reason="Send JSON.")
                if request.path != "/api/login":
                    session = self.sessions.get(request.cookies.get(COOKIE))
                    if not session or session["expires"] <= time.monotonic():
                        raise web.HTTPUnauthorized(
                            reason="Run /teemo_web in Discord for a new sign-in link."
                        )
                    request["member"] = self.member(session)
                    request["session"] = session
                    if request.method != "GET" and not secrets.compare_digest(
                        request.headers.get("X-CSRF-Token", ""), session["csrf"]
                    ):
                        raise web.HTTPForbidden(
                            reason="Refresh this page and try again."
                        )
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({"error": exc.reason}, status=exc.status)
        except (ValueError, TypeError, KeyError) as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        except Exception:
            LOGGER.exception("Web request failed: %s", request.path)
            response = web.json_response(
                {"error": "Teemo could not complete that request. Try again shortly."},
                status=503,
            )
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    async def index(self, request):
        return web.FileResponse(ASSETS / "index.html")

    async def javascript(self, request):
        return web.FileResponse(ASSETS / "app.js")

    async def styles(self, request):
        return web.FileResponse(ASSETS / "style.css")

    async def health(self, request):
        return web.json_response({"web": "online", "discord": self.bot.is_ready()})

    async def login(self, request):
        self.prune()
        data = await request.json()
        ticket = self.tickets.pop(str(data.get("token", "")), None)
        if not ticket or ticket["expires"] <= time.monotonic():
            raise web.HTTPUnauthorized(
                reason="This sign-in link expired or was already used. Run /teemo_web again."
            )
        self.member(ticket)
        key = secrets.token_urlsafe(32)
        self.sessions[key] = dict(
            ticket, expires=time.monotonic() + 8 * 3600, csrf=secrets.token_urlsafe(32)
        )
        response = web.json_response({"ok": True})
        response.set_cookie(
            COOKIE,
            key,
            httponly=True,
            secure=self.public_url.startswith("https://"),
            samesite="Strict",
            max_age=8 * 3600,
            path="/",
        )
        return response

    async def logout(self, request):
        self.sessions.pop(request.cookies.get(COOKIE), None)
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/")
        return response

    async def session_info(self, request):
        member = request["member"]
        guild = member.guild
        channels = [
            {"id": str(c.id), "name": c.name}
            for c in guild.text_channels
            if c.permissions_for(member).view_channel
            and c.permissions_for(guild.me).send_messages
        ]
        return web.json_response(
            {
                "csrf": request["session"]["csrf"],
                "name": member.display_name,
                "guild": guild.name,
                "guild_id": str(guild.id),
                "channels": channels,
                "settings": self.get_settings(),
                "events_ready": self.events.ready,
            }
        )

    async def snapshot(self, guild_id):
        async with self.cache_lock:
            cached = self.cache.get(guild_id)
            if cached and time.monotonic() - cached[0] < 60:
                return cached[1]
            formula = self.store._formula_equals("Guild ID", str(guild_id))
            tables = ("Polls", "Voice Sessions", "Solo Voice Sessions")
            polls, voice, solo = await asyncio.gather(
                *(self.store.list_records(t, formula) for t in tables)
            )
            cutoff = (utcnow() - timedelta(days=30)).date().isoformat()
            polls = [r for r in polls if r["fields"].get("Poll Date", "") >= cutoff]
            ids = [r["id"] for r in polls]
            responses = []
            for offset in range(0, len(ids), 40):
                parts = [
                    self.store._formula_equals("Poll Key", key)
                    for key in ids[offset : offset + 40]
                ]
                responses.extend(
                    await self.store.list_records(
                        "Responses", "OR(" + ",".join(parts) + ")"
                    )
                )
            result = {
                "polls": polls,
                "voice": voice,
                "solo": solo,
                "responses": responses,
                "fetched_at": utcnow().isoformat(),
            }
            self.cache[guild_id] = (time.monotonic(), result)
            return result

    def voice_data(self, rows, solo=False):
        now = utcnow()
        start = now - timedelta(days=30)
        result = []
        for row in rows:
            fields = row["fields"]
            try:
                joined = timestamp(
                    fields.get("Started Alone At" if solo else "Joined At")
                )
                left = fields.get("Ended Alone At" if solo else "Left At")
                end = min(timestamp(left), now) if left else now
            except (TypeError, ValueError):
                continue
            if end < start or joined > now:
                continue
            seconds = max(0, int((end - max(start, joined)).total_seconds()))
            result.append(
                {
                    "user_id": fields.get("User ID"),
                    "name": fields.get("Display Name", "Unknown"),
                    "channel": fields.get("Voice Channel Name", "Unknown"),
                    "joined": joined.isoformat(),
                    "left": left,
                    "seconds": seconds,
                    "active": not bool(left),
                }
            )
        return sorted(result, key=lambda x: x["joined"], reverse=True)

    async def dashboard(self, request):
        guild = request["member"].guild
        data = await self.snapshot(guild.id)
        voice, solo = (
            self.voice_data(data["voice"]),
            self.voice_data(data["solo"], True),
        )
        totals = defaultdict(
            lambda: {"name": "", "seconds": 0, "solo_seconds": 0, "sessions": 0}
        )
        for rows, key in ((voice, "seconds"), (solo, "solo_seconds")):
            for row in rows:
                person = totals[row["user_id"]]
                person["name"] = row["name"]
                person[key] += row["seconds"]
                if key == "seconds":
                    person["sessions"] += 1
        live = []
        for channel in guild.voice_channels + guild.stage_channels:
            people = [m.display_name for m in channel.members if not m.bot]
            if people:
                live.append(
                    {
                        "channel": channel.name,
                        "people": people,
                        "solo": len(people) == 1,
                    }
                )
        polls = sorted(
            [dict(id=r["id"], **r["fields"]) for r in data["polls"]],
            key=lambda x: x.get("Poll Date", ""),
            reverse=True,
        )
        events = [
            e for e in self.events.events.values() if e["guild_id"] == str(guild.id)
        ]
        responses = [dict(id=r["id"], **r["fields"]) for r in data["responses"]]
        return web.json_response(
            {
                "fetched_at": data["fetched_at"],
                "now": utcnow().isoformat(),
                "settings": self.get_settings(),
                "stats": {
                    "voice_seconds": sum(x["seconds"] for x in voice),
                    "solo_seconds": sum(x["seconds"] for x in solo),
                    "members": len(totals),
                    "responses": len(responses),
                },
                "live": live,
                "members": sorted(
                    totals.values(), key=lambda x: x["seconds"], reverse=True
                ),
                "voice": voice[:250],
                "solo": solo[:250],
                "polls": polls,
                "responses": responses,
                "events": sorted(events, key=lambda x: x["created_at"], reverse=True),
                "health": {
                    "discord": self.bot.is_ready(),
                    "event_scheduler": self.events.ready,
                    "error": self.events.last_error,
                    "uptime_seconds": int(time.monotonic() - self.started),
                },
            }
        )

    async def export(self, request):
        data = await self.snapshot(request["member"].guild.id)
        kind = request.query.get("kind", "voice")
        if kind not in {"voice", "solo", "responses"}:
            raise ValueError("Choose voice, solo, or responses.")
        rows = (
            self.voice_data(data[kind], kind == "solo")
            if kind != "responses"
            else [r["fields"] for r in data["responses"]]
        )
        output = io.StringIO()
        if rows:
            writer = csv.DictWriter(
                output, fieldnames=list(dict.fromkeys(k for r in rows for k in r))
            )
            writer.writeheader()
            for row in rows:
                # Spreadsheet formula injection: keep user-written names/reasons literal.
                writer.writerow(
                    {
                        k: "'" + str(v)
                        if str(v).lstrip().startswith(("=", "+", "-", "@"))
                        else v
                        for k, v in row.items()
                    }
                )
        return web.Response(
            text=output.getvalue(),
            content_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="teemo-{kind}-30days.csv"'
            },
        )

    async def settings(self, request):
        async with self.action_lock:
            await self.save_settings(await request.json(), request["member"])
        return web.json_response({"settings": self.get_settings()})

    async def daily(self, request):
        action = request.match_info["action"]
        if action not in {"poll", "report"}:
            raise ValueError("Unknown daily task.")
        async with self.action_lock:
            result = await self.daily_action(action, request["member"])
            self.cache.pop(request["member"].guild.id, None)
        return web.json_response({"message": result})

    def require_events(self):
        if not self.events.ready:
            raise web.HTTPServiceUnavailable(
                reason="The event service is starting. Refresh in a moment."
            )

    async def events_list(self, request):
        self.require_events()
        guild_id = str(request["member"].guild.id)
        return web.json_response(
            {
                "events": [
                    e for e in self.events.events.values() if e["guild_id"] == guild_id
                ]
            }
        )

    async def create_event(self, request):
        self.require_events()
        member = request["member"]
        event = await self.events.create(
            await request.json(), member.guild.id, member.id
        )
        return web.json_response({"event": event}, status=201)

    async def event_detail(self, request):
        self.require_events()
        event = self.events.owned(request.match_info["key"], request["member"].guild.id)
        votes = await self.events.votes(event) if event["kind"] == "poll" else []
        return web.json_response({"event": event, "votes": votes})

    async def event_action(self, request):
        self.require_events()
        member = request["member"]
        if request.match_info["action"] == "edit":
            event = await self.events.edit(
                request.match_info["key"],
                await request.json(),
                member.guild.id,
                member.id,
            )
            return web.json_response({"event": event})
        event = await self.events.action(
            request.match_info["key"],
            request.match_info["action"],
            member.guild.id,
            member.id,
        )
        return web.json_response({"event": event})

    async def work(self):
        while True:
            await asyncio.sleep(15)
            self.prune()
            if self.bot.is_ready():
                try:
                    if not self.events.ready:
                        await self.events.restore()
                    await self.events.tick()
                except Exception:
                    LOGGER.exception("Web event worker failed")
                    self.events.last_error = "Event service could not reach storage. It will retry automatically."

    async def start_worker(self, app):
        self.worker = asyncio.create_task(self.work())

    async def stop_worker(self, app):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
