"""Document versioning logic — pure, DB-free, in-memory.

A document version is identified by the SHA-256 of its (canonical) bytes. When a new upload
arrives for an existing document we decide whether it is a no-op (identical content) or a new
version that supersedes the previous one. We also diff the knowledge-subtree node hashes between
two versions so downstream consumers know which paths were added / removed / modified.

No database, no network, no heavy dependencies: everything here operates on in-memory structures
and uses only the standard library so the module imports and runs in any environment.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class VersionPlan:
    """Outcome of comparing a freshly-hashed upload against the current stored version.

    Attributes:
        is_noop: True when the new content hash equals the current hash (nothing to do).
        version_no: The version number to assign. Unchanged from ``current_no`` on a no-op,
            otherwise ``(current_no or 0) + 1``.
        supersedes_no: The version number this new version replaces (the prior ``current_no``),
            or ``None`` when this is the first version / a no-op.
    """

    is_noop: bool
    version_no: int
    supersedes_no: int | None


def content_hash(data: bytes | str) -> str:
    """Return the lowercase hex SHA-256 of ``data``.

    Strings are encoded as UTF-8 before hashing so the same logical content hashes identically
    regardless of whether the caller passes ``bytes`` or ``str``.

    Args:
        data: Raw document bytes or text.

    Returns:
        64-character lowercase hex digest.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def decide_version(
    new_hash: str,
    current_no: int | None,
    current_hash: str | None,
) -> VersionPlan:
    """Decide what to do with a new upload given the current stored version.

    Rules:
        * If ``new_hash`` matches ``current_hash`` the upload is a no-op; the version number stays
          at ``current_no`` (or 0 when there is no current version) and nothing is superseded.
        * Otherwise this is a new version: ``version_no = (current_no or 0) + 1`` and it supersedes
          the prior ``current_no`` (``None`` for a first version).

    Args:
        new_hash: SHA-256 hex of the incoming content.
        current_no: Version number of the currently-stored version, or ``None`` if none exists.
        current_hash: SHA-256 hex of the currently-stored version, or ``None`` if none exists.

    Returns:
        A :class:`VersionPlan` describing the decision.
    """
    if current_hash is not None and new_hash == current_hash:
        return VersionPlan(is_noop=True, version_no=current_no or 0, supersedes_no=None)
    return VersionPlan(
        is_noop=False,
        version_no=(current_no or 0) + 1,
        supersedes_no=current_no,
    )


def diff_nodes(
    old: list[tuple[str, str]],
    new: list[tuple[str, str]],
) -> list[dict[str, str]]:
    """Diff two sets of (path, node_content_hash) pairs into changed-field entries.

    Each input tuple is ``(path, node_content_hash)``. Paths present only in ``new`` are
    ``added``; paths present only in ``old`` are ``removed``; paths present in both with a
    differing hash are ``modified``. Paths whose hash is unchanged are omitted.

    Duplicate paths within a single input collapse to their last occurrence (a node path is a
    stable identity, so the latest hash wins). Results are ordered: ``added``/``modified`` follow
    the order they appear in ``new``, then ``removed`` follow the order they appear in ``old``.

    Args:
        old: (path, hash) pairs for the previous version's subtree.
        new: (path, hash) pairs for the new version's subtree.

    Returns:
        A list of ``{"path": ..., "change": "added" | "removed" | "modified"}`` dicts.
    """
    old_by_path = dict(old)
    new_by_path = dict(new)

    changes: list[dict[str, str]] = []
    seen_new: set[str] = set()
    for path, new_hash in new:
        if path in seen_new:
            continue
        seen_new.add(path)
        if path not in old_by_path:
            changes.append({"path": path, "change": "added"})
        elif old_by_path[path] != new_hash:
            changes.append({"path": path, "change": "modified"})

    seen_old: set[str] = set()
    for path, _old_hash in old:
        if path in seen_old:
            continue
        seen_old.add(path)
        if path not in new_by_path:
            changes.append({"path": path, "change": "removed"})

    return changes
