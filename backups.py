"""Private on-volume snapshots. Restore only missing historical records."""

import asyncio
import gzip
import json
import logging
import os
import uuid
from datetime import timedelta
from pathlib import Path

from event_manager import timestamp, utcnow

LOGGER = logging.getLogger(__name__)

TABLE_KEYS = {
    "Polls": "Poll Key",
    "Responses": "Response Key",
    "Reports": "Poll Key",
    "Voice Sessions": "Session Key",
    "Solo Voice Sessions": "Solo Session Key",
}


class Backups:
    def __init__(self, community, directory):
        self.community, self.store = community, community.store
        self.directory = Path(directory).resolve()
        self.lock = asyncio.Lock()
        self.last_checked = None

    def folder(self, guild_id):
        if not str(guild_id).isdecimal():
            raise ValueError("Invalid server ID.")
        path = (self.directory / str(guild_id)).resolve()
        if path.parent != self.directory:
            raise ValueError("Invalid backup directory.")
        return path

    def path(self, guild_id, backup_id):
        if len(backup_id) != 32 or any(c not in "0123456789abcdef" for c in backup_id):
            raise ValueError("Invalid backup ID.")
        folder = self.folder(guild_id)
        path = (folder / (backup_id + ".json.gz")).resolve()
        if path.parent != folder:
            raise ValueError("Invalid backup file.")
        return path

    def read(self, guild_id, backup_id):
        try:
            with gzip.open(
                self.path(guild_id, backup_id), "rt", encoding="utf-8"
            ) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            raise ValueError("Backup is missing or unreadable.") from None
        if data.get("version") != 1 or data.get("guild_id") != str(guild_id):
            raise ValueError("Backup server or version mismatch.")
        return data

    def list(self, guild_id):
        folder = self.folder(guild_id)
        result = []
        if folder.exists():
            for path in folder.glob("*.json.gz"):
                backup_id = path.name.removesuffix(".json.gz")
                data = self.read(guild_id, backup_id)
                result.append(
                    {
                        "id": backup_id,
                        "created_at": data["created_at"],
                        "records": sum(len(rows) for rows in data["tables"].values()),
                    }
                )
        return sorted(result, key=lambda x: x["created_at"], reverse=True)

    async def collect(self, guild_id):
        formula = self.store._formula_equals("Guild ID", str(guild_id))
        tables = {}
        for table in (
            "Polls",
            "Voice Sessions",
            "Solo Voice Sessions",
            "Admin Events",
            "Event Votes",
            "Teemo State",
        ):
            tables[table] = await self.store.list_records(table, formula)
        tables["Responses"], tables["Reports"] = [], []
        ids = [r["id"] for r in tables["Polls"]]
        for offset in range(0, len(ids), 40):
            formula = (
                "OR("
                + ",".join(
                    self.store._formula_equals("Poll Key", p)
                    for p in ids[offset : offset + 40]
                )
                + ")"
            )
            for table in ("Responses", "Reports"):
                tables[table].extend(await self.store.list_records(table, formula))
        return {
            "version": 1,
            "guild_id": str(guild_id),
            "created_at": utcnow().isoformat(),
            "tables": tables,
        }

    def write(self, guild_id, data):
        folder = self.folder(guild_id)
        folder.mkdir(parents=True, exist_ok=True)
        backup_id = uuid.uuid4().hex
        path = self.path(guild_id, backup_id)
        temp = path.with_suffix(".tmp")
        with gzip.open(temp, "wt", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(temp, path)
        for old in self.list(guild_id)[14:]:
            self.path(guild_id, old["id"]).unlink()
        return {"id": backup_id, "created_at": data["created_at"]}

    async def create(self, guild_id):
        async with self.lock:
            data = await self.collect(guild_id)
            result = await asyncio.to_thread(self.write, guild_id, data)
            LOGGER.info(
                "Private backup saved: %s records",
                sum(len(rows) for rows in data["tables"].values()),
            )
            return result

    async def restore(self, guild_id, backup_id, preview=True):
        # One replica, serialized with snapshot creation and other restores.
        async with (
            self.lock,
            self.community.operation_lock,
            self.community.polls._delivery_lock,
            self.community.polls._poll_lifecycle_lock,
        ):
            data = await asyncio.to_thread(self.read, guild_id, backup_id)
            mapping, missing, restored = {}, 0, 0
            for table, unique_field in TABLE_KEYS.items():
                for row in data["tables"].get(table, []):
                    fields = dict(row["fields"])
                    if table in {"Responses", "Reports"}:
                        old_poll = fields["Poll Key"]
                        if old_poll not in mapping:
                            continue
                        fields["Poll Key"] = mapping[old_poll]
                        if table == "Responses":
                            fields["Response Key"] = (
                                f"{mapping[old_poll]}:{fields['User ID']}"
                            )
                    elif fields.get("Guild ID") != str(guild_id):
                        raise ValueError(
                            "Backup contains data from a different server."
                        )
                    existing = await self.store._find_one(
                        table, unique_field, fields[unique_field]
                    )
                    if existing:
                        if (
                            table == "Polls"
                            and existing["fields"].get("Status") == "closed"
                        ):
                            mapping[row["id"]] = existing["id"]
                        continue
                    missing += 1
                    if table == "Polls":
                        fields["Status"] = "closed"
                        fields["Closed At"] = (
                            fields.get("Closed At") or data["created_at"]
                        )
                        # Do not reconnect old buttons or trigger today's catch-up report.
                        fields["Message ID"] = ""
                    if table in {"Voice Sessions", "Solo Voice Sessions"}:
                        end_field = (
                            "Left At" if table == "Voice Sessions" else "Ended Alone At"
                        )
                        start_field = (
                            "Joined At"
                            if table == "Voice Sessions"
                            else "Started Alone At"
                        )
                        if not fields.get(end_field):
                            fields[end_field] = data["created_at"]
                            fields["Data Quality"] = (
                                "estimated: restored active session"
                            )
                            fields["Duration Seconds"] = max(
                                0,
                                int(
                                    (
                                        timestamp(data["created_at"])
                                        - timestamp(fields[start_field])
                                    ).total_seconds()
                                ),
                            )
                        fields["Active Key"], fields["Status"] = "", "closed"
                    if not preview:
                        new = await self.store._create(table, fields)
                        restored += 1
                    if table == "Polls":
                        mapping[row["id"]] = row["id"] if preview else new["id"]
            return {"missing": missing, "restored": restored, "preview": preview}

    async def tick(self):
        now = utcnow()
        if self.last_checked and now - self.last_checked < timedelta(hours=1):
            return
        self.last_checked = now
        for guild in self.community.bot.guilds:
            if not self.community.config(guild.id).get("backups", True):
                continue
            backups = await asyncio.to_thread(self.list, guild.id)
            if not backups or now - timestamp(backups[0]["created_at"]) >= timedelta(
                days=1
            ):
                await self.create(guild.id)
