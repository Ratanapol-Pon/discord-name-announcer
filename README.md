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

It also runs a daily game poll (times and channels can be changed from
`/teemo_admin`):

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
- Daily summaries use a friendly **Tonight's lineup** card: a readable date,
  at-a-glance vote totals, and one member per line under Ready to play, Might
  join, and Sitting this one out. Yes votes include preferred times; No reasons
  appear beside the member's name. Long lists show an explicit remaining count
  with full answers available in the admin dashboard. Empty polls get a gentle
  no-replies message. This does not change the schedule, stored data, or yearly recap.
- A restart between 11:59 and 17:00 catches up a missing poll; a restart after
  17:00 retries a missing report for an existing poll.

## Community planning and admin tools

- **Yes → start time → games → available until → Save Yes + plan.** Choose up to
  four games or Any game. Flexible means 18:00 onwards; windows end the same
  evening. The summary suggests the game and earliest half-hour start with the
  largest overlap of at least 30 minutes and two members. This is a suggestion,
  not a booking. Legacy Yes votes without availability are not guessed.
- **Date ranges:** Today, this week (Monday onwards), this month, last 30 calendar
  days, this year, and custom ranges of up to 367 days. All boundaries are Bangkok
  midnight. The end date is inclusive; ongoing sessions stop at the current time.
  Exports follow the selected dates. Click a member under Voice activity for the
  full session, solo-period and answer history in that range.
- **Task history:** expected, attempted and completed timestamps, status, and
  Discord links. Logging of daily tasks starts with this upgrade. Existing event
  publications are also listed. Uncertain delivery is never automatically resent;
  Check delivery searches the latest 100 destination messages for its unique marker.
- **Summary controls:** Polls & posts → Responses → Preview summary. Update
  original edits the saved message; Resend creates a new copy and preserves the
  original. Both require confirmation and an idempotent operation ID. Saved votes
  are unchanged. A fresh preview starts a new operation. Previews are text-only,
  not an exact rendering of every Discord client.
- **Reminders:** opt in on the game plan; Teemo sends one DM 15 minutes before the
  suggested group start, only to opted-in members included in that overlap. Global
  opt-out and quiet hours take priority (default 23:00–09:00 Bangkok). No reminders
  are sent after the start time, and blocked DMs never fall back to public pings.
  Late recovery within the 15-minute window can send the reminder late.
- **Recurring templates:** select an existing event poll/news/announcement as the
  source, choose daily/weekly/fortnightly publication and relative closing/event
  times. Templates save paused; an admin must explicitly enable automatic posts.
  Pause or create a one-off draft at any time. Occurrences more than 15 minutes
  late are skipped, not posted in a burst. Uncertain delivery pauses the template.
- **Member privacy:** `/teemo_preferences` is available privately to all server
  members. It controls future voice/solo/attendance recording, public yearly voice
  totals, DM reminders and quiet hours. Equal quiet-hour start/end disables quiet
  hours. Existing records remain; this is not a deletion request. Poll answers
  remain stored. Paused members are omitted from the live dashboard names but still
  count as physically present when determining whether another member is alone.
  Admins can hide No reasons in future daily summaries under Privacy & backups;
  the existing default (public reasons) is preserved. Existing posts are unchanged.
- **Data quality:** new live sessions are marked recorded; sessions spanning bot
  downtime or first detected on reconnect are estimated. Older rows with no
  quality metadata show legacy / unknown. Voice time is presence, not speech.
- **Backups:** daily compressed JSON snapshots of the guild's poll, response,
  report, voice, solo, event, event-vote and community-state records. Snapshots are
  best-effort, not transactional. The newest 14 per guild are retained on the
  existing volume; admin-only JSON download and manual backup are provided.
  Credentials and audio clips are excluded. This is not an off-site disaster
  recovery service—download independent copies yourself. No additional paid backup
  service is configured. More data means more Airtable records/API requests and
  volume usage; existing provider limits still apply.
- **Restore:** preview the missing-record count, then confirm. Only missing
  historical polls, responses, reports and voice/solo sessions are added; existing
  rows are not overwritten. Poll IDs and dependent response/report keys are remapped.
  Restored polls stay closed without reconnecting old buttons. Active sessions are
  closed at backup time and marked estimated. Current open polls are excluded from
  restoring answers. Settings, plans, preferences, templates and events are included
  for reference in downloads but are **not automatically restored or reactivated**.
  Restore can be safely rerun after interruption; it skips keys already present.

Schema additions required before deploying this upgrade:

- `Teemo State`: `Key` (primary single-line text), `Guild ID`, `Kind`, `Updated At`
  (single-line text), `Data` (long text JSON).
- `Voice Sessions` and `Solo Voice Sessions`: `Data Quality` (single-line text).
- Optional `BACKUP_DIR`: defaults to a `backups` sibling of `CLIP_DIR`. With
  `CLIP_DIR=/data/clips`, snapshots use `/data/backups` on the existing volume.

Run offline checks with `python -m unittest discover -s tests` and
`node --test tests/*.test.cjs`. Keep one running bot replica.

## Name announcements

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
  - `/teemo_web` or `/teemo_admin` — get a private, one-use sign-in link to the
    web console (server administrators only)

News and announcement posts use an embed in the configured post channel.
Discord mentions are disabled, so text such as `@everyone` will not ping people.

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
- `Bot Settings`: `Setting Key` (primary text), `Poll Time`, `Report Time`,
  `Poll Channel IDs`, `Announcement Channel ID`, `Updated At`, `Updated By`,
  `Poll Enabled`, and `Report Enabled` (text: `yes` / `no`; missing means enabled).
