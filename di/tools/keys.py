"""``python -m di.tools.keys`` — the production first-key path.

Production unsets ``DI_BOOTSTRAP_API_KEY`` (enforced by ``di.posture``), so a fresh deployment
needs a way to mint its first real API key without a long-lived wildcard secret sitting in the
environment forever. This CLI does that: it connects directly (bypassing the HTTP API, since
there is no key yet to call it with) and calls the same ``di.auth.create_api_key`` the admin
router uses.

Ordering: this CLI assumes migrations have already been applied (``di_api_key`` must exist). If
run against a database that has not been migrated yet, it fails with an actionable message rather
than a raw asyncpg error.

Usage::

    python -m di.tools.keys create --name ops-console --client-ids '*' --scopes admin \\
        --expires-in-days 90

    python -m di.tools.keys list

    python -m di.tools.keys revoke --key-id <uuid>
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from di import auth
from di.config import get_settings
from di.db import close_pool, init_pool


async def _check_schema_ready() -> None:
    """Fail with an actionable message rather than a raw asyncpg error when the schema is not
    migrated yet — this CLI is often the very first thing run against a fresh database."""
    settings = get_settings()
    pool = await init_pool(settings)
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT to_regclass($1)", f'"{settings.pg_schema}".di_api_key'
        )
    if exists is None:
        print(
            f"error: \"{settings.pg_schema}\".di_api_key does not exist yet — run migrations "
            "first: `python -m di.migrate` (or start the app once with MIGRATIONS_MODE=auto).",
            file=sys.stderr,
        )
        sys.exit(1)


async def _create(args: argparse.Namespace) -> None:
    await _check_schema_ready()
    expires_at = None
    if args.expires_in_days is not None:
        expires_at = datetime.now(tz=UTC) + timedelta(days=args.expires_in_days)
    key_id, raw = await auth.create_api_key(
        name=args.name, client_ids=args.client_ids, scopes=args.scopes,
        expires_at=expires_at, created_by=f"cli:{args.created_by or 'unknown'}",
        rate_limit_rps=args.rate_limit_rps,
    )
    print(f"key_id:     {key_id}")
    print(f"api_key:    {raw}")
    print(f"expires_at: {expires_at.isoformat() if expires_at else '(never)'}")
    print("\nStore this key now — it is shown exactly once and cannot be recovered.")


async def _list(_: argparse.Namespace) -> None:
    await _check_schema_ready()
    keys = await auth.list_api_keys()
    if not keys:
        print("(no keys)")
        return
    for k in keys:
        status = "disabled" if k.get("disabled_at") else "active"
        print(f"{k['id']}  {k['name']:<30} {status:<10} client_ids={k['client_ids']} "
              f"scopes={k['scopes']} expires_at={k.get('expires_at')}")


async def _revoke(args: argparse.Namespace) -> None:
    await _check_schema_ready()
    ok = await auth.revoke_api_key(args.key_id)
    print("revoked" if ok else "not found or already disabled")
    if not ok:
        sys.exit(1)


async def _rotate(args: argparse.Namespace) -> None:
    await _check_schema_ready()
    new_key_id, raw, old_expires_at = await auth.rotate_api_key(
        args.key_id, overlap_hours=args.overlap_hours)
    print(f"new_key_id:        {new_key_id}")
    print(f"api_key:           {raw}")
    print(f"old_key_expires_at: {old_expires_at.isoformat()}")


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="mint a new API key")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--client-ids", type=_split_csv, default=["*"],
                          help="comma-separated tenant ids, or '*' for all")
    p_create.add_argument("--scopes", type=_split_csv, default=["read"],
                          help="comma-separated scopes: ingest,read,admin,* ")
    p_create.add_argument("--expires-in-days", type=int, default=90,
                          help="0 or omit --expires-in-days=0 for no expiry (not recommended)")
    p_create.add_argument("--rate-limit-rps", type=float, default=None)
    p_create.add_argument("--created-by", default=None, help="operator name, for the audit trail")
    p_create.set_defaults(func=_create)

    p_list = sub.add_parser("list", help="list issued keys (metadata only)")
    p_list.set_defaults(func=_list)

    p_revoke = sub.add_parser("revoke", help="revoke a key immediately")
    p_revoke.add_argument("--key-id", required=True)
    p_revoke.set_defaults(func=_revoke)

    p_rotate = sub.add_parser("rotate", help="mint a successor and time-box the predecessor")
    p_rotate.add_argument("--key-id", required=True)
    p_rotate.add_argument("--overlap-hours", type=int, default=None)
    p_rotate.set_defaults(func=_rotate)

    args = parser.parse_args()
    if getattr(args, "expires_in_days", None) == 0:
        args.expires_in_days = None

    async def _run():
        try:
            await args.func(args)
        finally:
            await close_pool()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
