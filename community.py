"""Persistent preferences, game plans and safe task operations for one bot replica."""

import asyncio
import copy
import json
import logging
import uuid
from datetime import date, datetime, time, timedelta

import discord

from event_manager import timestamp, utcnow, validate_event
from planning import BANGKOK, minutes, suggest, validate_plan

LOGGER = logging.getLogger(__name__)
STATE_TABLE = "Teemo State"


class Community:
    def __init__(self, bot, store, polls, events):
        self.bot, self.store, self.polls, self.events = bot, store, polls, events
        self.rows, self.ids = {}, {}
        self.lock = asyncio.Lock()
        self.operation_lock = asyncio.Lock()
        self.ready = False
        self.error = None
        self.last_tick = None
        self.reminder_reports = {}

    async def restore(self):
        async with self.lock:
            rows, ids = {}, {}
            for row in await self.store.list_records(STATE_TABLE):
                fields = row["fields"]
                item = json.loads(fields["Data"])
                if (
                    item["key"] != fields["Key"]
                    or str(item["guild_id"]) != fields["Guild ID"]
                ):
                    raise ValueError("Invalid community state record.")
                rows[item["key"]], ids[item["key"]] = item, row["id"]
            self.rows, self.ids, self.ready = rows, ids, True
            self.error = None
            LOGGER.info(
                "Community tools ready: %s persisted settings/plans/tasks", len(rows)
            )

    def get(self, key, default=None):
        return copy.deepcopy(self.rows.get(key, default))

    def items(self, guild_id, kind):
        return [
            copy.deepcopy(r)
            for r in self.rows.values()
            if r["guild_id"] == str(guild_id) and r["kind"] == kind
        ]

    async def put(self, key, guild_id, kind, data):
        if not self.ready:
            raise ValueError("Community settings are still loading. Try again shortly.")
        async with self.lock:
            item = dict(
                data,
                key=key,
                guild_id=str(guild_id),
                kind=kind,
                updated_at=utcnow().isoformat(),
            )
            fields = {
                "Key": key,
                "Guild ID": str(guild_id),
                "Kind": kind,
                "Updated At": item["updated_at"],
                "Data": json.dumps(item, ensure_ascii=False),
            }
            record_id = self.ids.get(key)
            if not record_id:
                existing = await self.store._find_one(STATE_TABLE, "Key", key)
                record_id = existing["id"] if existing else None
            if record_id:
                await self.store._update(STATE_TABLE, record_id, fields)
            else:
                record_id = (await self.store._create(STATE_TABLE, fields))["id"]
            self.ids[key], self.rows[key] = record_id, item
            return copy.deepcopy(item)

    def preferences(self, guild_id, user_id):
        return self.get(
            f"prefs:{guild_id}:{user_id}",
            {
                "tracking": True,
                "reminders": False,
                "quiet_start": "23:00",
                "quiet_end": "09:00",
            },
        )

    def tracking(self, guild_id, user_id):
        return self.ready and self.preferences(guild_id, user_id).get("tracking", True)

    def config(self, guild_id):
        return self.get(f"config:{guild_id}", {"public_reasons": True, "backups": True})

    async def save_plan(self, poll, user_id, plan, start):
        data = validate_plan(plan, start)
        if data["reminder"]:
            prefs = self.preferences(poll["guild_id"], user_id)
            await self.put(
                f"prefs:{poll['guild_id']}:{user_id}",
                poll["guild_id"],
                "prefs",
                dict(prefs, reminders=True),
            )
        return await self.put(
            f"plan:{poll['id']}:{user_id}",
            poll["guild_id"],
            "plan",
            dict(
                data,
                poll_id=poll["id"],
                user_id=str(user_id),
                poll_date=poll["poll_date"],
                confirmed=False,
            ),
        )

    async def confirm_plan(self, poll_id, user_id):
        key = f"plan:{poll_id}:{user_id}"
        plan = self.get(key)
        if plan:
            await self.put(key, plan["guild_id"], "plan", dict(plan, confirmed=True))

    def enrich(self, poll, report):
        report = copy.deepcopy(report)
        for r in report.get("responses", []):
            plan = r.get("plan")
            if not plan or not plan.get("confirmed"):
                plan = self.get(f"plan:{poll['id']}:{r['user_id']}")
            r["plan"] = (
                plan
                if plan
                and plan.get("confirmed")
                and plan.get("from") == r.get("play_time")
                else None
            )
        report["suggestion"] = suggest(report.get("responses", []))
        report["public_reasons"] = self.config(poll["guild_id"]).get(
            "public_reasons", True
        )
        return report

    async def begin_task(self, key, guild_id, **data):
        previous = self.get(key)
        if previous and previous.get("status") in {"running", "review", "sent"}:
            raise ValueError(
                "Delivery already ran or needs review. Check task history before resending."
            )
        task = dict(data, status="running", attempted_at=utcnow().isoformat())
        # Reserve in memory before the network write; a timeout must not permit a resend.
        self.rows[key] = dict(task, key=key, guild_id=str(guild_id), kind="task")
        return await self.put(key, guild_id, "task", task)

    async def finish_task(self, task, status, **data):
        return await self.put(
            task["key"], task["guild_id"], "task", dict(task, **data, status=status)
        )

    async def report_action(self, guild_id, user_id, data):
        channel_id = int(data["channel_id"])
        poll_date = date.fromisoformat(data["date"])
        action = data["action"]
        if action not in {"preview", "update", "resend"}:
            raise ValueError("Choose preview, update, or resend.")
        async with self.operation_lock, self.polls._delivery_lock:
            channel = await self.polls._get_channel(channel_id)
            if channel.guild.id != int(guild_id):
                raise ValueError("This channel belongs to another server.")
            poll = await self.store.get_poll(channel_id, poll_date)
            if not poll or poll["status"] != "closed":
                raise ValueError("Choose a closed daily poll with saved results.")
            report = await self.store.get_report(poll["id"])
            if not report:
                raise ValueError("No saved summary exists for this poll.")
            from game_poll import report_embed

            embed = report_embed(
                poll_date,
                self.polls.timezone_name,
                self.enrich(poll, report),
                timestamp(report["generated_at"]).astimezone(BANGKOK).strftime("%H:%M")
                if report.get("generated_at")
                else self.polls.report_time,
            )
            if action == "preview":
                return {"embed": embed.to_dict()}
            if data.get("confirmed") is not True:
                raise ValueError("Confirm before updating or posting a message.")
            request_id = str(data.get("request_id", ""))
            if not request_id.isalnum() or not 16 <= len(request_id) <= 64:
                raise ValueError("Invalid operation ID. Refresh the preview.")
            key = f"manual:{guild_id}:{request_id}"
            existing = self.get(key)
            if existing:
                return {"task": existing}
            if action == "update":
                if not report.get("message_id"):
                    raise ValueError(
                        "The original summary has no message link. Use resend instead."
                    )
                original = await channel.fetch_message(int(report["message_id"]))
                if original.author.id != self.bot.user.id:
                    raise ValueError("The original message was not sent by Teemo.")
            task = await self.begin_task(
                key,
                guild_id,
                action=f"summary-{action}",
                channel_id=str(channel_id),
                date=poll_date.isoformat(),
                expected_at=None,
                by=str(user_id),
            )
            embed.set_footer(text=f"{embed.footer.text} • {key}")
            try:
                if action == "update":
                    message = await original.edit(
                        embed=embed, allowed_mentions=discord.AllowedMentions.none()
                    )
                else:
                    message = await channel.send(
                        embed=embed, allowed_mentions=discord.AllowedMentions.none()
                    )
                task = await self.finish_task(
                    task,
                    "sent",
                    message_id=str(message.id),
                    completed_at=utcnow().isoformat(),
                )
            except Exception:
                await self.finish_task(
                    task,
                    "review",
                    error="Delivery uncertain. Check Discord before creating a new operation.",
                )
                raise
            return {"task": task}

    async def recover_task(self, guild_id, key):
        async with self.operation_lock, self.polls._delivery_lock:
            task = self.get(key)
            if (
                not task
                or task["guild_id"] != str(guild_id)
                or task.get("status") not in {"running", "review"}
            ):
                raise ValueError("Choose a task that needs delivery review.")
            if not task.get("channel_id"):
                raise ValueError(
                    "This task has no channel history to inspect; it will not be resent automatically."
                )
            channel = await self.polls._get_channel(int(task["channel_id"]))
            if str(channel.guild.id) != str(guild_id):
                raise ValueError("Channel server mismatch.")
            async for message in channel.history(
                limit=100, after=timestamp(task["attempted_at"]) - timedelta(seconds=5)
            ):
                if message.author.id == self.bot.user.id and any(
                    key in (e.footer.text or "") for e in message.embeds
                ):
                    if task.get("poll_id") and task["action"] in {"poll", "report"}:
                        method = (
                            self.store.set_poll_message
                            if task["action"] == "poll"
                            else self.store.set_report_message
                        )
                        await method(task["poll_id"], message.id)
                    return await self.finish_task(
                        task,
                        "sent",
                        message_id=str(message.id),
                        completed_at=utcnow().isoformat(),
                        error=None,
                    )
            return await self.finish_task(
                task,
                "review",
                error="No match in the latest 100 messages. Inspect Discord manually; no automatic resend.",
            )

    async def reminder_tick(self, now):
        if now.strftime("%H:%M") < self.polls.report_time:
            return
        fetched = set()
        self.reminder_reports = {
            k: v
            for k, v in self.reminder_reports.items()
            if k[0] == now.date().isoformat()
        }
        for plan in list(self.rows.values()):
            if (
                plan["kind"] != "plan"
                or not plan.get("confirmed")
                or not plan.get("reminder")
                or plan.get("poll_date") != now.date().isoformat()
            ):
                continue
            prefs = self.preferences(plan["guild_id"], plan["user_id"])
            if not prefs.get("reminders", False):
                continue
            # Use the group's suggested time only if this person is in its overlap.
            cache_key = (plan["poll_date"], plan["poll_id"])
            report = self.reminder_reports.get(cache_key)
            if not report and cache_key not in fetched:
                fetched.add(cache_key)
                report = await self.store.get_report(plan["poll_id"])
                if report:
                    self.reminder_reports[cache_key] = report
            if not report:
                continue
            poll = {"id": plan["poll_id"], "guild_id": plan["guild_id"]}
            suggestion = self.enrich(poll, report).get("suggestion")
            if not suggestion or plan["user_id"] not in suggestion["user_ids"]:
                continue
            start = datetime.combine(
                now.date(), time.fromisoformat(suggestion["time"]), BANGKOK
            )
            due = start - timedelta(minutes=15)
            if not due <= now < start:
                continue
            key = f"reminder:{plan['poll_id']}:{plan['user_id']}"
            if self.get(key):
                continue
            quiet_start, quiet_end = (
                minutes(prefs["quiet_start"]),
                minutes(prefs["quiet_end"]),
            )
            current = now.hour * 60 + now.minute
            quiet = (
                (quiet_start <= current < quiet_end)
                if quiet_start < quiet_end
                else (current >= quiet_start or current < quiet_end)
                if quiet_start != quiet_end
                else False
            )
            if quiet:
                await self.put(
                    key,
                    plan["guild_id"],
                    "task",
                    {
                        "action": "reminder",
                        "status": "skipped",
                        "expected_at": due.isoformat(),
                        "error": "Member quiet hours",
                        "date": plan["poll_date"],
                    },
                )
                continue
            task = await self.begin_task(
                key,
                plan["guild_id"],
                action="reminder",
                expected_at=due.isoformat(),
                date=plan["poll_date"],
            )
            try:
                user = self.bot.get_user(
                    int(plan["user_id"])
                ) or await self.bot.fetch_user(int(plan["user_id"]))
                if not self.preferences(plan["guild_id"], plan["user_id"]).get(
                    "reminders", False
                ):
                    await self.finish_task(task, "skipped", error="Member unsubscribed")
                    continue
                await user.send(
                    f"🎮 Suggested game: **{suggestion['game']}** starts at **{suggestion['time']} Bangkok**. This is a suggestion, not a confirmed booking. Use /teemo_preferences to stop reminders.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await self.finish_task(task, "sent", completed_at=utcnow().isoformat())
            except discord.Forbidden:
                await self.finish_task(
                    task, "failed", error="DMs are unavailable; no public fallback."
                )
            except Exception:
                LOGGER.exception("Reminder delivery uncertain: %s", key)
                await self.finish_task(
                    task,
                    "review",
                    error="Reminder delivery uncertain; no automatic resend.",
                )

    async def tick(self):
        if not self.ready:
            await self.restore()
        now = utcnow().astimezone(BANGKOK)
        if self.last_tick and now - self.last_tick < timedelta(minutes=1):
            return
        self.last_tick = now
        await self.reminder_tick(now)
        if self.events.ready:
            await self.template_tick(now)

    async def create_template(self, guild_id, user_id, data):
        event = self.events.owned(data["event_key"], guild_id)
        first = timestamp(data["first_at"])
        interval = int(data.get("interval_days", 7))
        close_hours, event_hours = (
            int(data.get("close_hours", 12)),
            int(data.get("event_hours", 24)),
        )
        name = str(data.get("name", "")).strip()
        if (
            not 1 <= len(name) <= 100
            or interval not in {1, 7, 14}
            or not 1 <= close_hours <= 168
            or not 0 <= event_hours <= 720
            or first <= utcnow()
        ):
            raise ValueError(
                "Choose a name, future first run, valid repeat interval and valid hour offsets."
            )
        template = {
            "name": name,
            "source": {
                k: event[k]
                for k in ("kind", "title", "body", "options", "channel_id", "no_reason")
            },
            "interval_days": interval,
            "close_hours": close_hours,
            "event_hours": event_hours,
            "next_at": first.isoformat(),
            "enabled": False,
            "created_by": str(user_id),
        }
        await self.events.channel(event)
        return await self.put(
            "template:" + uuid.uuid4().hex, guild_id, "template", template
        )

    def owned_template(self, guild_id, key):
        template = self.get(key)
        if (
            not template
            or template["kind"] != "template"
            or template["guild_id"] != str(guild_id)
        ):
            raise ValueError("Template not found in this server.")
        return template

    async def template_action(self, guild_id, key, action, data, user_id):
        async with self.operation_lock:
            template = self.owned_template(guild_id, key)
            if action == "toggle":
                if type(data.get("enabled")) is not bool:
                    raise ValueError("Choose enabled or paused.")
                if data["enabled"]:
                    while timestamp(template["next_at"]) <= utcnow():
                        template["next_at"] = (
                            timestamp(template["next_at"])
                            + timedelta(days=template["interval_days"])
                        ).isoformat()
                return {
                    "template": await self.put(
                        key,
                        guild_id,
                        "template",
                        dict(template, enabled=data["enabled"], error=None),
                    )
                }
            if action == "draft":
                return {
                    "event": await self.events.create(
                        self.template_payload(template, utcnow()), guild_id, user_id
                    )
                }
            raise ValueError("Unknown template action.")

    @staticmethod
    def template_payload(template, when):
        return dict(
            template["source"],
            publish_at=None,
            closes_at=(when + timedelta(hours=template["close_hours"])).isoformat()
            if template["source"]["kind"] == "poll"
            else None,
            event_at=(when + timedelta(hours=template["event_hours"])).isoformat()
            if template["event_hours"]
            else None,
        )

    async def template_tick(self, now):
        async with self.operation_lock, self.events.lock:
            for original in list(self.rows.values()):
                if original["kind"] != "template" or not original.get("enabled"):
                    continue
                template = copy.deepcopy(original)
                due = timestamp(template["next_at"])
                if due > now:
                    continue
                key = (
                    "repeat-"
                    + template["key"].split(":")[1]
                    + "-"
                    + due.strftime("%Y%m%d%H%M")
                )
                if now - due <= timedelta(minutes=15):
                    event = self.events.events.get(key)
                    if not event:
                        # Recover a create whose response was lost before another create.
                        existing = await self.store._find_one(
                            "Admin Events", "Key", key
                        )
                        if existing:
                            event = json.loads(existing["fields"]["Data"])
                            event["record_id"] = existing["id"]
                        else:
                            event = validate_event(
                                self.template_payload(template, due), now
                            )
                            event.update(
                                key=key,
                                guild_id=template["guild_id"],
                                created_by=template["created_by"],
                                created_at=now.isoformat(),
                                status="draft",
                                history=[
                                    {
                                        "action": "recurring-template",
                                        "at": now.isoformat(),
                                        "template": template["key"],
                                    }
                                ],
                            )
                            await self.events.channel(event)
                            await self.events.save(event)
                    if event["status"] == "draft":
                        event = await self.events.publish(event)
                    if event["status"] in {"review", "publishing"}:
                        await self.put(
                            template["key"],
                            template["guild_id"],
                            "template",
                            dict(
                                template,
                                enabled=False,
                                error="Delivery needs review. Check Polls & posts before enabling again.",
                            ),
                        )
                        continue
                    template["last_event"] = key
                else:
                    template["last_skipped_at"] = due.isoformat()
                while due <= now:
                    due += timedelta(days=template["interval_days"])
                template["next_at"] = due.isoformat()
                await self.put(
                    template["key"], template["guild_id"], "template", template
                )
