"""``python -m di.migrate`` — the standalone migration entrypoint.

Runs every pending migration as the owner/migration role, then exits. This is the CI/CD (or
one-shot Kubernetes Job / Cloud Run Job) step that ``MIGRATIONS_MODE=verify`` deployments depend
on: the running app instances never hold DDL-capable credentials, so schema changes are always
attributable to a deliberate, separately-authorized step rather than a side effect of a rolling
deploy.

Usage::

    python -m di.migrate                          # apply migrations, default embedding dim
    python -m di.migrate --embedding-dim 1536      # explicit dim (skip the /api/models lookup)

Without ``--embedding-dim``, this best-effort queries the retrieval gateway's ``/api/models`` —
the same discovery ``di.app`` does at boot — so vector columns are sized correctly even when this
step runs before any app instance has ever booted against this database. Failing that lookup falls
back to ``settings.embedding_dim_default``, exactly like the in-app path.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from di.config import get_settings
from di.db import open_migration_connection, run_migrations, set_embedding_dim
from di.retrieval_client import get_retrieval_client

logger = logging.getLogger(__name__)


async def _discover_embedding_dim(explicit: int | None) -> None:
    if explicit is not None:
        set_embedding_dim(explicit)
        logger.info("using explicit embedding dim %d", explicit)
        return
    settings = get_settings()
    client = get_retrieval_client(settings)
    try:
        info = await client.models()
        if dim := info.get("embedding_dim"):
            set_embedding_dim(int(dim))
            logger.info("embedding dim set to %s from retrieval /api/models", dim)
            return
    except Exception:  # noqa: BLE001 - best-effort discovery, never fatal to the migration step
        logger.warning("could not query retrieval /api/models for embedding dim", exc_info=True)
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()
    logger.info("using configured default embedding dim %d", settings.embedding_dim_default)


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedding-dim", type=int, default=None,
                        help="explicit vector column dimension; skips the /api/models lookup")
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(level=settings.di_log_level)

    await _discover_embedding_dim(args.embedding_dim)

    conn = await open_migration_connection(settings)
    try:
        await run_migrations(settings, connection=conn)
    except Exception:
        logger.exception("migration run failed")
        return 1
    finally:
        await conn.close()
    logger.info("migrations applied cleanly")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
