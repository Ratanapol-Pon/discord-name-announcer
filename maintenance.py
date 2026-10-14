"""Shared logic for deleting old Airtable records (script + web console)."""

from __future__ import annotations

from datetime import date, timedelta

from airtable_store import (
    POLLS_TABLE,
    REPORTS_TABLE,
    RESPONSES_TABLE,
    SOLO_VOICE_SESSIONS_TABLE,
    VOICE_SESSIONS_TABLE,
    AirtablePollStore,
)

# Newest-first cleanup order: responses/reports/polls are safe to trim;
# voice tables feed the December 25 yearly recap, so they are opt-in only.
CORE_TABLES = [RESPONSES_TABLE, REPORTS_TABLE, POLLS_TABLE]
VOICE_TABLES = [VOICE_SESSIONS_TABLE, SOLO_VOICE_SESSIONS_TABLE]

MIN_KEEP_DAYS = 7
DEFAULT_KEEP_DAYS = 45


def cleanup_cutoff(keep_days: int) -> date:
    """Records older than this date are eligible for deletion."""
    keep_days = int(keep_days)
    if keep_days < MIN_KEEP_DAYS:
        raise ValueError(f"Keep at least {MIN_KEEP_DAYS} days of history.")
    return date.today() - timedelta(days=keep_days)


def cleanup_tables(include_voice: bool) -> list[str]:
    tables = list(CORE_TABLES)
    if include_voice:
        tables.extend(VOICE_TABLES)
    return tables


def cleanup_formula(cutoff: date, guild_id: int | None) -> str:
    formula = f"IS_BEFORE(CREATED_TIME(), '{cutoff.isoformat()}')"
    if guild_id is not None:
        formula = f"AND({formula}, {{Guild ID}}='{int(guild_id)}')"
    return formula


async def clean_old_records(
    store: AirtablePollStore,
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    include_voice: bool = False,
    guild_id: int | None = None,
    apply: bool = False,
) -> dict[str, dict[str, int]]:
    """Count (dry-run) or delete records older than the cutoff.

    Returns ``{table: {"old": n, "deleted": n}}``. ``guild_id=None`` cleans
    the whole base (script use); the web console always scopes to the
    signed-in admin's own server.
    """
    cutoff = cleanup_cutoff(keep_days)
    formula = cleanup_formula(cutoff, guild_id)
    results: dict[str, dict[str, int]] = {}
    for table in cleanup_tables(include_voice):
        records = await store.list_records(table, formula)
        record_ids = [record["id"] for record in records]
        deleted = await store.delete_records(table, record_ids) if apply else 0
        results[table] = {"old": len(record_ids), "deleted": deleted}
    return results
