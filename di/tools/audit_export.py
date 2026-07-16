"""``python -m di.tools.audit_export`` — access-log retention: export a month's partition to a
CSV file, then DETACH + DROP it from Postgres.

``di_access_log`` is append-only and monthly-partitioned (migration 007). Postgres handles this
fine indefinitely, but unbounded retention does not scale to years, and a bank's long-horizon KYC
retention requirement belongs in object storage, not the operational database. This tool is the
"move it to cold storage" half of that story — scheduling it (a cron / Cloud Scheduler job calling
it monthly) is an ops obligation this tool does not itself perform.

Runs as the owner/migration role (DETACH/DROP is DDL, which the least-privilege runtime role does
not have — see docs/specs/2026-07-15-enterprise-scale-plan.md §3's role split).

Usage::

    python -m di.tools.audit_export --month 2026-01 --dest /path/to/export/dir
    python -m di.tools.audit_export --month 2026-01 --dest /path/to/export/dir --drop
    python -m di.tools.audit_export --check-retention   # lists partitions older than the
                                                          # configured retention horizon
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from di.config import get_settings
from di.db import month_partition_name, open_migration_connection


def _parse_month(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}") from exc


def _partition_month(relname: str) -> date:
    # di_access_log_YYYY_MM
    _, year, month = relname.rsplit("_", 2)
    return date(int(year), int(month), 1)


async def _export_month(month: date, dest: Path, *, drop: bool) -> None:
    settings = get_settings()
    schema = settings.pg_schema
    partition = month_partition_name(month)
    conn = await open_migration_connection(settings)
    try:
        exists = await conn.fetchval("SELECT to_regclass($1)", f'"{schema}"."{partition}"')
        if exists is None:
            print(f"error: partition {schema}.{partition} does not exist", file=sys.stderr)
            sys.exit(1)

        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / f"{partition}.csv"
        rows = await conn.fetch(f'SELECT * FROM "{schema}"."{partition}" ORDER BY ts, id')
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if rows:
                writer.writerow(rows[0].keys())
                for r in rows:
                    writer.writerow([r[k] for k in r.keys()])
        print(f"exported {len(rows)} row(s) -> {out_path}")

        if drop:
            async with conn.transaction():
                await conn.execute(
                    f'ALTER TABLE "{schema}".di_access_log '
                    f'DETACH PARTITION "{schema}"."{partition}"'
                )
                await conn.execute(f'DROP TABLE "{schema}"."{partition}"')
            print(f"detached and dropped {schema}.{partition}")
    finally:
        await conn.close()


async def _check_retention() -> None:
    settings = get_settings()
    schema = settings.pg_schema
    cutoff = date.today() - timedelta(days=settings.access_audit_retention_days)
    conn = await open_migration_connection(settings)
    try:
        rows = await conn.fetch(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "JOIN pg_namespace n ON n.oid = p.relnamespace "
            "WHERE n.nspname = $1 AND p.relname = 'di_access_log' "
            "ORDER BY c.relname",
            schema,
        )
    finally:
        await conn.close()
    stale = [r["relname"] for r in rows if _partition_month(r["relname"]) < cutoff]
    if not stale:
        print(f"OK: no partitions older than {settings.access_audit_retention_days} days "
              f"(cutoff {cutoff.isoformat()})")
        return
    print(f"{len(stale)} partition(s) older than the retention horizon "
          f"({settings.access_audit_retention_days} days, cutoff {cutoff.isoformat()}):")
    for name in stale:
        print(f"  {name}")
    print("\nExport + drop each with: "
          "python -m di.tools.audit_export --month YYYY-MM --dest DIR --drop")


async def _main(args: argparse.Namespace) -> None:
    if args.check_retention:
        await _check_retention()
        return
    if not args.month or not args.dest:
        print("error: --month and --dest are required unless --check-retention is given",
              file=sys.stderr)
        sys.exit(2)
    await _export_month(args.month, Path(args.dest), drop=args.drop)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--month", type=_parse_month, help="YYYY-MM")
    parser.add_argument("--dest", help="output directory for the CSV export")
    parser.add_argument("--drop", action="store_true",
                        help="detach + drop the partition after a successful export")
    parser.add_argument("--check-retention", action="store_true",
                        help="list partitions older than access_audit_retention_days and exit")
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
