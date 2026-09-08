"""
Discord Name Announcer Bot
--------------------------
When a member joins ANY voice channel and at least one other human is
already inside, the bot joins, plays that member's pre-recorded name
clip, and leaves immediately.

Admins add clips with:  /setclip @member <audio file or direct URL>
Uploading again for the same member replaces the old clip.
"""

import asyncio
import glob
import ipaddress
import logging
import os
import socket
import time
import uuid
from datetime import datetime
from datetime import time as clock_time
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from airtable_store import AirtablePollStore
from game_poll import GamePollService, GamePollView

load_dotenv()
LOGGER = logging.getLogger(__name__)
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN. Put it in a .env file (see .env.example).")


def _clock_from_text(value: str) -> clock_time:
    if (
        not isinstance(value, str)
        or len(value) != 5
        or value[2] != ":"
        or not value[:2].isdigit()
        or not value[3:].isdigit()
    ):
        raise ValueError("Time must use 24-hour HH:MM format.")
    try:
        return clock_time(hour=int(value[:2]), minute=int(value[3:]))
    except ValueError as exc:
        raise ValueError("Time must use 24-hour HH:MM format.") from exc


def _parse_clock(value: str, variable_name: str) -> clock_time:
    try:
        return _clock_from_text(value)
    except ValueError as exc:
        raise SystemExit(f"{variable_name} must use 24-hour HH:MM format.") from exc


TIMEZONE_NAME = os.getenv("BOT_TIMEZONE", "Asia/Bangkok")
try:
    BOT_TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError as exc:
    raise SystemExit(f"Unknown BOT_TIMEZONE: {TIMEZONE_NAME}") from exc

POLL_CLOCK = _parse_clock(os.getenv("POLL_TIME", "11:59"), "POLL_TIME")
REPORT_CLOCK = _parse_clock(os.getenv("REPORT_TIME", "17:00"), "REPORT_TIME")
if REPORT_CLOCK <= POLL_CLOCK:
    raise SystemExit("REPORT_TIME must be later than POLL_TIME on the same day.")
POLL_RUN_TIME = POLL_CLOCK.replace(tzinfo=BOT_TIMEZONE)
REPORT_RUN_TIME = REPORT_CLOCK.replace(tzinfo=BOT_TIMEZONE)

CLIP_DIR = os.getenv("CLIP_DIR", "clips")  # where name clips live
COOLDOWN_SECONDS = 60  # anti-spam per member
MAX_CLIP_BYTES = 10 * 1024 * 1024  # 10 MiB
DOWNLOAD_TIMEOUT_SECONDS = 20
MAX_REDIRECTS = 3
ALLOWED_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".webm", ".opus"}
CONTENT_TYPE_EXT = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/vnd.wave": ".wav",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/webm": ".webm",
    "video/webm": ".webm",
    "audio/opus": ".opus",
}

os.makedirs(CLIP_DIR, exist_ok=True)

intents = discord.Intents.default()
intents.voice_states = True  # required: detect voice joins
intents.members = True  # required: see who is in channels

bot = commands.Bot(command_prefix="!", intents=intents)

last_announce: dict[int, float] = {}  # member_id -> unix time
guild_locks: dict[int, asyncio.Lock] = {}  # one playback at a time per server
game_poll_service: GamePollService | None = None
poll_runtime_started = False
active_poll_clock = POLL_CLOCK
active_report_clock = REPORT_CLOCK
announcement_channel_id: int | None = None


def _poll_channel_ids() -> list[int]:
    raw = os.getenv("POLL_CHANNEL_IDS") or os.getenv("POLL_CHANNEL_ID", "")
    try:
        channel_ids = [int(item.strip()) for item in raw.split(",") if item.strip()]
    except ValueError as exc:
        raise SystemExit(
            "POLL_CHANNEL_ID(S) must contain Discord channel IDs only."
        ) from exc
    if not channel_ids:
        raise SystemExit(
            "Missing POLL_CHANNEL_ID (or comma-separated POLL_CHANNEL_IDS)."
        )
    return list(dict.fromkeys(channel_ids))


