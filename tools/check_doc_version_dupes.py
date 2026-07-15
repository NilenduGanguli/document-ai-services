#!/usr/bin/env python3
"""Pre-deploy check for the ``doc_version(client_id, doc_id, version_no)`` uniqueness backstop.

``CREATE UNIQUE INDEX`` cannot be created over duplicate rows, so this must be run — and pass —
before rolling out migration 011 (``011_doc_version_unique.sql``) to any long-lived environment.
A fresh compose database is never affected (it has no concurrent-ingest history), but any
environment that ran the pre-hardening pipeline (no advisory lock around ``create_version``) may
carry duplicates from the race the index closes.

Usage:
    python tools/check_doc_version_dupes.py                  # report only, exit 1 if dupes found
    python tools/check_doc_version_dupes.py --repair          # also repair (see below)
    python tools/check_doc_version_dupes.py --repair --dry-run

Repair strategy: for each duplicate ``(client_id, doc_id, version_no)`` group, keep the row with
the earliest ``created_at`` (the "real" version at that slot) and renumber every later duplicate
to the next free ``version_no`` for that document, preserving relative order by ``created_at``.
``is_current`` is left untouched by the renumber (at most one row in a group was current, since
``doc_version_one_current`` already enforces that per document) — only ``version_no`` changes, so
no other table (which references ``doc_version`` by id, not by number) needs updating.

Exits 0 if no duplicates exist (or all were repaired), 1 otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import asyncpg

from di.config import get_settings


async def _connect() -> asyncpg.Connection:
    """Connect with owner/superuser-class credentials: under RLS this check must see EVERY
    tenant's rows (it never binds ``app.current_client_id``), which the least-privilege runtime
    role cannot do. Falls back to ``pg_user`` for pre-RLS-rollout environments."""
    s = get_settings()
    user = getattr(s, "pg_migration_user", "") or s.pg_user
    password = getattr(s, "pg_migration_password", "") or s.pg_password or None
    return await asyncpg.connect(
        host=s.pg_host, port=s.pg_port, user=user, password=password, database=s.pg_database,
    )


async def _find_dupes(conn: asyncpg.Connection, schema: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        f'SELECT client_id, doc_id, version_no, count(*) AS n, '
        f'       array_agg(id ORDER BY created_at) AS ids '
        f'FROM "{schema}".doc_version '
        f"GROUP BY client_id, doc_id, version_no HAVING count(*) > 1 "
        f"ORDER BY client_id, doc_id, version_no"
    )


async def _repair_group(conn: asyncpg.Connection, schema: str, client_id: str, doc_id: str,
                        version_no: int, ids: list[str], *, dry_run: bool) -> None:
    """Keep ``ids[0]`` (earliest) at ``version_no``; renumber the rest to free slots above the
    document's current max, preserving their relative (created_at) order."""
    keep, renumber = ids[0], ids[1:]
    max_no = await conn.fetchval(
        f'SELECT max(version_no) FROM "{schema}".doc_version '
        f"WHERE client_id = $1 AND doc_id = $2",
        client_id, doc_id,
    )
    next_no = int(max_no) + 1
    for row_id in renumber:
        print(f"  {'[dry-run] would renumber' if dry_run else 'renumbering'} "
              f"doc_version {row_id} (client={client_id} doc={doc_id}) "
              f"{version_no} -> {next_no}")
        if not dry_run:
            await conn.execute(
                f'UPDATE "{schema}".doc_version SET version_no = $1 WHERE id = $2',
                next_no, row_id,
            )
        next_no += 1
    print(f"  kept {keep} at version_no={version_no}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repair", action="store_true", help="renumber duplicates instead of only reporting")
    ap.add_argument("--dry-run", action="store_true", help="with --repair, print the plan without writing")
    args = ap.parse_args()

    settings = get_settings()
    schema = settings.pg_schema
    conn = await _connect()
    try:
        dupes = await _find_dupes(conn, schema)
        if not dupes:
            print(f"OK: no (client_id, doc_id, version_no) duplicates in \"{schema}\".doc_version")
            return 0

        print(f"FOUND {len(dupes)} duplicate group(s) in \"{schema}\".doc_version:\n")
        for row in dupes:
            print(f"  client_id={row['client_id']} doc_id={row['doc_id']} "
                  f"version_no={row['version_no']} rows={row['n']} ids={list(row['ids'])}")

        if not args.repair:
            print("\nRun with --repair to renumber duplicates (add --dry-run to preview).")
            return 1

        print("\nRepairing:" if not args.dry_run else "\nDry run — no changes will be written:")
        for row in dupes:
            async with conn.transaction():
                await _repair_group(
                    conn, schema, row["client_id"], row["doc_id"], row["version_no"],
                    list(row["ids"]), dry_run=args.dry_run,
                )
                if args.dry_run:
                    raise asyncio.CancelledError  # rollback the txn; caught below
    except asyncio.CancelledError:
        pass  # expected unwind from the dry-run rollback above
    finally:
        await conn.close()

    if args.repair and not args.dry_run:
        conn2 = await _connect()
        try:
            remaining = await _find_dupes(conn2, schema)
        finally:
            await conn2.close()
        if remaining:
            print(f"\nFAILED: {len(remaining)} duplicate group(s) remain after repair")
            return 1
        print("\nOK: all duplicates repaired")
        return 0

    return 0 if args.dry_run else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
