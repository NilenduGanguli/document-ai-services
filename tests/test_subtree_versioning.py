"""Unit tests for di.subtree.versioning (pure, DB-free)."""
from __future__ import annotations

import hashlib

from di.subtree.versioning import VersionPlan, content_hash, decide_version, diff_nodes


def test_content_hash_str_and_bytes_match() -> None:
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert content_hash("hello world") == expected
    assert content_hash(b"hello world") == expected
    # 64 lowercase hex chars
    h = content_hash("anything")
    assert len(h) == 64
    assert h == h.lower()


def test_content_hash_distinguishes_content() -> None:
    assert content_hash("a") != content_hash("b")


def test_decide_version_noop_on_identical_hash() -> None:
    plan = decide_version(new_hash="abc", current_no=3, current_hash="abc")
    assert plan == VersionPlan(is_noop=True, version_no=3, supersedes_no=None)


def test_decide_version_increment_and_supersedes() -> None:
    plan = decide_version(new_hash="def", current_no=3, current_hash="abc")
    assert plan.is_noop is False
    assert plan.version_no == 4
    assert plan.supersedes_no == 3


def test_decide_version_first_version() -> None:
    plan = decide_version(new_hash="abc", current_no=None, current_hash=None)
    assert plan == VersionPlan(is_noop=False, version_no=1, supersedes_no=None)


def test_decide_version_noop_when_current_no_is_zero() -> None:
    plan = decide_version(new_hash="abc", current_no=0, current_hash="abc")
    assert plan == VersionPlan(is_noop=True, version_no=0, supersedes_no=None)


def test_decide_version_resume_on_incomplete_hash_match() -> None:
    """The at-least-once noop-on-retry fix: a hash match against an INCOMPLETE version is never
    a noop — it must resume the SAME version_no, not mint a new one and not skip work."""
    plan = decide_version(new_hash="abc", current_no=3, current_hash="abc",
                          current_complete=False)
    assert plan.is_noop is False
    assert plan.resume is True
    assert plan.version_no == 3
    assert plan.supersedes_no is None


def test_decide_version_default_complete_is_true() -> None:
    """current_complete defaults to True so pre-ingest_complete callers are unaffected."""
    plan = decide_version(new_hash="abc", current_no=3, current_hash="abc")
    assert plan.resume is False
    assert plan.is_noop is True


def test_decide_version_new_content_ignores_completeness() -> None:
    """A genuinely new version is unaffected by current_complete either way — resume only
    applies when the hash actually matches."""
    plan = decide_version(new_hash="def", current_no=3, current_hash="abc",
                          current_complete=False)
    assert plan.is_noop is False
    assert plan.resume is False
    assert plan.version_no == 4
    assert plan.supersedes_no == 3


def test_diff_nodes_added_removed_modified() -> None:
    old = [
        ("root.a", "h1"),
        ("root.b", "h2"),
        ("root.c", "h3"),
    ]
    new = [
        ("root.a", "h1"),       # unchanged -> omitted
        ("root.b", "h2-new"),   # modified
        ("root.d", "h4"),       # added
    ]
    result = diff_nodes(old, new)
    assert {"path": "root.b", "change": "modified"} in result
    assert {"path": "root.d", "change": "added"} in result
    assert {"path": "root.c", "change": "removed"} in result
    # unchanged node must not appear
    assert all(entry["path"] != "root.a" for entry in result)
    assert len(result) == 3
    # every entry has the expected shape
    for entry in result:
        assert set(entry) == {"path", "change"}
        assert entry["change"] in {"added", "removed", "modified"}


def test_diff_nodes_empty_inputs() -> None:
    assert diff_nodes([], []) == []
    assert diff_nodes([("p", "h")], []) == [{"path": "p", "change": "removed"}]
    assert diff_nodes([], [("p", "h")]) == [{"path": "p", "change": "added"}]


def test_diff_nodes_ordering_new_then_removed() -> None:
    old = [("x", "1"), ("y", "2")]
    new = [("y", "2-mod"), ("z", "3")]
    result = diff_nodes(old, new)
    # added/modified (from new order) come before removed (from old order)
    assert result == [
        {"path": "y", "change": "modified"},
        {"path": "z", "change": "added"},
        {"path": "x", "change": "removed"},
    ]


def test_diff_nodes_duplicate_path_uses_last_hash() -> None:
    old = [("p", "old")]
    new = [("p", "ignored"), ("p", "final")]
    result = diff_nodes(old, new)
    assert result == [{"path": "p", "change": "modified"}]