def configure_game_poll() -> None:
    """Create the Airtable store and register the persistent poll view."""
    global game_poll_service

    airtable_token = os.getenv("AIRTABLE_TOKEN")
    airtable_base_id = os.getenv("AIRTABLE_BASE_ID")
    if not airtable_token:
        raise SystemExit("Missing AIRTABLE_TOKEN.")
    if not airtable_base_id:
        raise SystemExit("Missing AIRTABLE_BASE_ID.")

    store = AirtablePollStore(airtable_token, airtable_base_id)
    game_poll_service = GamePollService(
        bot,
        store,
        _poll_channel_ids(),
        TIMEZONE_NAME,
        f"{active_report_clock:%H:%M}",
    )
    bot.add_view(GamePollView(game_poll_service))


def _parse_channel_ids(value: str) -> list[int]:
    try:
        channel_ids = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError("Channel IDs must be numbers separated by commas.") from exc
    if not channel_ids:
        raise ValueError("At least one poll channel ID is required.")
    return list(dict.fromkeys(channel_ids))


async def _get_guild_message_channel(
    channel_id: int, guild_id: int
) -> discord.abc.Messageable:
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    if (
        not isinstance(channel, discord.abc.Messageable)
        or getattr(getattr(channel, "guild", None), "id", None) != guild_id
    ):
        raise ValueError(f"{channel_id} is not a text channel in this server.")
    return channel


def _apply_runtime_settings(
    poll_clock: clock_time,
    report_clock: clock_time,
    poll_channel_ids: list[int],
    post_channel_id: int | None,
) -> None:
    """Apply validated settings without restarting Teemo."""
    global active_poll_clock, active_report_clock, announcement_channel_id

    if report_clock <= poll_clock:
        raise ValueError("Report time must be later than poll time on the same day.")
    if not poll_channel_ids:
        raise ValueError("At least one poll channel is required.")

    active_poll_clock = poll_clock
    active_report_clock = report_clock
    announcement_channel_id = post_channel_id
    if game_poll_service:
        game_poll_service.channel_ids = list(dict.fromkeys(poll_channel_ids))
        game_poll_service.report_time = f"{report_clock:%H:%M}"

    daily_game_poll.change_interval(time=poll_clock.replace(tzinfo=BOT_TIMEZONE))
    daily_game_poll_report.change_interval(
        time=report_clock.replace(tzinfo=BOT_TIMEZONE)
    )


async def _load_runtime_settings() -> None:
    if not game_poll_service:
        return
    settings = await game_poll_service.store.get_bot_settings()
    if not settings:
        return

    poll_clock = _clock_from_text(
        settings.get("poll_time") or f"{active_poll_clock:%H:%M}"
    )
    report_clock = _clock_from_text(
        settings.get("report_time") or f"{active_report_clock:%H:%M}"
    )
    channel_ids = settings.get("poll_channel_ids") or game_poll_service.channel_ids
    _apply_runtime_settings(
        poll_clock,
        report_clock,
        channel_ids,
        settings.get("announcement_channel_id"),
    )


def clip_path(user_id: int) -> str | None:
    """Return the saved clip file for a user, or None."""
    matches = glob.glob(os.path.join(CLIP_DIR, f"{user_id}.*"))
    return matches[0] if matches else None


def _extension_from_url_or_type(url: str, content_type: str) -> str | None:
    """Find a supported audio extension from a URL path or Content-Type."""
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in ALLOWED_EXT:
        return ext
    return CONTENT_TYPE_EXT.get(content_type.split(";", 1)[0].strip().lower())


