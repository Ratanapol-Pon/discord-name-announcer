# Discord Name Announcer Bot

When someone joins a voice channel that already has people in it, the bot
jumps in, plays a pre-recorded clip of that person's name, and leaves.
The first person into an empty channel stays un-announced.

For every human member, Teemo also stores each voice-channel session in
Airtable: join time, leave time, channel, and duration in seconds. Moving to a
different channel closes the old session and starts a new one. Bot accounts are
excluded, and active sessions are reconciled after a Teemo restart.

Teemo separately records **solo voice periods**. A solo period begins whenever
exactly one human is in a channel and ends when another human joins or the solo
person leaves. These records include the person, channel, start/end timestamps,
and duration in seconds; bot accounts do not affect the solo count.

It also runs a daily game poll:

- **11:59 Asia/Bangkok:** asks whether anyone wants to play a game tonight.
- **Yes** opens a private start-time picker (18:00–23:30 in 30-minute slots,
  plus Flexible); **Maybe** is a one-click response.
- **No** opens a required reason form.
- People can change their answer until the poll closes.
- When a **Yes** voter joins any server voice channel that day, Teemo records
  the first join time and voice channel in Airtable.
- **17:00 Asia/Bangkok:** closes the poll and posts a summary of votes and No
  reasons. Voice attendance stays private in Airtable and is not shown in the
  Discord summary.
- A restart between 11:59 and 17:00 catches up a missing poll; a restart after
  17:00 retries a missing report for an existing poll.

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
  - `/gamepoll_test` — post today's poll immediately
  - `/gamepoll_test_report` — close today's poll and post its report immediately

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
   - Bot Permissions: `View Channel`, `Send Messages`, `Embed Links`,
     `Read Message History`, `Connect`, `Speak`
5. Open the generated URL, pick your server, authorize.

### 2. Create the Airtable base

Create a base named **Teemo Game Polls** with these tables and fields:

- `Polls`: `Poll Key` (primary text), `Guild ID` (text), `Channel ID` (text),
  `Poll Date` (date), `Message ID` (text), `Question` (long text), `Status`
  (text or single select), and `Closed At` (date/time).
- `Responses`: `Response Key` (primary text), `Poll Key` (text), `User ID`
  (text), `Display Name` (text), `Choice` (text or single select), `Reason`
  (long text), `Play Time` (text), `Responded At` (ISO timestamp text),
  `Joined Voice Chat` (text), `Joined At` (ISO timestamp text), `Voice Channel
  ID` (text), and `Voice Channel Name` (text).
- `Reports`: `Poll Key` (primary text), `Message ID` (text), `Yes Count`,
  `Maybe Count`, and `No Count` (numbers), `No Reasons JSON` and
  `Responses JSON` (long text), and `Generated At` (date/time).
- `Voice Sessions`: `Session Key` (primary text), `Active Key`, `Guild ID`,
  `User ID`, `Display Name`, `Voice Channel ID`, `Voice Channel Name`, `Joined
  At`, `Left At`, `Duration Seconds` (number), `Session Date`, and `Status`.
- `Solo Voice Sessions`: `Solo Session Key` (primary text), `Active Key`,
  `Guild ID`, `User ID`, `Display Name`, `Voice Channel ID`, `Voice Channel
  Name`, `Started Alone At`, `Ended Alone At`, `Duration Seconds` (number),
  `Session Date`, and `Status`.

Create a personal access token restricted to this base with only
`data.records:read` and `data.records:write`. Keep it server-side and never
commit it.

### 3. Configure and run locally

Copy `.env.example` to `.env`, then set:

```dotenv
DISCORD_TOKEN=your_discord_bot_token
POLL_CHANNEL_ID=your_discord_text_channel_id
AIRTABLE_BASE_ID=app_your_airtable_base_id
AIRTABLE_TOKEN=your_airtable_personal_access_token
```

To copy a Discord channel ID, enable Discord **Developer Mode**, right-click the
target text channel, and choose **Copy Channel ID**.

```bash
pip install -r requirements.txt
```
Install **FFmpeg** and make sure `ffmpeg` is on your PATH
(Windows: `winget install ffmpeg` then restart the terminal).

```bash
python bot.py
```
You should see both `✅ Logged in as ...` and `✅ Daily game poll enabled ...`.

### 4. Test in Discord
1. Friend A joins a voice channel alone → bot stays quiet ✅
2. You join the same channel → nothing (you have no clip yet)
3. Run `/setclip @yourself` and either attach a short recording of your name or
   paste a direct audio-file link into the `url` option
4. Leave and rejoin → bot hops in, says your name, leaves ✅

For a quick poll test, run `/gamepoll_test`, vote with all three buttons, then
run `/gamepoll_test_report` and confirm:

- the No button requires text;
- the summary appears at the configured report time;
- rows exist in the `Polls`, `Responses`, and `Reports` Airtable tables.

### 5. Host it 24/7 (Railway, ~$5/mo, easiest)
1. Push this folder to a GitHub repo.
2. https://railway.app → **New Project → Deploy from GitHub repo**.
3. Railway reads `nixpacks.toml` automatically (Python + FFmpeg).
4. In Railway → **Variables** → add `DISCORD_TOKEN`, `POLL_CHANNEL_ID`,
   `AIRTABLE_BASE_ID`, and `AIRTABLE_TOKEN` from your `.env`.
5. **Important:** add a **Volume** mounted at `/data` and set variable
   `CLIP_DIR=/data/clips` — otherwise uploaded clips vanish on redeploys.

Free alternative: Oracle Cloud "Always Free" VM (free forever, but more
setup work — ask me if you want that path instead).

## Files
| File | Purpose |
|---|---|
| `bot.py` | Discord bot, name announcer, and Bangkok scheduler |
| `game_poll.py` | Persistent Discord poll UI and summaries |
| `airtable_store.py` | Airtable persistence for polls, responses, and reports |
| `requirements.in` | Direct pinned dependencies |
| `requirements.txt` | Fully locked Python dependency graph |
| `.env.example` | token template — copy to `.env` |
| `nixpacks.toml` | Railway build config |
| `clips/` | auto-created; one audio file per Discord user ID |
