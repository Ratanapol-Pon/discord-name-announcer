"""Persistent one-time polls and editorial posts, independent of daily game polls."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from collections import Counter
from datetime import datetime, timezone

import discord

LOGGER = logging.getLogger(__name__)
EVENTS = "Admin Events"
VOTES = "Event Votes"


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Dates must include a timezone.")
    return parsed.astimezone(timezone.utc)


def validate_event(data, now=None):
    now = now or utcnow()
    kind = data.get("kind", "poll")
    if kind not in {"poll", "news", "announcement"}:
        raise ValueError("Choose poll, news, or announcement.")
    title = str(data.get("title", "")).strip()
    body = str(data.get("body", "")).strip()
    if not 1 <= len(title) <= 200 or len(body) > 3500:
        raise ValueError("Use a title of 1–200 characters and a message under 3,500.")
    if kind != "poll" and not body:
        raise ValueError("A message is required.")
    options = data.get("options", []) if kind == "poll" else []
    if not isinstance(options, list) or any(not isinstance(x, str) for x in options):
        raise ValueError("Poll choices must be a list of text labels.")
    options = [x.strip() for x in options]
    if kind == "poll" and (
        not 2 <= len(options) <= 10
        or any(not 1 <= len(x) <= 80 for x in options)
        or len({x.casefold() for x in options}) != len(options)
    ):
        raise ValueError("Provide 2–10 distinct choices, each under 80 characters.")
    publish_at = timestamp(data["publish_at"]) if data.get("publish_at") else None
    closes_at = timestamp(data["closes_at"]) if data.get("closes_at") else None
    event_at = timestamp(data["event_at"]) if data.get("event_at") else None
    if publish_at and publish_at <= now:
        raise ValueError("The scheduled publishing time must be in the future.")
    if kind == "poll" and (not closes_at or closes_at <= (publish_at or now)):
        raise ValueError("Choose a poll closing time after publication.")
    no_reason = bool(data.get("no_reason")) and kind == "poll"
    if no_reason and not any(x.casefold() == "no" for x in options):
        raise ValueError("Include a 'No' choice to require a reason for No.")
    channel_id = str(data.get("channel_id", ""))
    if not channel_id.isdecimal() or int(channel_id) <= 0:
        raise ValueError("Choose a destination channel.")
    return {
        "kind": kind,
        "title": title,
        "body": body,
        "options": options,
        "channel_id": channel_id,
        "no_reason": no_reason,
        "publish_at": publish_at.isoformat() if publish_at else None,
        "closes_at": closes_at.isoformat() if closes_at else None,
        "event_at": event_at.isoformat() if event_at else None,
    }


def event_embed(event, votes=None):
    embed = discord.Embed(
        title=event["title"],
        description=event["body"] or None,
        color=0x91B87A if event["kind"] == "poll" else 0xD9AE6E,
    )
    if event.get("event_at"):
        embed.add_field(
            name="Event time",
            value=f"<t:{int(timestamp(event['event_at']).timestamp())}:F>",
        )
    if event["kind"] == "poll":
        counts = Counter(v["choice"] for v in (votes or []))
        for i, option in enumerate(event["options"]):
            value = str(counts[i]) + " vote(s)" if votes is not None else "Choose below"
            embed.add_field(name=option, value=value, inline=True)
        label = "Closed" if event["status"] == "closed" else "Voting closes"
        embed.add_field(
            name=label,
            value=f"<t:{int(timestamp(event['closes_at']).timestamp())}:F>",
            inline=False,
        )
    embed.set_footer(text=f"Teemo • {event['kind'].title()} • {event['key']}")
    return embed


class EventReasonModal(discord.ui.Modal, title="Why can't you join?"):
    reason = discord.ui.TextInput(
        label="Reason", style=discord.TextStyle.paragraph, max_length=500
    )

    def __init__(self, manager, key, choice):
        super().__init__()
        self.manager, self.key, self.choice = manager, key, choice

    async def on_submit(self, interaction):
        await self.manager.vote(
            interaction, self.key, self.choice, str(self.reason.value)
        )


class EventSelect(discord.ui.Select):
    def __init__(self, manager, event):
        super().__init__(
            custom_id=f"teemo_event:{event['key']}",
            placeholder="Choose your answer",
            options=[
                discord.SelectOption(label=x, value=str(i))
                for i, x in enumerate(event["options"])
            ],
            disabled=event["status"] != "open",
        )
        self.manager, self.key = manager, event["key"]

    async def callback(self, interaction):
        event = self.manager.events.get(self.key)
        choice = int(self.values[0])
        if (
            event
            and event.get("no_reason")
            and event["options"][choice].casefold() == "no"
        ):
            await interaction.response.send_modal(
                EventReasonModal(self.manager, self.key, choice)
            )
        else:
            await self.manager.vote(interaction, self.key, choice)


class EventView(discord.ui.View):
    def __init__(self, manager, event):
        super().__init__(timeout=None)
        self.add_item(EventSelect(manager, event))


class EventManager:
    def __init__(self, bot, store):
        self.bot, self.store = bot, store
        self.events = {}
        self.lock = asyncio.Lock()
        self.ready = False
        self.last_error = None
        self.restore_error = None

    async def save(self, event):
        fields = {
            "Key": event["key"],
            "Guild ID": event["guild_id"],
            "Title": event["title"],
            "Status": event["status"],
            "Updated At": utcnow().isoformat(),
            "Data": json.dumps(
                {k: v for k, v in event.items() if k != "record_id"}, ensure_ascii=False
            ),
        }
        if event.get("record_id"):
            await self.store._update(EVENTS, event["record_id"], fields)
        else:
            row = await self.store._create(EVENTS, fields)
            event["record_id"] = row["id"]
        self.events[event["key"]] = copy.deepcopy(event)
        return copy.deepcopy(event)

    async def restore(self):
        async with self.lock:
            for row in await self.store.list_records(EVENTS):
                try:
                    item = json.loads(row["fields"]["Data"])
                    for field in (
                        "key",
                        "guild_id",
                        "channel_id",
                        "kind",
                        "title",
                        "body",
                        "options",
                        "created_at",
                        "status",
                    ):
                        if field not in item:
                            raise ValueError("Incomplete event record")
                    item["record_id"] = row["id"]
                except (ValueError, TypeError, KeyError):
                    LOGGER.exception(
                        "Skipping malformed event record: %s", row.get("id")
                    )
                    self.restore_error = "An Airtable event record is malformed and was skipped. Check Admin Events."
                    continue
                self.events[item["key"]] = item
                if item["status"] == "open" and item.get("message_id"):
                    self.bot.add_view(
                        EventView(self, item), message_id=int(item["message_id"])
                    )
                if item["status"] == "publishing":
                    # A crash between Discord delivery and persistence must not duplicate a post.
                    try:
                        await self.recover(item)
                    except Exception:
                        LOGGER.exception(
                            "Event recovery needs attention: %s", item["key"]
                        )
                        item.update(
                            status="review",
                            error="Could not check delivery after restart. Check channel permissions, then use Check delivery.",
                        )
                        await self.save(item)
            self.ready = True
            LOGGER.info("Event scheduler ready: %s saved event(s)", len(self.events))

    async def channel(self, event):
        channel = self.bot.get_channel(int(event["channel_id"]))
        if channel is None:
            channel = await self.bot.fetch_channel(int(event["channel_id"]))
        if (
            not isinstance(channel, discord.TextChannel)
            or str(channel.guild.id) != event["guild_id"]
        ):
            raise ValueError("Choose a text channel in this server.")
        perms = channel.permissions_for(channel.guild.me)
        if not (
            perms.view_channel
            and perms.send_messages
            and perms.embed_links
            and perms.read_message_history
        ):
            raise ValueError(
                "Teemo needs View Channel, Send Messages, Embed Links, and Read Message History in that channel."
            )
        return channel

    async def create(self, data, guild_id, user_id):
        event = validate_event(data)
        event.update(
            key=uuid.uuid4().hex,
            guild_id=str(guild_id),
            created_by=str(user_id),
            created_at=utcnow().isoformat(),
            status="draft",
            history=[],
        )
        await self.channel(event)
        async with self.lock:
            return await self.save(event)

    def owned(self, key, guild_id):
        item = self.events.get(key)
        if not item or item["guild_id"] != str(guild_id):
            raise ValueError("Event not found in this server.")
        return copy.deepcopy(item)

    async def edit(self, key, data, guild_id, user_id):
        updated = validate_event(data)
        async with self.lock:
            event = self.owned(key, guild_id)
            if event["status"] != "draft":
                raise ValueError(
                    "Only unpublished drafts can be edited. Duplicate this post to make a new version."
                )
            event.update(updated)
            await self.channel(event)
            event.setdefault("history", []).append(
                {"action": "edited", "by": str(user_id), "at": utcnow().isoformat()}
            )
            return await self.save(event)

    async def action(self, key, action, guild_id, user_id):
        async with self.lock:
            event = self.owned(key, guild_id)
            event.setdefault("history", []).append(
                {"action": action, "by": str(user_id), "at": utcnow().isoformat()}
            )
            if action == "publish" and event["status"] == "draft":
                if event.get("closes_at") and timestamp(event["closes_at"]) <= utcnow():
                    raise ValueError(
                        "This draft's closing time has passed. Duplicate it with a new time."
                    )
                if (
                    event.get("publish_at")
                    and timestamp(event["publish_at"]) > utcnow()
                ):
                    event["status"] = "scheduled"
                    return await self.save(event)
                return await self.publish(event)
            if action == "cancel" and event["status"] in {
                "draft",
                "scheduled",
                "review",
            }:
                event["status"] = "cancelled"
                return await self.save(event)
            if action == "close" and event["status"] == "open":
                return await self.close(event)
            if action == "recover" and event["status"] == "review":
                return await self.recover(event)
            raise ValueError("This action is not available for the current status.")

    async def publish(self, event):
        channel = await self.channel(event)
        event["status"] = "publishing"
        await self.save(event)
        try:
            displayed = dict(event, status="open")
            msg = await channel.send(
                embed=event_embed(displayed),
                view=EventView(self, displayed) if event["kind"] == "poll" else None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            event.update(
                message_id=str(msg.id),
                published_at=utcnow().isoformat(),
                status="open" if event["kind"] == "poll" else "published",
                error=None,
            )
            return await self.save(event)
        except Exception:
            LOGGER.exception("Event publication needs review: %s", event["key"])
            event.update(
                status="review",
                error="Delivery could not be confirmed. Use Check delivery before creating another post.",
            )
            await self.save(event)
            return event

    async def recover(self, event):
        channel = await self.channel(event)
        async for msg in channel.history(
            limit=100, after=timestamp(event["created_at"])
        ):
            if msg.author.id == self.bot.user.id and any(
                event["key"] in (e.footer.text or "") for e in msg.embeds
            ):
                event.update(
                    message_id=str(msg.id),
                    status="open" if event["kind"] == "poll" else "published",
                    error=None,
                )
                if event["kind"] == "poll":
                    self.bot.add_view(EventView(self, event), message_id=msg.id)
                return await self.save(event)
        event.update(
            status="review",
            error="No matching message found in the latest 100 channel messages. Check Discord before cancelling and recreating.",
        )
        return await self.save(event)

    async def votes(self, event):
        formula = f"AND({self.store._formula_equals('Guild ID', event['guild_id'])},FIND('{event['key']}:',{{Key}})=1)"
        return [
            json.loads(r["fields"]["Data"])
            for r in await self.store.list_records(VOTES, formula)
        ]

    async def vote(self, interaction, key, choice, reason=""):
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.lock:
                event = self.owned(key, interaction.guild_id)
                if (
                    event["status"] != "open"
                    or timestamp(event["closes_at"]) <= utcnow()
                ):
                    raise ValueError("This poll is closed.")
                if not 0 <= choice < len(event["options"]):
                    raise ValueError("Choose a valid option.")
                if (
                    event.get("no_reason")
                    and event["options"][choice].casefold() == "no"
                    and not reason.strip()
                ):
                    raise ValueError("Please give a reason for No.")
                vote = {
                    "event_key": key,
                    "user_id": str(interaction.user.id),
                    "display_name": interaction.user.display_name,
                    "choice": choice,
                    "answer": event["options"][choice],
                    "reason": reason.strip()[:500],
                    "at": utcnow().isoformat(),
                }
                vote_key = f"{key}:{interaction.user.id}"
                row = await self.store._find_one(VOTES, "Key", vote_key)
                fields = {
                    "Key": vote_key,
                    "Guild ID": event["guild_id"],
                    "Title": vote["display_name"],
                    "Status": vote["answer"],
                    "Updated At": vote["at"],
                    "Data": json.dumps(vote, ensure_ascii=False),
                }
                if row:
                    await self.store._update(VOTES, row["id"], fields)
                else:
                    await self.store._create(VOTES, fields)
            await interaction.followup.send(
                "Your answer is saved. You can change it until voting closes.",
                ephemeral=True,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            LOGGER.exception("Event vote failed")
            await interaction.followup.send(
                "Your answer could not be saved. Please try again.", ephemeral=True
            )

    async def close(self, event):
        event.update(status="closed", closed_at=utcnow().isoformat())
        await self.save(event)
        return await self.finish_close(event)

    async def finish_close(self, event):
        votes = await self.votes(event)
        channel = await self.channel(event)
        try:
            msg = await channel.fetch_message(int(event["message_id"]))
            await msg.edit(embed=event_embed(event, votes), view=EventView(self, event))
        except discord.NotFound:
            event["error"] = "Poll is closed; the original Discord message was deleted."
        event["results_updated"] = True
        return await self.save(event)

    async def tick(self):
        if not self.ready:
            return
        async with self.lock:
            self.last_error = self.restore_error
            for original in list(self.events.values()):
                event = dict(original)
                try:
                    if (
                        event["status"] == "scheduled"
                        and timestamp(event["publish_at"]) <= utcnow()
                    ):
                        if (
                            event.get("closes_at")
                            and timestamp(event["closes_at"]) <= utcnow()
                        ):
                            event.update(
                                status="cancelled",
                                error="Publication window expired while Teemo was offline.",
                            )
                            await self.save(event)
                        else:
                            await self.publish(event)
                    elif (
                        event["status"] == "open"
                        and timestamp(event["closes_at"]) <= utcnow()
                    ):
                        await self.close(event)
                    elif event["status"] == "closed" and not event.get(
                        "results_updated"
                    ):
                        await self.finish_close(event)
                except Exception:
                    LOGGER.exception("Event scheduler failed: %s", event["key"])
                    self.last_error = (
                        "An event task failed; check channel permissions and retry."
                    )