async def _validate_public_url(url: str) -> None:
    """Reject malformed URLs and hosts that resolve to private/local networks."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("The link must be a valid public HTTP or HTTPS URL.")
    if parsed.username or parsed.password:
        raise ValueError("Links containing a username or password are not allowed.")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise ValueError("The link's host could not be resolved.") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError(
                "Links to private or local network addresses are not allowed."
            )


async def _download_url_clip(url: str, temp_path: str) -> str:
    """Download a direct public audio URL to temp_path and return its extension."""
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
    current_url = url

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for redirect_count in range(MAX_REDIRECTS + 1):
            await _validate_public_url(current_url)
            async with session.get(current_url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    if redirect_count == MAX_REDIRECTS:
                        raise ValueError("The audio link redirects too many times.")
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("The audio link returned an invalid redirect.")
                    current_url = urljoin(current_url, location)
                    continue

                if response.status < 200 or response.status >= 300:
                    raise ValueError(f"The audio link returned HTTP {response.status}.")

                if response.content_length and response.content_length > MAX_CLIP_BYTES:
                    raise ValueError("The linked audio file is larger than 10 MiB.")

                ext = _extension_from_url_or_type(
                    str(response.url), response.headers.get("Content-Type", "")
                )
                if not ext:
                    raise ValueError(
                        "The link must point directly to a supported audio file "
                        f"({', '.join(sorted(ALLOWED_EXT))})."
                    )

                downloaded = 0
                with open(temp_path, "wb") as temp_file:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        downloaded += len(chunk)
                        if downloaded > MAX_CLIP_BYTES:
                            raise ValueError(
                                "The linked audio file is larger than 10 MiB."
                            )
                        temp_file.write(chunk)

                if downloaded == 0:
                    raise ValueError("The linked audio file is empty.")
                return ext

    raise ValueError("The audio link could not be downloaded.")


def _install_clip(temp_path: str, user_id: int, ext: str) -> None:
    """Atomically install a downloaded clip, then remove older formats."""
    dest = os.path.join(CLIP_DIR, f"{user_id}{ext}")
    os.replace(temp_path, dest)
    for old in glob.glob(os.path.join(CLIP_DIR, f"{user_id}.*")):
        if os.path.normcase(old) != os.path.normcase(dest):
            os.remove(old)


# ---------------------------------------------------------------- events


@tasks.loop(time=POLL_RUN_TIME)
async def daily_game_poll():
    if game_poll_service:
        today = datetime.now(BOT_TIMEZONE).date()
        await game_poll_service.post_daily_polls(today)


@tasks.loop(time=REPORT_RUN_TIME)
async def daily_game_poll_report():
    if game_poll_service:
        today = datetime.now(BOT_TIMEZONE).date()
        await game_poll_service.generate_daily_reports(today)


async def _catch_up_game_poll_schedule() -> None:
    """Recover today's poll/report when the bot restarts after a scheduled time."""
    if not game_poll_service:
        return
    now = datetime.now(BOT_TIMEZONE)
    local_clock = now.time().replace(tzinfo=None)
    if active_poll_clock <= local_clock < active_report_clock:
        await game_poll_service.post_daily_polls(now.date())
    elif local_clock >= active_report_clock:
        await game_poll_service.generate_daily_reports(now.date())


@bot.event
async def on_ready():
    global poll_runtime_started

    await bot.tree.sync()  # register slash commands with Discord
    print(f"✅ Logged in as {bot.user} (id {bot.user.id})")

    if game_poll_service and not poll_runtime_started:
        try:
            await game_poll_service.store.healthcheck()
        except Exception:
            LOGGER.exception(
                "Airtable health check failed. Verify the base schema and access token."
            )
            return
        try:
            await _load_runtime_settings()
        except Exception:
            LOGGER.exception(
                "Stored Teemo settings are invalid; using environment defaults."
            )
        restored_views = await game_poll_service.restore_open_poll_views(
            datetime.now(BOT_TIMEZONE).date()
        )
        try:
            started_sessions, closed_sessions = (
                await game_poll_service.reconcile_voice_sessions(
                    datetime.now(BOT_TIMEZONE).date()
                )
            )
            voice_session_status = (
                f"voice sessions reconciled ({started_sessions} started, "
                f"{closed_sessions} closed)"
            )
        except Exception:
            LOGGER.exception("Failed to reconcile active voice sessions")
            voice_session_status = "voice-session reconciliation failed"
        try:
            started_solo, closed_solo = (
                await game_poll_service.reconcile_solo_voice_sessions(
                    datetime.now(BOT_TIMEZONE).date()
                )
            )
            solo_session_status = (
                f"solo periods reconciled ({started_solo} started, "
                f"{closed_solo} closed)"
            )
        except Exception:
            LOGGER.exception("Failed to reconcile active solo voice periods")
            solo_session_status = "solo-period reconciliation failed"
        poll_runtime_started = True
        daily_game_poll.start()
        daily_game_poll_report.start()
        asyncio.create_task(_catch_up_game_poll_schedule())
        print(
            f"✅ Daily game poll enabled at {active_poll_clock:%H:%M}; "
            f"report at {active_report_clock:%H:%M} ({TIMEZONE_NAME}); "
            f"restored {restored_views} open poll view(s); "
            f"{voice_session_status}; {solo_session_status}"
        )


