"""Async Airtable persistence for the daily Discord game poll."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import quote

import aiohttp

AIRTABLE_API_ROOT = "https://api.airtable.com/v0"
POLLS_TABLE = "Polls"
RESPONSES_TABLE = "Responses"
REPORTS_TABLE = "Reports"
VOICE_SESSIONS_TABLE = "Voice Sessions"
MIN_REQUEST_INTERVAL_SECONDS = 0.22  # Airtable limit: 5 requests/second/base.
PLAY_TIME_OPTIONS = (
    "Flexible",
    "18:00",
    "18:30",
    "19:00",
    "19:30",
    "20:00",
    "20:30",
    "21:00",
    "21:30",
    "22:00",
    "22:30",
    "23:00",
    "23:30",
)


class PollClosedError(RuntimeError):
    """Raised when somebody tries to respond to a closed poll."""


class PollNotFoundError(RuntimeError):
    """Raised when a Discord message is not linked to a stored poll."""


class AirtablePollStore:
    """Persist polls using a PAT restricted to one Airtable base."""

    def __init__(self, token: str, base_id: str) -> None:
        self._token = token
        self._base_id = base_id
        self._request_lock = asyncio.Lock()
        self._voice_session_lock = asyncio.Lock()
        self._last_request_at = 0.0

    def _table_url(self, table: str, record_id: str | None = None) -> str:
        url = f"{AIRTABLE_API_ROOT}/{quote(self._base_id, safe='')}/{quote(table, safe='')}"
        if record_id:
            url += f"/{quote(record_id, safe='')}"
        return url

    async def _request(
        self,
        method: str,
        table: str,
        *,
        record_id: str | None = None,
        params: dict[str, str | int] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        async with self._request_lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
                await asyncio.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)

            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for attempt in range(2):
                    async with session.request(
                        method,
                        self._table_url(table, record_id),
                        headers=headers,
                        params=params,
                        json=payload,
                    ) as response:
                        self._last_request_at = time.monotonic()
                        if response.status == 429 and attempt == 0:
                            retry_after = min(
                                float(response.headers.get("Retry-After", 30)), 30
                            )
                            await asyncio.sleep(retry_after)
                            continue
                        body = await response.text()
                        if response.status < 200 or response.status >= 300:
                            try:
                                detail = json.loads(body).get("error", body)
                            except json.JSONDecodeError:
                                detail = body
                            raise RuntimeError(
                                f"Airtable request failed ({response.status}): {detail}"
                            )
                        return json.loads(body) if body else {}

        raise RuntimeError("Airtable rate-limit retry failed.")

    @staticmethod
    def _formula_equals(field: str, value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"{{{field}}}='{escaped}'"

    async def _find_one(
        self, table: str, field: str, value: str
    ) -> dict[str, Any] | None:
        result = await self._request(
            "GET",
            table,
            params={
                "maxRecords": 1,
                "filterByFormula": self._formula_equals(field, value),
            },
        )
        records = result.get("records") or []
        return records[0] if records else None

    async def _create(self, table: str, fields: dict[str, Any]) -> dict[str, Any]:
        result = await self._request(
            "POST", table, payload={"records": [{"fields": fields}], "typecast": True}
        )
        return result["records"][0]

    async def _update(
        self, table: str, record_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            table,
            record_id=record_id,
            payload={"fields": fields, "typecast": True},
        )

    async def healthcheck(self) -> None:
        await self._request("GET", POLLS_TABLE, params={"maxRecords": 1})
        await self._request("GET", VOICE_SESSIONS_TABLE, params={"maxRecords": 1})

    @staticmethod
    def _voice_active_key(guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"

    @staticmethod
    def _utc_timestamp(value: datetime | None = None) -> datetime:
        value = value or datetime.now(timezone.utc)
        if value.tzinfo is None:
            raise ValueError("Voice-session timestamps must include a timezone.")
        return value.astimezone(timezone.utc)

    async def _close_voice_session_record(
        self, record: dict[str, Any], left_at: datetime
    ) -> int:
        fields = record.get("fields") or {}
        joined_text = fields.get("Joined At")
        try:
            joined_at = datetime.fromisoformat(str(joined_text).replace("Z", "+00:00"))
            if joined_at.tzinfo is None:
                joined_at = joined_at.replace(tzinfo=timezone.utc)
            duration_seconds = max(
                0, int((left_at - joined_at.astimezone(timezone.utc)).total_seconds())
            )
        except (TypeError, ValueError):
            duration_seconds = 0

        await self._update(
            VOICE_SESSIONS_TABLE,
            record["id"],
            {
                "Active Key": "",
                "Left At": left_at.isoformat(),
                "Duration Seconds": duration_seconds,
                "Status": "closed",
            },
        )
        return duration_seconds

    async def start_voice_session(
        self,
        guild_id: int,
        user_id: int,
        display_name: str,
        voice_channel_id: int,
        voice_channel_name: str,
        session_date: date,
        *,
        joined_at: datetime | None = None,
    ) -> str:
        """Start a user's channel session, closing a stale/moved session first."""
        joined_at = self._utc_timestamp(joined_at)
        active_key = self._voice_active_key(guild_id, user_id)
        async with self._voice_session_lock:
            existing = await self._find_one(
                VOICE_SESSIONS_TABLE, "Active Key", active_key
            )
            if existing:
                existing_fields = existing.get("fields") or {}
                if existing_fields.get("Voice Channel ID") == str(voice_channel_id):
                    return existing["id"]
                await self._close_voice_session_record(existing, joined_at)

            record = await self._create(
                VOICE_SESSIONS_TABLE,
                {
                    "Session Key": (
                        f"{active_key}:{joined_at.isoformat(timespec='microseconds')}"
                    ),
                    "Active Key": active_key,
                    "Guild ID": str(guild_id),
                    "User ID": str(user_id),
                    "Display Name": display_name[:100],
                    "Voice Channel ID": str(voice_channel_id),
                    "Voice Channel Name": voice_channel_name[:100],
                    "Joined At": joined_at.isoformat(),
                    "Left At": "",
                    "Duration Seconds": 0,
                    "Session Date": session_date.isoformat(),
                    "Status": "active",
                },
            )
            return record["id"]

    async def stop_voice_session(
        self,
        guild_id: int,
        user_id: int,
        *,
        left_at: datetime | None = None,
    ) -> int | None:
        """Close a user's active voice session and return its duration."""
        left_at = self._utc_timestamp(left_at)
        active_key = self._voice_active_key(guild_id, user_id)
        async with self._voice_session_lock:
            existing = await self._find_one(
                VOICE_SESSIONS_TABLE, "Active Key", active_key
            )
            if not existing:
                return None
            return await self._close_voice_session_record(existing, left_at)

    async def _get_active_voice_sessions(self) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {
            "pageSize": 100,
            "filterByFormula": self._formula_equals("Status", "active"),
        }
        records: list[dict[str, Any]] = []
        while True:
            result = await self._request("GET", VOICE_SESSIONS_TABLE, params=params)
            records.extend(result.get("records") or [])
            offset = result.get("offset")
            if not offset:
                return records
            params["offset"] = offset

    async def reconcile_voice_sessions(
        self,
        connected_members: list[dict[str, Any]],
        *,
        reconciled_at: datetime | None = None,
    ) -> tuple[int, int]:
        """Make stored active sessions match Discord after a bot restart."""
        reconciled_at = self._utc_timestamp(reconciled_at)
        current = {
            self._voice_active_key(item["guild_id"], item["user_id"]): item
            for item in connected_members
        }
        started = 0
        closed = 0
        retained: set[str] = set()

        async with self._voice_session_lock:
            for record in await self._get_active_voice_sessions():
                fields = record.get("fields") or {}
                active_key = fields.get("Active Key")
                member = current.get(active_key)
                same_channel = member and fields.get("Voice Channel ID") == str(
                    member["voice_channel_id"]
                )
                if same_channel and active_key not in retained:
                    retained.add(active_key)
                    continue
                await self._close_voice_session_record(record, reconciled_at)
                closed += 1

            for active_key, member in current.items():
                if active_key in retained:
                    continue
                record = await self._create(
                    VOICE_SESSIONS_TABLE,
                    {
                        "Session Key": (
                            f"{active_key}:"
                            f"{reconciled_at.isoformat(timespec='microseconds')}"
                        ),
                        "Active Key": active_key,
                        "Guild ID": str(member["guild_id"]),
                        "User ID": str(member["user_id"]),
                        "Display Name": str(member["display_name"])[:100],
                        "Voice Channel ID": str(member["voice_channel_id"]),
                        "Voice Channel Name": str(member["voice_channel_name"])[:100],
                        "Joined At": reconciled_at.isoformat(),
                        "Left At": "",
                        "Duration Seconds": 0,
                        "Session Date": str(member["session_date"]),
                        "Status": "active",
                    },
                )
                if record:
                    started += 1

        return started, closed

    @staticmethod
    def _poll(record: dict[str, Any]) -> dict[str, Any]:
        fields = record.get("fields") or {}
        return {
            "id": record["id"],
            "guild_id": int(fields["Guild ID"]),
            "channel_id": int(fields["Channel ID"]),
            "poll_date": fields["Poll Date"],
            "message_id": int(fields["Message ID"])
            if fields.get("Message ID")
            else None,
            "question": fields.get("Question", ""),
            "status": fields.get("Status", "open"),
            "closed_at": fields.get("Closed At"),
        }

    async def get_poll(self, channel_id: int, poll_date: date) -> dict[str, Any] | None:
        key = f"{channel_id}:{poll_date.isoformat()}"
        record = await self._find_one(POLLS_TABLE, "Poll Key", key)
        return self._poll(record) if record else None

    async def create_poll(
        self, guild_id: int, channel_id: int, poll_date: date
    ) -> dict[str, Any]:
        existing = await self.get_poll(channel_id, poll_date)
        if existing:
            return existing
        record = await self._create(
            POLLS_TABLE,
            {
                "Poll Key": f"{channel_id}:{poll_date.isoformat()}",
                "Guild ID": str(guild_id),
                "Channel ID": str(channel_id),
                "Poll Date": poll_date.isoformat(),
                "Question": "Does anyone want to play any game tonight?",
                "Status": "open",
            },
        )
        return self._poll(record)

    async def set_poll_message(self, poll_id: str, message_id: int) -> None:
        await self._update(POLLS_TABLE, poll_id, {"Message ID": str(message_id)})

    async def get_poll_by_message(self, message_id: int) -> dict[str, Any] | None:
        record = await self._find_one(POLLS_TABLE, "Message ID", str(message_id))
        return self._poll(record) if record else None

    async def record_response(
        self,
        message_id: int,
        user_id: int,
        display_name: str,
        choice: str,
        reason: str | None,
        play_time: str | None = None,
    ) -> str:
        if choice not in {"yes", "maybe", "no"}:
            raise ValueError(f"Unsupported poll choice: {choice}")
        cleaned_reason = reason.strip() if reason else None
        if choice == "no" and not cleaned_reason:
            raise ValueError("A reason is required when choosing No.")
        if choice != "no":
            cleaned_reason = None
        if choice == "yes" and play_time not in PLAY_TIME_OPTIONS:
            raise ValueError("Please select a valid play time when choosing Yes.")
        if choice != "yes":
            play_time = None

        poll = await self.get_poll_by_message(message_id)
        if not poll:
            raise PollNotFoundError("This poll is not linked to Airtable.")
        if poll["status"] != "open":
            raise PollClosedError("This poll is already closed.")

        response_key = f"{poll['id']}:{user_id}"
        existing = await self._find_one(RESPONSES_TABLE, "Response Key", response_key)
        existing_fields = (existing.get("fields") or {}) if existing else {}
        fields = {
            "Response Key": response_key,
            "Poll Key": poll["id"],
            "User ID": str(user_id),
            "Display Name": display_name[:100],
            "Choice": choice,
            "Reason": cleaned_reason[:500] if cleaned_reason else "",
            "Play Time": play_time or "",
            "Responded At": datetime.now(timezone.utc).isoformat(),
        }
        if choice != "yes" or existing_fields.get("Choice") != "yes":
            fields.update(
                {
                    "Joined Voice Chat": "no",
                    "Joined At": "",
                    "Voice Channel ID": "",
                    "Voice Channel Name": "",
                }
            )
        if existing:
            await self._update(RESPONSES_TABLE, existing["id"], fields)
        else:
            await self._create(RESPONSES_TABLE, fields)
        return poll["id"]

    async def mark_yes_voice_join(
        self,
        poll_id: str,
        user_id: int,
        voice_channel_id: int,
        voice_channel_name: str,
    ) -> bool:
        """Record the first voice join for a user whose current vote is Yes."""
        response_key = f"{poll_id}:{user_id}"
        record = await self._find_one(RESPONSES_TABLE, "Response Key", response_key)
        if not record:
            return False

        fields = record.get("fields") or {}
        if fields.get("Choice") != "yes" or fields.get("Joined Voice Chat") == "yes":
            return False

        await self._update(
            RESPONSES_TABLE,
            record["id"],
            {
                "Joined Voice Chat": "yes",
                "Joined At": datetime.now(timezone.utc).isoformat(),
                "Voice Channel ID": str(voice_channel_id),
                "Voice Channel Name": voice_channel_name[:100],
            },
        )
        return True

    async def get_responses(self, poll_id: str) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {
            "pageSize": 100,
            "filterByFormula": self._formula_equals("Poll Key", poll_id),
            "sort[0][field]": "Responded At",
            "sort[0][direction]": "asc",
        }
        records: list[dict[str, Any]] = []
        while True:
            result = await self._request("GET", RESPONSES_TABLE, params=params)
            records.extend(result.get("records") or [])
            offset = result.get("offset")
            if not offset:
                break
            params["offset"] = offset

        return [
            {
                "user_id": int(record["fields"]["User ID"]),
                "display_name": record["fields"]["Display Name"],
                "choice": record["fields"]["Choice"],
                "reason": record["fields"].get("Reason") or None,
                "play_time": record["fields"].get("Play Time") or None,
                "responded_at": record["fields"].get("Responded At"),
                "joined_voice_chat": record["fields"].get("Joined Voice Chat") == "yes",
                "joined_at": record["fields"].get("Joined At"),
                "voice_channel_id": int(record["fields"]["Voice Channel ID"])
                if record["fields"].get("Voice Channel ID")
                else None,
                "voice_channel_name": record["fields"].get("Voice Channel Name"),
            }
            for record in records
        ]

    async def close_poll(self, poll_id: str) -> None:
        await self._update(
            POLLS_TABLE,
            poll_id,
            {
                "Status": "closed",
                "Closed At": datetime.now(timezone.utc).isoformat(),
            },
        )

    @staticmethod
    def _report(record: dict[str, Any]) -> dict[str, Any]:
        fields = record.get("fields") or {}
        return {
            "poll_id": fields["Poll Key"],
            "message_id": int(fields["Message ID"])
            if fields.get("Message ID")
            else None,
            "yes_count": int(fields.get("Yes Count", 0)),
            "maybe_count": int(fields.get("Maybe Count", 0)),
            "no_count": int(fields.get("No Count", 0)),
            "no_reasons": json.loads(fields.get("No Reasons JSON", "[]")),
            "responses": json.loads(fields.get("Responses JSON", "[]")),
            "generated_at": fields.get("Generated At"),
        }

    async def get_report(self, poll_id: str) -> dict[str, Any] | None:
        record = await self._find_one(REPORTS_TABLE, "Poll Key", poll_id)
        return self._report(record) if record else None

    async def save_report(
        self, poll_id: str, responses: list[dict[str, Any]]
    ) -> dict[str, Any]:
        counts = Counter(response["choice"] for response in responses)
        no_reasons = [
            {
                "user_id": response["user_id"],
                "display_name": response["display_name"],
                "reason": response["reason"],
            }
            for response in responses
            if response["choice"] == "no"
        ]
        fields = {
            "Poll Key": poll_id,
            "Yes Count": counts["yes"],
            "Maybe Count": counts["maybe"],
            "No Count": counts["no"],
            "No Reasons JSON": json.dumps(no_reasons, ensure_ascii=False),
            "Responses JSON": json.dumps(responses, ensure_ascii=False),
            "Generated At": datetime.now(timezone.utc).isoformat(),
        }
        existing = await self._find_one(REPORTS_TABLE, "Poll Key", poll_id)
        record = (
            await self._update(REPORTS_TABLE, existing["id"], fields)
            if existing
            else await self._create(REPORTS_TABLE, fields)
        )
        return self._report(record)

    async def set_report_message(self, poll_id: str, message_id: int) -> None:
        record = await self._find_one(REPORTS_TABLE, "Poll Key", poll_id)
        if not record:
            raise RuntimeError("The Airtable report record is missing.")
        await self._update(REPORTS_TABLE, record["id"], {"Message ID": str(message_id)})
