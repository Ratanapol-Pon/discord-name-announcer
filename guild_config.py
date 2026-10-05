"""Per-guild Teemo settings with safe defaults for brand-new servers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GuildSettings:
    """Daily poll configuration for one Discord server.

    A server with no saved settings record is "not set up": the name
    announcer works, but no polls/reports are posted there. A record with
    at least one poll channel marks setup as complete.
    """

    guild_id: int
    poll_time: str = "11:59"
    report_time: str = "17:00"
    poll_channel_ids: list[int] = field(default_factory=list)
    announcement_channel_id: int | None = None
    poll_enabled: bool = True
    report_enabled: bool = True

    @property
    def setup_complete(self) -> bool:
        return bool(self.poll_channel_ids)

    def as_web_dict(self, timezone_name: str) -> dict:
        return {
            "poll_time": self.poll_time,
            "report_time": self.report_time,
            "poll_channel_ids": [str(x) for x in self.poll_channel_ids],
            "announcement_channel_id": str(
                self.announcement_channel_id
                or (self.poll_channel_ids[0] if self.poll_channel_ids else "")
            ),
            "poll_enabled": self.poll_enabled,
            "report_enabled": self.report_enabled,
            "timezone": timezone_name,
            "setup_complete": self.setup_complete,
        }


class GuildSettingsRegistry:
    """In-memory per-guild settings, loaded from and saved to Airtable.

    Implements the settings-provider protocol used by GamePollService:
    ``for_guild(guild_id)`` and ``configured()``.
    """

    def __init__(self, default_poll_time: str = "11:59", default_report_time: str = "17:00"):
        self.default_poll_time = default_poll_time
        self.default_report_time = default_report_time
        self._settings: dict[int, GuildSettings] = {}

    def for_guild(self, guild_id: int) -> GuildSettings | None:
        """Return the guild's settings, or None when it was never set up."""
        return self._settings.get(int(guild_id))

    def get_or_default(self, guild_id: int) -> GuildSettings:
        """Return saved settings or unsaved defaults (no poll channels)."""
        guild_id = int(guild_id)
        if guild_id not in self._settings:
            self._settings[guild_id] = GuildSettings(
                guild_id,
                poll_time=self.default_poll_time,
                report_time=self.default_report_time,
            )
        return self._settings[guild_id]

    def put(self, settings: GuildSettings) -> None:
        self._settings[int(settings.guild_id)] = settings

    def configured(self) -> list[GuildSettings]:
        """Settings of servers that completed setup (have poll channels)."""
        return [s for s in self._settings.values() if s.setup_complete]

    def known_guild_ids(self) -> list[int]:
        return list(self._settings)
