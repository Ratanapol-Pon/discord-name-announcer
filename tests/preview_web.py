"""Local UI preview with simulated data only. Never accesses live services."""

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import web
from test_admin_web import event_input, fixture

from event_manager import utcnow


async def main():
    admin, manager, store, member, _ = fixture()
    guild = member.guild
    guild.name = "Teemo community · Demo"
    channel = SimpleNamespace(
        id=123,
        name="game-night",
        permissions_for=lambda _: SimpleNamespace(
            view_channel=True, send_messages=True
        ),
    )
    guild.text_channels = [channel]
    guild.voice_channels = [
        SimpleNamespace(
            name="The Lobby",
            members=[
                SimpleNamespace(display_name="Rz", bot=False),
                SimpleNamespace(display_name="Ahri", bot=False),
            ],
        ),
        SimpleNamespace(
            name="Chill room", members=[SimpleNamespace(display_name="Jett", bot=False)]
        ),
    ]
    for i, (name, hours) in enumerate(
        [("Rz", 12.4), ("Ahri", 8.7), ("Jett", 6.3), ("Kai", 4.8), ("Milo", 2.6)]
    ):
        await store._create(
            "Voice Sessions",
            {
                "Guild ID": "99",
                "User ID": str(i + 1),
                "Display Name": name,
                "Voice Channel Name": "The Lobby",
                "Joined At": (utcnow() - timedelta(hours=hours + 2)).isoformat(),
                "Left At": (utcnow() - timedelta(hours=2)).isoformat(),
            },
        )
        await store._create(
            "Solo Voice Sessions",
            {
                "Guild ID": "99",
                "User ID": str(i + 1),
                "Display Name": name,
                "Voice Channel Name": "Chill room",
                "Started Alone At": (
                    utcnow() - timedelta(hours=hours / 5 + 2)
                ).isoformat(),
                "Ended Alone At": (utcnow() - timedelta(hours=2)).isoformat(),
            },
        )
    for title, kind, status in [
        ("Friday night, one more game?", "poll", "open"),
        ("A weekend worth clearing your calendar for", "poll", "scheduled"),
        ("Welcome to our little corner of Discord", "announcement", "draft"),
    ]:
        event = await manager.create(dict(event_input(), title=title, kind=kind), 99, 1)
        event["status"] = status
        if status == "scheduled":
            event["publish_at"] = (utcnow() + timedelta(hours=3)).isoformat()
        await manager.save(event)
    admin.public_url = "http://127.0.0.1:8081"
    runner = web.AppRunner(admin.app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8081).start()
    print(admin.issue_link(1, 99), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
