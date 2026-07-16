"""``python -m di.tools.remerge_backfill`` — proactive re-merge sweep for the multi-valued-facts
upgrade (migration 008).

Existing ``client_merged_fact`` rows for a promoted multi-valued key keep ``instance_key=''``
until the client is next touched (an ingest or an adjudication triggers
``di.pipeline._remerge_client_facts``, which rewrites them with real fingerprints). At
millions-of-clients scale that is optional hygiene, not a correctness requirement — the collapsed
``''`` rows are no worse than they were before this upgrade until re-merged.

Client ids must be supplied explicitly (``--client-ids`` or ``--client-ids-file``), not
auto-discovered: ``client_merged_fact`` carries FORCE ROW LEVEL SECURITY with a tenant_isolation
policy, and the migration/owner role is deliberately NOT BYPASSRLS (see the Phase-1 role split,
docs/specs/2026-07-15-enterprise-scale-plan.md §3) — there is no connection in this system that
can legitimately SELECT DISTINCT client_id across every tenant's rows, by design. Get the
candidate list from wherever your deployment already tracks tenant ids (a client registry, a
billing system, an export from before the upgrade) — the same constraint that keeps a compromised
request handler from reading another tenant's data also applies to this tool.

Usage::

    python -m di.tools.remerge_backfill --client-ids acme-001,acme-002
    python -m di.tools.remerge_backfill --client-ids-file clients.txt   # one client_id per line
    python -m di.tools.remerge_backfill --client-ids acme-001 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from di.config import get_settings
from di.db import close_pool, init_pool
from di.pipeline import _remerge_client_facts


def _load_client_ids(args: argparse.Namespace) -> list[str]:
    if args.client_ids:
        return [c.strip() for c in args.client_ids.split(",") if c.strip()]
    if args.client_ids_file:
        text = Path(args.client_ids_file).read_text(encoding="utf-8")
        return [line.strip() for line in text.splitlines() if line.strip()]
    return []


async def _main(args: argparse.Namespace) -> None:
    client_ids = _load_client_ids(args)
    if not client_ids:
        print("error: --client-ids or --client-ids-file is required (see module docstring for "
              "why this cannot be auto-discovered)")
        raise SystemExit(2)

    print(f"{len(client_ids)} client(s) to sweep")
    if args.dry_run:
        for cid in client_ids:
            print(f"  {cid}")
        return

    settings = get_settings()
    await init_pool(settings)
    try:
        sem = asyncio.Semaphore(args.concurrency)
        done = 0

        async def _one(cid: str) -> None:
            nonlocal done
            async with sem:
                try:
                    n = await _remerge_client_facts(cid)
                    done += 1
                    print(f"[{done}/{len(client_ids)}] {cid}: {n} merged facts")
                except Exception as exc:  # noqa: BLE001 - one client's failure must not stop the sweep
                    done += 1
                    print(f"[{done}/{len(client_ids)}] {cid}: FAILED — {exc}")
        await asyncio.gather(*(_one(cid) for cid in client_ids))
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-ids", help="comma-separated client ids")
    parser.add_argument("--client-ids-file", help="path to a file with one client_id per line")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true",
                        help="list the client ids without re-merging")
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
