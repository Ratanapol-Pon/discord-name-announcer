"""Daily Discord game poll with persistent Airtable-backed responses."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import date
from typing import Any

import discord

from airtable_store import AirtablePollStore, PollClosedError, PollNotFoundError

LOGGER = logging.getLogger(__name__)
POLL_QUESTION = "Does anyone want to play any game tonight?"


def poll_embed(poll_date: date, timezone_name: str) -> discord.Embed:
    embed = discord.Embed(
        title="Tonight's game poll",
        description=POLL_QUESTION,
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="How to answer",
        value=(
            "Choose **Yes**, **Maybe**, or **No** below. "
            "Choosing **No** opens a required reason form."
        ),
        inline=False,
    )
    embed.set_footer(
        text=f"Closes at 18:00 ({timezone_name}) • {poll_date.isoformat()} • You can change your vote"
    )
    return embed


def _response_names(responses: Iterable[dict[str, Any]], choice: str) -> str:
    names = [
        discord.utils.escape_markdown(str(response["display_name"]))
        for response in responses
        if response["choice"] == choice
    ]
    return ", ".join(names) if names else "—"


def report_embed(
    poll_date: date, timezone_name: str, report: dict[str, Any]
) -> discord.Embed:
    responses = list(report.get("responses") or [])
    embed = discord.Embed(
        title="Tonight's game poll — summary",
        description=f"Results for {poll_date.isoformat()}",
        color=discord.Color.green()
        if report.get("yes_count", 0)
        else discord.Color.gold(),
    )
    for choice, label, emoji in (
        ("yes", "Yes", "✅"),
        ("maybe", "Maybe", "🤔"),
        ("no", "No", "❌"),
    ):
        names = _response_names(responses, choice)
        if len(names) > 1000:
            names = names[:997] + "..."
        embed.add_field(
            name=f"{emoji} {label} ({int(report.get(f'{choice}_count', 0))})",
            value=names,
            inline=False,
        )

    reasons = [
        "• **{}:** {}".format(
            discord.utils.escape_markdown(str(item["display_name"])),
            discord.utils.escape_markdown(str(item["reason"])),
        )
        for item in report.get("no_reasons") or []
    ]
    reason_text = "\n".join(reasons) if reasons else "No ‘No’ reasons submitted."
    if len(reason_text) > 1000:
        reason_text = reason_text[:997] + "..."
    embed.add_field(name="Reasons from No votes", value=reason_text, inline=False)
    embed.set_footer(text=f"Closed at 18:00 ({timezone_name}) • Saved to Airtable")
    return embed


class NoReasonModal(discord.ui.Modal, title="Why can't you play tonight?"):
    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="For example: working late, family plans, or need rest",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )

    def __init__(self, service: GamePollService, message_id: int) -> None:
        super().__init__()
        self.service = service
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.save_interaction_response(
            interaction, self.message_id, "no", str(self.reason.value)
        )


class GamePollView(discord.ui.View):
    def __init__(self, service: GamePollService, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.service = service
        if disabled:
            for item in self.children:
                item.disabled = True

    @discord.ui.button(
        label="Yes",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="game_poll:yes",
    )
    async def yes(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self.service.save_interaction_response(
            interaction, interaction.message.id, "yes", None
        )

    @discord.ui.button(
        label="Maybe",
        emoji="🤔",
        style=discord.ButtonStyle.primary,
        custom_id="game_poll:maybe",
    )
    async def maybe(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self.service.save_interaction_response(
            interaction, interaction.message.id, "maybe", None
        )

    @discord.ui.button(
        label="No",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="game_poll:no",
    )
    async def no(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            NoReasonModal(self.service, interaction.message.id)
        )


class GamePollService:
    def __init__(
        self,
        bot: discord.Client,
        store: AirtablePollStore,
        channel_ids: list[int],
        timezone_name: str,
    ) -> None:
        self.bot = bot
        self.store = store
        self.channel_ids = channel_ids
        self.timezone_name = timezone_name
        self._poll_lifecycle_lock = asyncio.Lock()

    async def _get_channel(self, channel_id: int) -> discord.abc.Messageable:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable) or not getattr(
            channel, "guild", None
        ):
            raise RuntimeError(
                f"POLL_CHANNEL_ID {channel_id} is not a server text channel."
            )
        return channel

    async def post_daily_polls(self, poll_date: date) -> None:
        for channel_id in self.channel_ids:
            try:
                await self.post_poll(channel_id, poll_date)
            except Exception:
                LOGGER.exception("Failed to post game poll in channel %s", channel_id)

    async def restore_open_poll_views(self, poll_date: date) -> int:
        """Bind persisted button views to today's open poll messages."""
        restored = 0
        for channel_id in self.channel_ids:
            poll = await self.store.get_poll(channel_id, poll_date)
            if (
                poll
                and poll.get("message_id")
                and poll.get("status", "open") == "open"
            ):
                self.bot.add_view(
                    GamePollView(self), message_id=int(poll["message_id"])
                )
                restored += 1
        return restored

    async def post_poll(self, channel_id: int, poll_date: date) -> bool:
        channel = await self._get_channel(channel_id)
        guild_id = channel.guild.id
        poll = await self.store.create_poll(guild_id, channel_id, poll_date)
        if poll.get("message_id"):
            return False

        message = await channel.send(
            embed=poll_embed(poll_date, self.timezone_name),
            view=GamePollView(self),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.store.set_poll_message(poll["id"], message.id)
        return True

    async def save_interaction_response(
        self,
        interaction: discord.Interaction,
        message_id: int,
        choice: str,
        reason: str | None,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        display_name = getattr(interaction.user, "display_name", interaction.user.name)
        try:
            async with self._poll_lifecycle_lock:
                await self.store.record_response(
                    message_id, interaction.user.id, display_name, choice, reason
                )
            suffix = f" Reason: {reason.strip()}" if reason else ""
            await interaction.followup.send(
                f"Your response is now **{choice.title()}**.{suffix}", ephemeral=True
            )
        except (PollClosedError, PollNotFoundError, ValueError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            LOGGER.exception("Failed to save a game-poll response")
            await interaction.followup.send(
                "I couldn't save that response. Please try again.", ephemeral=True
            )

    async def generate_daily_reports(self, poll_date: date) -> None:
        for channel_id in self.channel_ids:
            try:
                await self.generate_report(channel_id, poll_date)
            except Exception:
                LOGGER.exception(
                    "Failed to generate game-poll report for channel %s", channel_id
                )

    async def generate_report(self, channel_id: int, poll_date: date) -> bool:
        channel = await self._get_channel(channel_id)
        poll = await self.store.get_poll(channel_id, poll_date)
        if not poll or not poll.get("message_id"):
            return False

        async with self._poll_lifecycle_lock:
            report = await self.store.get_report(poll["id"])
            if report and report.get("message_id"):
                return False
            if poll["status"] != "closed":
                # Close first so a response cannot be accepted after the snapshot.
                await self.store.close_poll(poll["id"])
            if not report:
                responses = await self.store.get_responses(poll["id"])
                report = await self.store.save_report(poll["id"], responses)

        try:
            poll_message = await channel.fetch_message(int(poll["message_id"]))
            await poll_message.edit(view=GamePollView(self, disabled=True))
        except (discord.NotFound, discord.Forbidden):
            LOGGER.warning(
                "Could not disable buttons on poll message %s", poll["message_id"]
            )

        summary_message = await channel.send(
            embed=report_embed(poll_date, self.timezone_name, report),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.store.set_report_message(poll["id"], summary_message.id)
        return True