- `Admin Events` and `Event Votes`: each has `Key` (primary text), `Guild ID`,
  `Title`, `Status`, `Updated At` (text), and `Data` (long text containing JSON).

The `Bot Settings` record with key `global` is created or updated by the admin
panel. Changes apply immediately and are restored from Airtable after a restart.

## Web console

### Yearly December 25 announcement

Every December 25 at **17:00 Asia/Bangkok**, Teemo posts a yearly game-poll
summary in the configured announcement channel (or the first daily poll channel
if none is configured). It covers **January 1–December 24 of the current year**:
daily polls posted, distinct respondents, Yes/Maybe/No totals, and each person's
total time in voice chat. The public post shows up to ten voice-time highlights,
total member hours and session counts; an attached CSV includes every member
with recorded voice time. It does not expose private No reasons, solo breakdowns,
individual join/leave timestamps, or Yes-voter attendance comparisons. One-time
event polls are excluded. Poll counts use the latest saved answer per person per
poll. The **daily** summary still excludes attendance and voice statistics.

Voice sessions are clipped to the reporting period in the bot's timezone. Total
member hours include solo presence and count each member separately, not room
wall-clock time. Tracking began when the bot was enabled, so earlier activity is
not available; sessions spanning bot downtime can be approximate. The bot measures
presence, not speaking, and members with no recorded voice time are not listed.

The announcement and aggregate totals are saved in Airtable's existing
`Admin Events` table and appear in the console's post history. Its persistent
year/server key prevents routine restart, channel-change, or retry duplicates. Uncertain delivery
requires **Check delivery** in the console instead of automatic resending.
If Teemo is offline at the scheduled time, it catches up through December 31;
it does not send historical years' summaries in January. The bot must be hosted
and running for delivery. No new Airtable tables are needed.
The announcement channel must grant Teemo **Attach Files** permission for the CSV.

Set `YEARLY_SUMMARY_ENABLED=false` to disable this independent yearly task, or
change `YEARLY_SUMMARY_TIME` (24-hour format). These environment changes require
a restart; the web daily schedule controls do not change the yearly schedule.

### Opening the console

Run `/teemo_web` in your Discord server and open the private link within five
minutes. It works once; your browser session lasts eight hours or until the bot
restarts. Do not share sign-in links. Administrator permission is checked on
every request. `/teemo_admin` now opens the same web console.

- **Overview:** live voice rooms, 30-day poll responses, voice and solo hours,
  member activity, scheduled posts, and event-service warnings.
- **Polls & posts:** daily response details and one-time event results. Create,
  edit, duplicate, cancel drafts, close voting early, or check uncertain delivery.
- **Create something:** one-time polls with 2–10 custom choices, an optional
  required No reason, a voting deadline, and an optional event start time.
  Compose news or announcements using your own text (no AI news generation).
  Save a draft, then explicitly publish now or approve its scheduled time.
- **Voice activity:** individual sessions, solo periods, member totals and CSV
  exports. Solo time is included in total voice time, not added twice.
- **Daily schedule:** change daily times/channels, pause either task, and run
  today's poll or summary immediately. Saving past times does not send a post.

The UI uses Bangkok time. Attendance remains private; Discord's daily summary
contains votes, play times and reasons only. Voice tracking measures presence,
not speaking. Bot downtime can make session end times approximate. The dashboard
defaults to 30 days and shows up to 250 recent sessions; the date filter changes
the period and CSV includes all sessions in it. Member details include all of
that member's sessions in the selected period. Historical Airtable records are retained. History refreshes every minute
while viewing; forms are not refreshed underneath your edits.

Use **one running replica**. Event schedules and votes survive restarts in
Airtable. A poll whose entire publishing window passed during downtime is
cancelled. Uncertain delivery is marked for review instead of automatically
resending; use **Check delivery** to inspect the latest 100 destination messages.
If an Airtable write failed after Discord delivery, inspect Discord before
creating a replacement. Editing Airtable JSON directly is not a supported admin
workflow; make changes in the console.

The web server runs in the bot process on `PORT` (default 8080). In Railway,
open the service's **Settings → Networking → Generate Domain**, targeting port
8080. Teemo uses `RAILWAY_PUBLIC_DOMAIN` automatically. For another host, set
`ADMIN_PUBLIC_URL=https://your-domain` and proxy HTTPS to the web port. Keep
Discord and Airtable credentials server-side. The public page reveals no server
data; APIs require an administrator session, with same-origin/CSRF checks for
writes and HttpOnly cookies (Secure over HTTPS).

For local development, use `ADMIN_PUBLIC_URL=http://127.0.0.1:8080`. To preview
with simulated data only, run `python tests/preview_web.py` and open its printed
one-use link. The preview cannot access Discord or Airtable. Run offline tests
with `python -m unittest discover -s tests`.

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
| `admin_web.py` / `web/` | Protected web API and responsive admin console |
| `event_manager.py` | Persistent one-time polls, posts, voting, and scheduling |
| `airtable_store.py` | Airtable persistence for polls, responses, and reports |
| `requirements.in` | Direct pinned dependencies |
| `requirements.txt` | Fully locked Python dependency graph |
| `.env.example` | token template — copy to `.env` |
| `nixpacks.toml` | Railway build config |
| `clips/` | auto-created; one audio file per Discord user ID |
