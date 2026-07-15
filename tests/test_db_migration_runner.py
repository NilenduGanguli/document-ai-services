"""Unit tests for the migration-runner hardening in ``di/db.py``.

Pure-logic pieces only (checksum drift detection, message shape); the actual apply/ledger
transaction and advisory-lock serialization are proved by the DB-backed integration suite
(``DI_RUN_INTEGRATION=1``), since they require a live Postgres.
"""
from __future__ import annotations

import hashlib

from di.db import _drift_message


def test_drift_message_names_the_file_and_both_checksums() -> None:
    msg = _drift_message("005_hardening.sql", "aaaa" * 16, "bbbb" * 16)
    assert "005_hardening.sql" in msg
    assert "aaaa" in msg
    assert "bbbb" in msg
    assert "never be edited" in msg


def test_checksum_is_sensitive_to_any_byte_change() -> None:
    a = hashlib.sha256(b"-- migration v1\nCREATE TABLE x (id int);\n").hexdigest()
    b = hashlib.sha256(b"-- migration v1\nCREATE TABLE x (id  int);\n").hexdigest()  # one extra space
    assert a != b