@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
):
    # ignore bots (including ourselves)
    if member.bot:
        return

    # Ignore mute/deafen/video changes that do not change voice channel.
    if before.channel == after.channel:
        return

    event_time = datetime.now(BOT_TIMEZONE)
    local_date = event_time.date()
    if game_poll_service:
        try:
            await game_poll_service.track_solo_voice_channels(
                (before.channel, after.channel), local_date, event_time
            )
        except Exception:
            LOGGER.exception(
                "Failed to record solo voice period for Discord user %s", member.id
            )
        try:
            await game_poll_service.track_voice_session(
                member, before.channel, after.channel, local_date, event_time
            )
        except Exception:
            LOGGER.exception(
                "Failed to record voice session for Discord user %s", member.id
            )

    # only react to a FRESH join from outside voice chat.
    # (moving between channels does not re-announce; change this `if`
    #  to `if after.channel is None or after.channel == before.channel:`
    #  if you want moves to announce too)
    if before.channel is not None or after.channel is None:
        return

    channel = after.channel

    if game_poll_service:
        try:
            await game_poll_service.track_voice_join(
                member, channel, local_date
            )
        except Exception:
            LOGGER.exception(
                "Failed to record voice attendance for Discord user %s", member.id
            )

    # rule: stay quiet for the FIRST person in the channel
    other_humans = [m for m in channel.members if not m.bot and m.id != member.id]
    if len(other_humans) < 1:
        return

    # cooldown so disconnect/reconnect spam doesn't blast audio
    now = time.time()
    if now - last_announce.get(member.id, 0) < COOLDOWN_SECONDS:
        return

    path = clip_path(member.id)
    if not path:
        return  # nobody has recorded this person's name yet

    last_announce[member.id] = now
    try:
        await announce(channel, path)
    except Exception as e:
        print(f"⚠️ Failed to announce {member}: {e}")


async def announce(channel: discord.VoiceChannel, path: str):
    """Join the channel, play the clip, leave. Serialized per server."""
    lock = guild_locks.setdefault(channel.guild.id, asyncio.Lock())
    async with lock:
        vc = channel.guild.voice_client
        if vc is None:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)

        done = asyncio.Event()

        def _after(err):
            if err:
                print(f"⚠️ Playback error: {err}")
            bot.loop.call_soon_threadsafe(done.set)

        vc.play(discord.FFmpegPCMAudio(path), after=_after)
        await done.wait()
        await vc.disconnect()


# ---------------------------------------------------------------- commands


def _is_administrator(user: discord.abc.User) -> bool:
    permissions = getattr(user, "guild_permissions", None)
    return bool(getattr(permissions, "administrator", False))


