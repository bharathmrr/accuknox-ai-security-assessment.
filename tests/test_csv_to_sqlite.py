"""Tests for csv_to_sqlite.py."""

from __future__ import annotations

from pathlib import Path

import pytest

import csv_to_sqlite as imp

SAMPLE_CSV = Path(imp.__file__).parent / "sample_users.csv"


@pytest.fixture
def conn():
    c = imp.connect(":memory:")
    yield c
    c.close()


def write_csv(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "u.csv"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.mark.parametrize("email,ok", [
    ("a.b@example.com", True), ("a+tag@sub.example.co.in", True),
    ("a@example", False), ("a..b@example.com", False), (".a@example.com", False),
    ("a@-example.com", False), ("no-at-sign.com", False), ("a@example.c", False)])
def test_email_validation(email, ok):
    assert imp.is_valid_email(email) is ok


def test_phone_normalisation():
    assert imp.normalise_phone("+91 98450-12345") == "+919845012345"
    assert imp.normalise_phone("") is None
    for bad in ("12ab34", "123", "1" * 16):
        with pytest.raises(ValueError):
            imp.normalise_phone(bad)


def test_sample_file_statistics(conn):
    stats = imp.import_csv(SAMPLE_CSV, conn)
    assert (stats.total_rows, stats.inserted, stats.rejected) == (14, 7, 7)
    assert stats.invalid_email == 2
    assert stats.duplicate_in_file == 2
    assert stats.missing_fields == 1
    assert stats.status == "committed"
    # Emails normalised to lower case; whitespace collapsed.
    row = conn.execute("SELECT name, email FROM users WHERE id = 12").fetchone()
    assert row == ("Ishaan Bose", "ishaan.bose@example.com")


def test_sql_injection_payload_is_inert(conn):
    imp.import_csv(SAMPLE_CSV, conn)
    name = conn.execute("SELECT name FROM users WHERE id = 9").fetchone()[0]
    assert name == "Robert'); DROP TABLE users;--"
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 7


def test_reimport_detects_db_duplicates(conn):
    imp.import_csv(SAMPLE_CSV, conn)
    stats = imp.import_csv(SAMPLE_CSV, conn)
    assert stats.inserted == 0
    # 7 previously inserted rows + the 2 rows that were in-file duplicates on the
    # first run (same id / email as rows now stored) all collide with the DB.
    assert stats.duplicate_in_db == 9
    assert conn.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0] == 2


def test_strict_mode_rolls_back_everything(conn):
    stats = imp.import_csv(SAMPLE_CSV, conn, mode="strict")
    assert stats.status == "rolled_back" and stats.inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    status = conn.execute("SELECT status FROM import_runs").fetchone()[0]
    assert status == "rolled_back"


def test_strict_mode_commits_clean_file(conn, tmp_path):
    p = write_csv(tmp_path, "id,name,email,phone\n1,A,a@example.com,9845012345\n")
    assert imp.import_csv(p, conn, mode="strict").inserted == 1


def test_missing_columns_and_file(conn, tmp_path):
    with pytest.raises(imp.CsvFormatError, match="missing required column"):
        imp.import_csv(write_csv(tmp_path, "id,name\n1,A\n"), conn)
    with pytest.raises(imp.CsvFormatError, match="not found"):
        imp.import_csv(tmp_path / "nope.csv", conn)


def test_header_case_and_bom(conn, tmp_path):
    p = tmp_path / "bom.csv"
    p.write_bytes("﻿ID,Name,Email,Phone\n1,A,a@example.com,\n".encode("utf-8"))
    assert imp.import_csv(p, conn).inserted == 1


def test_cli_and_rejects_file(tmp_path, capsys):
    rejects = tmp_path / "rej.csv"
    code = imp.main([str(SAMPLE_CSV), "--db", str(tmp_path / "u.db"), "--rejects", str(rejects)])
    assert code == 0
    assert "Inserted             : 7" in capsys.readouterr().out
    assert len(rejects.read_text(encoding="utf-8").strip().splitlines()) == 8  # header + 7
