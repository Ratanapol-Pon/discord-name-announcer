"""Delete old Airtable records so the base fits the free plan again.

Free-plan bases stop accepting new records past 1,000 rows, and Teemo's
polls, responses, reports and voice logs add up quickly. This script removes
rows older than a cutoff, newest records are never touched.

Dry-run by default (only counts what would go):

    python cleanup_airtable.py

Actually delete:

    python cleanup_airtable.py --apply

Options:
    --days N           Keep at least N days of history (default 45).
    --include-voice    Also clean Voice/Solo Voice Sessions. Off by default
                       because the December 25 yearly recap reads them.

Credentials come from AIRTABLE_TOKEN / AIRTABLE_BASE_ID in the environment
or the project .env file — the same settings the bot uses. Nothing is asked
for interactively and nothing is printed beyond counts, so the token stays
out of the console.

Note: deletes are API calls too, so this only works while the workspace is
inside its monthly API allowance (the limit resets on the 1st of the month).
Deleting rows by hand in the Airtable app does NOT use the API allowance and
works even while the bot is blocked.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from airtable_store import (
    POLLS_TABLE,
    REPORTS_TABLE,
    RESPONSES_TABLE,
    SOLO_VOICE_SESSIONS_TABLE,
    VOICE_SESSIONS_TABLE,
    AirtablePollStore,
    AirtableQuotaExceededError,
)

CORE_TABLES = [RESPONSES_TABLE, REPORTS_TABLE, POLLS_TABLE]
VOICE_TABLES = [VOICE_SESSIONS_TABLE, SOLO_VOICE_SESSIONS_TABLE]


async def clean_table(
    store: AirtablePollStore, table: str, cutoff: date, apply: bool
) -> tuple[int, int]:
    """Return (old record count, deleted count) for one table."""
    formula = f"IS_BEFORE(CREATED_TIME(), '{cutoff.isoformat()}')"
    records = await store.list_records(table, formula)
    record_ids = [record["id"] for record in records]
    deleted = await store.delete_records(table, record_ids) if apply else 0
    return len(record_ids), deleted


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--days",
        type=int,
        default=45,
        help="keep at least this many days of history (default: 45)",
    )
    parser.add_argument(
        "--include-voice",
        action="store_true",
        help="also clean voice session logs (the yearly recap needs them)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without it only counts are shown",
    )
    args = parser.parse_args()
    if args.days < 7:
        parser.error("--days must be 7 or more so recent history stays intact.")

    load_dotenv()
    token = os.environ.get("AIRTABLE_TOKEN", "").strip()
    base_id = os.environ.get("AIRTABLE_BASE_ID", "").strip()
    if not token or not base_id:
        print(
            "AIRTABLE_TOKEN and AIRTABLE_BASE_ID are required. Put them in the "
            "environment or in the project .env file (see .env.example)."
        )
        return 2

    cutoff = date.today() - timedelta(days=args.days)
    tables = list(CORE_TABLES)
    if args.include_voice:
        tables.extend(VOICE_TABLES)

    store = AirtablePollStore(token, base_id)
    mode = "DELETE" if args.apply else "DRY-RUN (nothing is deleted)"
    print(f"{mode} — records older than {cutoff.isoformat()} ({args.days} days)")
    try:
        total_old = total_deleted = 0
        for table in tables:
            old, deleted = await clean_table(store, table, cutoff, args.apply)
            total_old += old
            total_deleted += deleted
            if args.apply:
                print(f"  {table}: deleted {deleted} row(s)")
            else:
                print(f"  {table}: would delete {old} row(s)")
        if not args.apply:
            print(
                f"\n{total_old} old row(s) found. Re-run with --apply to delete them."
            )
        else:
            print(f"\nDone — deleted {total_deleted} row(s).")
    except AirtableQuotaExceededError:
        print(
            "\nAirtable's monthly API limit is used up, so cleanup cannot run "
            "right now. Either wait for the limit to reset on the 1st of the "
            "month, or delete old rows by hand in the Airtable app (manual "
            "deletes do not use the API allowance)."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
