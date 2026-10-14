"""Delete old Airtable records so the base fits the free plan again.

Free-plan bases stop accepting new records past 1,000 rows, and Teemo's
polls, responses, reports and voice logs add up quickly. This script removes
rows older than a cutoff; newest records are never touched.

Dry-run by default (only counts what would go):

    python cleanup_airtable.py

Actually delete:

    python cleanup_airtable.py --apply

Options:
    --days N           Keep at least N days of history (default 45).
    --include-voice    Also clean Voice/Solo Voice Sessions. Off by default
                       because the December 25 yearly recap reads them.
    --guild-id ID      Only clean one Discord server's records (default:
                       the whole base).

Credentials come from AIRTABLE_TOKEN / AIRTABLE_BASE_ID in the environment
or the project .env file — the same settings the bot uses. Nothing is asked
for interactively, so the token never appears in the console. Server admins
can also run a guild-scoped cleanup from the web console (no token needed).

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

from dotenv import load_dotenv

from airtable_store import AirtablePollStore, AirtableQuotaExceededError
from maintenance import DEFAULT_KEEP_DAYS, MIN_KEEP_DAYS, clean_old_records


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_KEEP_DAYS,
        help=f"keep at least this many days of history (default: {DEFAULT_KEEP_DAYS})",
    )
    parser.add_argument(
        "--include-voice",
        action="store_true",
        help="also clean voice session logs (the yearly recap needs them)",
    )
    parser.add_argument(
        "--guild-id",
        type=int,
        default=None,
        help="only clean one Discord server's records (default: whole base)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without it only counts are shown",
    )
    args = parser.parse_args()
    if args.days < MIN_KEEP_DAYS:
        parser.error(f"--days must be {MIN_KEEP_DAYS} or more so recent history stays intact.")

    load_dotenv()
    token = os.environ.get("AIRTABLE_TOKEN", "").strip()
    base_id = os.environ.get("AIRTABLE_BASE_ID", "").strip()
    if not token or not base_id:
        print(
            "AIRTABLE_TOKEN and AIRTABLE_BASE_ID are required. Put them in the "
            "environment or in the project .env file (see .env.example)."
        )
        return 2

    store = AirtablePollStore(token, base_id)
    mode = "DELETE" if args.apply else "DRY-RUN (nothing is deleted)"
    scope = f"server {args.guild_id}" if args.guild_id else "whole base"
    print(f"{mode} — {scope}, keeping the last {args.days} days")
    try:
        results = await clean_old_records(
            store,
            keep_days=args.days,
            include_voice=args.include_voice,
            guild_id=args.guild_id,
            apply=args.apply,
        )
    except AirtableQuotaExceededError:
        print(
            "\nAirtable's monthly API limit is used up, so cleanup cannot run "
            "right now. Either wait for the limit to reset on the 1st of the "
            "month, or delete old rows by hand in the Airtable app (manual "
            "deletes do not use the API allowance)."
        )
        return 1
    for table, counts in results.items():
        if args.apply:
            print(f"  {table}: deleted {counts['deleted']} row(s)")
        else:
            print(f"  {table}: would delete {counts['old']} row(s)")
    total = sum(c["deleted" if args.apply else "old"] for c in results.values())
    if not args.apply:
        print(f"\n{total} old row(s) found. Re-run with --apply to delete them.")
    else:
        print(f"\nDone — deleted {total} row(s).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
