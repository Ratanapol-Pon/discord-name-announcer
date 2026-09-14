"""December 25 poll recap; public totals only, with persistent delivery history."""

import json
import logging
from collections import Counter
from datetime import datetime

import discord

from event_manager import EVENTS, timestamp

LOGGER = logging.getLogger(__name__)


class YearlySummary:
    def __init__(self, events, timezone, clock, get_channel_id, enabled=True):
        self.events = events
        self.store = events.store
        self.timezone = timezone
        self.clock = clock
        self.get_channel_id = get_channel_id
        self.enabled = enabled
        self.last_error = None

    def is_due(self, now):
        if now.tzinfo is None:
            raise ValueError("The summary clock requires a timezone-aware datetime.")
        local = now.astimezone(self.timezone)
        due = datetime.combine(
            local.date().replace(month=12, day=25), self.clock, self.timezone
        )
        # Retry a missed December 25 delivery through December 31 only.
        return self.enabled and local >= due

    async def totals(self, guild_id, year):
        start, end = f"{year}-01-01", f"{year}-12-25"
        scope = self.store._formula_equals("Guild ID", str(guild_id))
        formula = (
            f"AND({scope},NOT(IS_BEFORE({{Poll Date}},DATETIME_PARSE('{start}'))),"
            f"IS_BEFORE({{Poll Date}},DATETIME_PARSE('{end}')))"
        )
        rows = await self.store.list_records("Polls", formula)
        # Enforce the scope locally as well; never mix another server's data.
        polls = [
            r
            for r in rows
            if str(r["fields"].get("Guild ID")) == str(guild_id)
            and start <= r["fields"].get("Poll Date", "") < end
            and r["fields"].get("Message ID")
        ]
        poll_ids = {r["id"] for r in polls}
        responses = {}
        ids = sorted(poll_ids)
        for offset in range(0, len(ids), 40):
            parts = [
                self.store._formula_equals("Poll Key", key)
                for key in ids[offset : offset + 40]
            ]
            for row in await self.store.list_records(
                "Responses", "OR(" + ",".join(parts) + ")"
            ):
                fields = row["fields"]
                if fields.get("Poll Key") in poll_ids and fields.get("User ID"):
                    responses[(fields["Poll Key"], fields["User ID"])] = fields
        choices = Counter(str(r.get("Choice", "")).lower() for r in responses.values())
        return {
            "year": year,
            "period_start": start,
            "period_end": f"{year}-12-24",
            "polls": len(polls),
            "responses": sum(choices[c] for c in ("yes", "maybe", "no")),
            "participants": len(
                {
                    r["User ID"]
                    for r in responses.values()
                    if str(r.get("Choice", "")).lower() in {"yes", "maybe", "no"}
                }
            ),
            "yes": choices["yes"],
            "maybe": choices["maybe"],
            "no": choices["no"],
        }

    async def voice_totals(self, guild_id, year):
        start = datetime(year, 1, 1, tzinfo=self.timezone)
        end = datetime(year, 12, 25, tzinfo=self.timezone)
        rows = await self.store.list_records(
            "Voice Sessions", self.store._formula_equals("Guild ID", str(guild_id))
        )
        members = {}
        seen = set()
        for row in rows:
            fields = row["fields"]
            user_id = str(fields.get("User ID", ""))
            if (
                str(fields.get("Guild ID")) != str(guild_id)
                or not user_id
                or row["id"] in seen
            ):
                continue
            seen.add(row["id"])
            try:
                joined = timestamp(fields["Joined At"])
                left = timestamp(fields["Left At"]) if fields.get("Left At") else end
            except (ValueError, TypeError, KeyError):
                LOGGER.warning("Ignoring invalid yearly voice session: %s", row["id"])
                continue
            seconds = max(0, int((min(left, end) - max(joined, start)).total_seconds()))
            if not seconds:
                continue
            person = members.setdefault(
                user_id,
                {
                    "user_id": user_id,
                    "name": fields.get("Display Name") or user_id,
                    "seconds": 0,
                    "sessions": 0,
                },
            )
            person["seconds"] += seconds
            person["sessions"] += 1
        return sorted(members.values(), key=lambda p: (-p["seconds"], p["user_id"]))

    async def run(self, now=None):
        now = now or datetime.now(self.timezone)
        if not self.is_due(now) or not self.events.ready:
            return None
        channel_id = self.get_channel_id()
        if not channel_id:
            return None
        year = now.astimezone(self.timezone).year
        channel = self.events.bot.get_channel(int(channel_id))
        if channel is None:
            channel = await self.events.bot.fetch_channel(int(channel_id))
        guild_id = str(channel.guild.id)
        key = f"annual-summary-{guild_id}-{year}"
        async with self.events.lock:
            event = self.events.events.get(key)
            if event is None:
                # Also recover an Airtable create whose response was lost.
                row = await self.store._find_one(EVENTS, "Key", key)
                if row:
                    event = json.loads(row["fields"]["Data"])
                    event["record_id"] = row["id"]
                    self.events.events[key] = event
            if event and event["status"] != "draft":
                # Includes cancelled/review/publishing: never blindly resend.
                return event
            if event is None:
                await self.events.channel(
                    {"channel_id": str(channel_id), "guild_id": guild_id}
                )
                totals = await self.totals(guild_id, year)
                voice_members = await self.voice_totals(guild_id, year)
                if (
                    voice_members
                    and not channel.permissions_for(channel.guild.me).attach_files
                ):
                    raise ValueError(
                        "Teemo needs Attach Files permission for the yearly voice CSV."
                    )
                totals["voice_members"] = voice_members
                totals["voice_seconds"] = sum(p["seconds"] for p in voice_members)
                totals["voice_sessions"] = sum(p["sessions"] for p in voice_members)
                body = (
                    f"Here is our {year} game-poll recap!\n"
                    "**Period: January 1–December 24** (not the complete calendar year).\n\n"
                    f"**Daily polls posted:** {totals['polls']}\n"
                    f"**Members who answered:** {totals['participants']}\n"
                    f"**Saved answers:** {totals['responses']}\n"
                    f"Yes: **{totals['yes']}** · Maybe: **{totals['maybe']}** · No: **{totals['no']}**\n\n"
                    "Counts use each member's latest saved answer per daily poll; "
                    "one-time event polls are not included.\n\n"
                    f"**Total member voice time:** {totals['voice_seconds'] / 3600:,.1f} hours\n"
                    f"**Members in voice:** {len(voice_members)} · "
                    f"**Voice sessions:** {totals['voice_sessions']}\n"
                )
                if voice_members:
                    body += "\n**Voice time highlights (up to 10 members)**\n"
                    for person in voice_members[:10]:
                        name = discord.utils.escape_markdown(
                            " ".join(person["name"].split())[:70]
                        )
                        body += f"{name}: **{person['seconds'] / 3600:,.1f} h**\n"
                    body += "\nThe attached CSV lists every member with recorded voice time.\n"
                else:
                    body += "\nNo voice time was recorded for this period.\n"
                body += (
                    "\nVoice time measures channel presence, not speaking. It includes time "
                    "spent alone; overlapping members' time is counted separately. "
                    "Bot downtime may make durations approximate.\n\n"
                    "Thanks for making time to play together. Happy holidays!"
                )
                if not totals["polls"]:
                    body += "\n\nNo daily polls were recorded for this period."
                event = {
                    "key": key,
                    "guild_id": guild_id,
                    "channel_id": str(channel_id),
                    "kind": "announcement",
                    "title": f"Teemo {year} — Yearly community summary",
                    "body": body,
                    "options": [],
                    "no_reason": False,
                    "publish_at": None,
                    "closes_at": None,
                    "event_at": None,
                    "created_by": str(self.events.bot.user.id),
                    "created_at": now.isoformat(),
                    "status": "draft",
                    "history": [
                        {"action": "annual-summary-generated", "at": now.isoformat()}
                    ],
                    "annual_summary": totals,
                }
                await self.events.save(event)
            result = await self.events.publish(dict(event))
            LOGGER.info("Yearly summary %s: %s", key, result["status"])
            return result
