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


def _parse_clock(value: str, variable_name: str) -> clock_time:
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return clock_time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
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
    game_poll_service = GamePollService(bot, store, _poll_channel_ids(), TIMEZONE_NAME)
    bot.add_view(GamePollView(game_poll_service))


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
    if POLL_CLOCK <= local_clock < REPORT_CLOCK:
        await game_poll_service.post_daily_polls(now.date())
    elif local_clock >= REPORT_CLOCK:
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
        restored_views = await game_poll_service.restore_open_poll_views(
            datetime.now(BOT_TIMEZONE).date()
        )
        poll_runtime_started = True
        daily_game_poll.start()
        daily_game_poll_report.start()
        asyncio.create_task(_catch_up_game_poll_schedule())
        print(
            f"✅ Daily game poll enabled at {POLL_CLOCK:%H:%M}; "
            f"report at {REPORT_CLOCK:%H:%M} ({TIMEZONE_NAME}); "
            f"restored {restored_views} open poll view(s)"
        )


@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
):
    # ignore bots (including ourselves)
    if member.bot:
        return

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
                member, channel, datetime.now(BOT_TIMEZONE).date()
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
