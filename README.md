# Discord Name Announcer Bot

When someone joins a voice channel that already has people in it, the bot
jumps in, plays a pre-recorded clip of that person's name, and leaves.
The first person into an empty channel stays un-announced.

## How it works

- Watches **all** voice channels in the server.
- Announces only when **≥ 1 other human** is already in the channel.
- 60-second cooldown per person so reconnect spam doesn't blast audio.
- People with no recorded clip are skipped silently.
- Admins manage clips with slash commands:
  - `/setclip @member audio:<file>` — add or **replace** a clip by uploading it
  - `/setclip @member url:<link>` — add or **replace** a clip from a direct audio URL
  - `/removeclip @member` — delete a clip
  - `/clips` — list who has clips

For `/setclip`, provide exactly one source: `audio` or `url`. URL sources must be
public HTTP(S) links that point directly to a supported audio file, not a YouTube,
Spotify, SoundCloud, or other webpage. Supported formats are MP3, WAV, OGG, M4A,
WebM, and Opus; files are limited to 10 MiB.

## Setup (one time, ~15 min)

### 1. Create the bot on Discord
1. Go to https://discord.com/developers/applications → **New Application**.
2. Left menu → **Bot** → **Reset Token** → copy the token (keep it secret).
3. Same page, scroll down → enable **Server Members Intent** and
   **Presence Intent** is NOT needed, but **Voice States** works by default.
   (You need: Server Members Intent ✅)
4. Left menu → **OAuth2 → URL Generator**:
   - Scopes: `bot` + `applications.commands`
   - Bot Permissions: `View Channel`, `Connect`, `Speak`
5. Open the generated URL, pick your server, authorize.

### 2. Run it locally first (test)
```bash
pip install -r requirements.txt
```
Install **FFmpeg** and make sure `ffmpeg` is on your PATH
(Windows: `winget install ffmpeg` then restart the terminal).

```bash
copy .env.example .env   # then paste your token into .env
python bot.py
```
You should see `✅ Logged in as ...`.

### 3. Test in Discord
1. Friend A joins a voice channel alone → bot stays quiet ✅
2. You join the same channel → nothing (you have no clip yet)
3. Run `/setclip @yourself` and either attach a short recording of your name or
   paste a direct audio-file link into the `url` option
4. Leave and rejoin → bot hops in, says your name, leaves ✅

### 4. Host it 24/7 (Railway, ~$5/mo, easiest)
1. Push this folder to a GitHub repo.
2. https://railway.app → **New Project → Deploy from GitHub repo**.
3. Railway reads `nixpacks.toml` automatically (Python + FFmpeg).
4. In Railway → **Variables** → add `DISCORD_TOKEN` = your token.
5. **Important:** add a **Volume** mounted at `/data` and set variable
   `CLIP_DIR=/data/clips` — otherwise uploaded clips vanish on redeploys.

Free alternative: Oracle Cloud "Always Free" VM (free forever, but more
setup work — ask me if you want that path instead).

## Files
| File | Purpose |
|---|---|
| `bot.py` | the whole bot |
| `requirements.txt` | Python dependencies |
| `.env.example` | token template — copy to `.env` |
| `nixpacks.toml` | Railway build config |
| `clips/` | auto-created; one audio file per Discord user ID |
