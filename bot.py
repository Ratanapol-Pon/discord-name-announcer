"""
Discord Name Announcer Bot
--------------------------
When a member joins ANY voice channel and at least one other human is
already inside, the bot joins, plays that member's pre-recorded name
clip, and leaves immediately.

Admins upload clips with:  /setclip @member <audio file>
Uploading again for the same member replaces the old clip.
"""

import os
import glob
import time
import asyncio

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN. Put it in a .env file (see .env.example).")

CLIP_DIR = os.getenv("CLIP_DIR", "clips")          # where name clips live
COOLDOWN_SECONDS = 60                              # anti-spam per member
ALLOWED_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".webm", ".opus"}

os.makedirs(CLIP_DIR, exist_ok=True)

intents = discord.Intents.default()
intents.voice_states = True   # required: detect voice joins
intents.members = True        # required: see who is in channels

bot = commands.Bot(command_prefix="!", intents=intents)

last_announce: dict[int, float] = {}   # member_id -> unix time
guild_locks: dict[int, asyncio.Lock] = {}  # one playback at a time per server


def clip_path(user_id: int) -> str | None:
    """Return the saved clip file for a user, or None."""
    matches = glob.glob(os.path.join(CLIP_DIR, f"{user_id}.*"))
    return matches[0] if matches else None


# ---------------------------------------------------------------- events

@bot.event
async def on_ready():
    await bot.tree.sync()  # register slash commands with Discord
    print(f"✅ Logged in as {bot.user} (id {bot.user.id})")


@bot.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after: discord.VoiceState):
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

    # rule: stay quiet for the FIRST person in the channel
    other_humans = [m for m in channel.members
                    if not m.bot and m.id != member.id]
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

@bot.tree.command(name="setclip",
                  description="Set or replace the join-announcement clip for a member (admin only)")
@app_commands.describe(member="Who this clip belongs to",
                       audio="Short audio file of their name (mp3/wav/ogg/m4a)")
@app_commands.checks.has_permissions(administrator=True)
async def setclip(interaction: discord.Interaction,
                  member: discord.Member,
                  audio: discord.Attachment):
    ext = os.path.splitext(audio.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        await interaction.response.send_message(
            f"❌ Unsupported file type `{ext}`. Use one of: {', '.join(sorted(ALLOWED_EXT))}",
            ephemeral=True)
        return

    # replace any old clip for this member
    for old in glob.glob(os.path.join(CLIP_DIR, f"{member.id}.*")):
        os.remove(old)

    dest = os.path.join(CLIP_DIR, f"{member.id}{ext}")
    await audio.save(dest)
    await interaction.response.send_message(
        f"✅ Clip saved for **{member.display_name}** — it replaced any previous clip.",
        ephemeral=True)


@bot.tree.command(name="removeclip",
                  description="Delete a member's announcement clip (admin only)")
@app_commands.describe(member="Whose clip to delete")
@app_commands.checks.has_permissions(administrator=True)
async def removeclip(interaction: discord.Interaction, member: discord.Member):
    removed = False
    for old in glob.glob(os.path.join(CLIP_DIR, f"{member.id}.*")):
        os.remove(old)
        removed = True
    msg = (f"🗑️ Clip removed for **{member.display_name}**." if removed
           else f"ℹ️ **{member.display_name}** had no clip saved.")
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="clips", description="List members who have a clip saved (admin only)")
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
        "Saved clips:\n" + "\n".join(lines), ephemeral=True)


@setclip.error
@removeclip.error
@clips.error
async def admin_only_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message(
            "❌ Admins only.", ephemeral=True)
    else:
        raise error


bot.run(TOKEN)
