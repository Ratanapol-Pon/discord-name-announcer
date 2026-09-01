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
MIN_REQUEST_INTERVAL_SECONDS = 0.22  # Airtable limit: 5 requests/second/base.


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
    ) -> None:
        if choice not in {"yes", "maybe", "no"}:
            raise ValueError(f"Unsupported poll choice: {choice}")
        cleaned_reason = reason.strip() if reason else None
        if choice == "no" and not cleaned_reason:
            raise ValueError("A reason is required when choosing No.")
        if choice != "no":
            cleaned_reason = None

        poll = await self.get_poll_by_message(message_id)
        if not poll:
            raise PollNotFoundError("This poll is not linked to Airtable.")
        if poll["status"] != "open":
            raise PollClosedError("This poll is already closed.")

        response_key = f"{poll['id']}:{user_id}"
        fields = {
            "Response Key": response_key,
            "Poll Key": poll["id"],
            "User ID": str(user_id),
            "Display Name": display_name[:100],
            "Choice": choice,
            "Reason": cleaned_reason[:500] if cleaned_reason else "",
            "Responded At": datetime.now(timezone.utc).isoformat(),
        }
        existing = await self._find_one(RESPONSES_TABLE, "Response Key", response_key)
        if existing:
            await self._update(RESPONSES_TABLE, existing["id"], fields)
        else:
            await self._create(RESPONSES_TABLE, fields)

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
                "responded_at": record["fields"].get("Responded At"),
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