def _admin_panel_embed(current_channel_id: int) -> discord.Embed:
    poll_channels = (
        game_poll_service.channel_ids if game_poll_service else _poll_channel_ids()
    )
    post_channel_id = announcement_channel_id or current_channel_id
    embed = discord.Embed(
        title="Teemo Admin Panel",
        description="Manage the daily game poll and publish server updates.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Daily schedule",
        value=(
            f"Poll: **{active_poll_clock:%H:%M}**\n"
            f"Summary: **{active_report_clock:%H:%M}**\n"
            f"Timezone: **{TIMEZONE_NAME}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Channels",
        value=(
            "Poll: " + ", ".join(f"<#{channel_id}>" for channel_id in poll_channels)
            + f"\nNews / announcements: <#{post_channel_id}>"
        ),
        inline=True,
    )
    embed.set_footer(text="Only the administrator who opened this panel can use it.")
    return embed


class ScheduleModal(discord.ui.Modal, title="Adjust Teemo's daily tasks"):
    def __init__(self, guild_id: int, admin_id: int, current_channel_id: int) -> None:
        super().__init__()
        self.guild_id = guild_id
        self.admin_id = admin_id
        poll_channels = (
            game_poll_service.channel_ids if game_poll_service else _poll_channel_ids()
        )
        self.poll_time = discord.ui.TextInput(
            label="Daily poll time (HH:MM)",
            default=f"{active_poll_clock:%H:%M}",
            min_length=5,
            max_length=5,
        )
        self.report_time = discord.ui.TextInput(
            label="Daily summary time (HH:MM)",
            default=f"{active_report_clock:%H:%M}",
            min_length=5,
            max_length=5,
        )
        self.poll_channels = discord.ui.TextInput(
            label="Poll channel ID(s), comma-separated",
            default=",".join(str(value) for value in poll_channels),
            max_length=300,
        )
        self.post_channel = discord.ui.TextInput(
            label="News / announcement channel ID",
            default=str(announcement_channel_id or current_channel_id),
            max_length=20,
        )
        self.add_item(self.poll_time)
        self.add_item(self.report_time)
        self.add_item(self.poll_channels)
        self.add_item(self.post_channel)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != self.admin_id or not _is_administrator(
            interaction.user
        ):
            await interaction.followup.send(
                "This panel can only be used by the administrator who opened it.",
                ephemeral=True,
            )
            return
        if not game_poll_service:
            await interaction.followup.send(
                "The game poll service is not configured.", ephemeral=True
            )
            return

        try:
            poll_clock = _clock_from_text(str(self.poll_time.value).strip())
            report_clock = _clock_from_text(str(self.report_time.value).strip())
            channel_ids = _parse_channel_ids(str(self.poll_channels.value))
            post_channel_id = int(str(self.post_channel.value).strip())
            if report_clock <= poll_clock:
                raise ValueError(
                    "Summary time must be later than poll time on the same day."
                )
            for channel_id in list(dict.fromkeys(channel_ids + [post_channel_id])):
                await _get_guild_message_channel(channel_id, self.guild_id)

            await game_poll_service.store.save_bot_settings(
                poll_time=f"{poll_clock:%H:%M}",
                report_time=f"{report_clock:%H:%M}",
                poll_channel_ids=channel_ids,
                announcement_channel_id=post_channel_id,
                updated_by=f"{interaction.user} ({interaction.user.id})",
            )
            _apply_runtime_settings(
                poll_clock, report_clock, channel_ids, post_channel_id
            )
            await _catch_up_game_poll_schedule()
            await interaction.followup.send(
                "Settings saved to Airtable and applied immediately.\n"
                f"Poll: **{poll_clock:%H:%M}** • Summary: **{report_clock:%H:%M}**",
                ephemeral=True,
            )
        except (TypeError, ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(
                f"Could not save settings: {exc}", ephemeral=True
            )
        except Exception:
            LOGGER.exception("Failed to update Teemo admin settings")
            await interaction.followup.send(
                "I couldn't save those settings. Please try again.", ephemeral=True
            )


class PublishPostModal(discord.ui.Modal):
    def __init__(
        self, kind: str, guild_id: int, admin_id: int, fallback_channel_id: int
    ) -> None:
        super().__init__(title=f"Create {kind}")
        self.kind = kind
        self.guild_id = guild_id
        self.admin_id = admin_id
        self.target_channel_id = announcement_channel_id or fallback_channel_id
        self.heading = discord.ui.TextInput(
            label=f"{kind.title()} title", max_length=200
        )
        self.body = discord.ui.TextInput(
            label="Message",
            style=discord.TextStyle.paragraph,
            max_length=4000,
        )
        self.add_item(self.heading)
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != self.admin_id or not _is_administrator(
            interaction.user
        ):
            await interaction.followup.send(
                "This panel can only be used by the administrator who opened it.",
                ephemeral=True,
            )
            return
        try:
            channel = await _get_guild_message_channel(
                self.target_channel_id, self.guild_id
            )
            color = (
                discord.Color.blue()
                if self.kind == "news"
                else discord.Color.orange()
            )
            embed = discord.Embed(
                title=str(self.heading.value).strip(),
                description=str(self.body.value).strip(),
                color=color,
                timestamp=datetime.now(BOT_TIMEZONE),
            )
            embed.set_footer(text=f"Teemo {self.kind.title()}")
            message = await channel.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
            await interaction.followup.send(
                f"{self.kind.title()} posted in <#{self.target_channel_id}>: "
                f"{message.jump_url}",
                ephemeral=True,
            )
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(
                f"Could not post the {self.kind}: {exc}", ephemeral=True
            )
        except Exception:
            LOGGER.exception("Failed to publish Teemo %s", self.kind)
            await interaction.followup.send(
                f"I couldn't post the {self.kind}. Please try again.", ephemeral=True
            )


class AdminPanelView(discord.ui.View):
    def __init__(self, admin_id: int, guild_id: int, channel_id: int) -> None:
        super().__init__(timeout=600)
        self.admin_id = admin_id
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.admin_id and _is_administrator(
            interaction.user
        ):
            return True
        await interaction.response.send_message(
            "This panel can only be used by the administrator who opened it.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Schedule", style=discord.ButtonStyle.secondary)
    async def schedule(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            ScheduleModal(self.guild_id, self.admin_id, self.channel_id)
        )

    @discord.ui.button(label="Post Poll Now", style=discord.ButtonStyle.success)
    async def post_poll(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not game_poll_service:
            await interaction.followup.send(
                "The game poll service is not configured.", ephemeral=True
            )
            return
        created = await game_poll_service.post_daily_polls(
            datetime.now(BOT_TIMEZONE).date()
        )
        message = (
            f"Posted today's poll in **{created}** configured channel(s)."
            if created
            else (
                "No new poll was posted; today's poll already exists or a "
                "channel failed."
            )
        )
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Report Now", style=discord.ButtonStyle.primary)
    async def post_report(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not game_poll_service:
            await interaction.followup.send(
                "The game poll service is not configured.", ephemeral=True
            )
            return
        generated = await game_poll_service.generate_daily_reports(
            datetime.now(BOT_TIMEZONE).date()
        )
        message = (
            f"Posted today's summary in **{generated}** configured channel(s)."
            if generated
            else (
                "No new summary was posted; there is no open poll or it "
                "already exists."
            )
        )
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Create News", style=discord.ButtonStyle.primary)
    async def create_news(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            PublishPostModal("news", self.guild_id, self.admin_id, self.channel_id)
        )

    @discord.ui.button(label="Announcement", style=discord.ButtonStyle.danger)
    async def create_announcement(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            PublishPostModal(
                "announcement", self.guild_id, self.admin_id, self.channel_id
            )
        )


@bot.tree.command(
    name="teemo_admin",
    description="Open Teemo's private bot-management panel (admin only)",
)
@app_commands.checks.has_permissions(administrator=True)
async def teemo_admin(interaction: discord.Interaction):
    if not interaction.guild_id or not interaction.channel_id:
        await interaction.response.send_message(
            "Open this panel from a server text channel.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        embed=_admin_panel_embed(interaction.channel_id),
        view=AdminPanelView(
            interaction.user.id, interaction.guild_id, interaction.channel_id
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="gamepoll_test",
    description="Post today's game poll immediately (admin only)",
)
@app_commands.checks.has_permissions(administrator=True)
async def gamepoll_test(interaction: discord.Interaction):
    if not game_poll_service or not interaction.channel_id:
        await interaction.response.send_message(
            "❌ The game poll is not configured for this channel.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    created = await game_poll_service.post_poll(
        interaction.channel_id, datetime.now(BOT_TIMEZONE).date()
    )
    message = (
        "✅ Test poll posted in this channel."
        if created
        else "ℹ️ Today's poll already exists in this channel."
    )
    await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(
    name="gamepoll_test_report",
    description="Close today's poll and post its report now (admin only)",
)
@app_commands.checks.has_permissions(administrator=True)
async def gamepoll_test_report(interaction: discord.Interaction):
    if not game_poll_service or not interaction.channel_id:
        await interaction.response.send_message(
            "❌ The game poll is not configured for this channel.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    generated = await game_poll_service.generate_report(
        interaction.channel_id, datetime.now(BOT_TIMEZONE).date()
    )
    message = (
        "✅ Test report posted and today's poll is now closed."
        if generated
        else "ℹ️ There is no open poll to report, or its report already exists."
    )
    await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(
    name="setclip",
    description="Set or replace the join-announcement clip for a member (admin only)",
)
@app_commands.describe(
    member="Who this clip belongs to",
    audio="Upload a short audio file",
    url="Or paste a direct public audio-file URL",
)
@app_commands.checks.has_permissions(administrator=True)
async def setclip(
    interaction: discord.Interaction,
    member: discord.Member,
    audio: discord.Attachment | None = None,
    url: str | None = None,
):
    if (audio is None) == (url is None):
        await interaction.response.send_message(
            "❌ Provide exactly one source: either an audio attachment or a direct audio URL.",
            ephemeral=True,
        )
        return

    if audio is not None:
        ext = os.path.splitext(audio.filename or "")[1].lower()
        if ext not in ALLOWED_EXT:
            await interaction.response.send_message(
                f"❌ Unsupported file type `{ext}`. Use one of: {', '.join(sorted(ALLOWED_EXT))}",
                ephemeral=True,
            )
            return
        if audio.size > MAX_CLIP_BYTES:
            await interaction.response.send_message(
                "❌ The audio file must be 10 MiB or smaller.", ephemeral=True
            )
            return
        if audio.size == 0:
            await interaction.response.send_message(
                "❌ The audio file is empty.", ephemeral=True
            )
            return

    await interaction.response.defer(ephemeral=True)
    temp_path = os.path.join(CLIP_DIR, f".{member.id}.{uuid.uuid4().hex}.tmp")

    try:
        if audio is not None:
            await audio.save(temp_path)
            if os.path.getsize(temp_path) > MAX_CLIP_BYTES:
                raise ValueError("The audio file must be 10 MiB or smaller.")
            if os.path.getsize(temp_path) == 0:
                raise ValueError("The audio file is empty.")
        else:
            ext = await _download_url_clip(url, temp_path)

        _install_clip(temp_path, member.id, ext)
        source = "uploaded file" if audio is not None else "direct URL"
        await interaction.followup.send(
            f"✅ Clip saved for **{member.display_name}** from the {source} — "
            "it replaced any previous clip.",
            ephemeral=True,
        )
    except (ValueError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        await interaction.followup.send(
            f"❌ Could not save the clip: {exc}", ephemeral=True
        )
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@bot.tree.command(
    name="removeclip", description="Delete a member's announcement clip (admin only)"
)
@app_commands.describe(member="Whose clip to delete")
@app_commands.checks.has_permissions(administrator=True)
async def removeclip(interaction: discord.Interaction, member: discord.Member):
    removed = False
    for old in glob.glob(os.path.join(CLIP_DIR, f"{member.id}.*")):
        os.remove(old)
        removed = True
    msg = (
        f"🗑️ Clip removed for **{member.display_name}**."
        if removed
        else f"ℹ️ **{member.display_name}** had no clip saved."
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(
    name="clips", description="List members who have a clip saved (admin only)"
)
@app_commands.checks.has_permissions(administrator=True)
async def clips(interaction: discord.Interaction):
    files = glob.glob(os.path.join(CLIP_DIR, "*.*"))
    if not files:
        await interaction.response.send_message("No clips saved yet.", ephemeral=True)
        return
    lines = []
    for f in files:
        uid = int(os.path.splitext(os.path.basename(f))[0])
        m = interaction.guild.get_member(uid)
        lines.append(f"• {m.display_name if m else f'unknown user {uid}'}")
    await interaction.response.send_message(
        "Saved clips:\n" + "\n".join(lines), ephemeral=True
    )


@setclip.error
@removeclip.error
@clips.error
@gamepoll_test.error
@gamepoll_test_report.error
@teemo_admin.error
async def admin_only_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
    else:
        raise error


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    configure_game_poll()
    bot.run(TOKEN)
