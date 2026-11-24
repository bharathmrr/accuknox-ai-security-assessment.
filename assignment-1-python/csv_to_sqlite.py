"""Task 3 - Reusable CSV -> SQLite user importer.

Highlights
----------
* Streams the CSV with ``csv.DictReader`` (constant memory for large files).
* Validates header, ID, name, email (pragmatic RFC-5322 subset) and phone.
* Detects duplicates both *within the file* and *against existing rows*
  (by ID and by case-insensitive email).
* Runs the whole import inside ONE transaction. ``--mode strict`` rolls back
  everything if any row is invalid; ``--mode skip`` (default) imports valid rows
  and reports rejected ones.
* All SQL statements are parameterised - CSV content is never interpolated into SQL.
* Produces import statistics and an optional rejected-rows CSV.

Usage
-----
    python csv_to_sqlite.py sample_users.csv
    python csv_to_sqlite.py sample_users.csv --db output/users.db --mode strict
    python csv_to_sqlite.py sample_users.csv --rejects output/rejected_rows.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
DEFAULT_DB = BASE_DIR / "output" / "users.db"
REQUIRED_COLUMNS = ("id", "name", "email", "phone")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY CHECK (id > 0),
    name        TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    email       TEXT    NOT NULL COLLATE NOCASE UNIQUE
                        CHECK (email LIKE '%_@_%._%'),
    phone       TEXT    CHECK (phone IS NULL OR length(phone) BETWEEN 7 AND 16),
    imported_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS import_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file  TEXT    NOT NULL,
    started_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    total_rows   INTEGER NOT NULL,
    inserted     INTEGER NOT NULL,
    rejected     INTEGER NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('committed', 'rolled_back'))
);
"""

# Pragmatic email check: local part of allowed characters, no leading/trailing
# or consecutive dots, a domain with at least one dot and a 2+ letter TLD.
EMAIL_RE = re.compile(
    r"^(?!\.)(?!.*\.\.)[A-Za-z0-9._%+\-]{1,64}(?<!\.)@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}$"
)
PHONE_ALLOWED_RE = re.compile(r"^\+?[0-9 ()\-]+$")
NAME_FORBIDDEN_RE = re.compile(r"[\x00-\x1f<>]")


class ImportErrorBase(Exception):
    """Base class for importer errors."""


class CsvFormatError(ImportErrorBase):
    """Raised when the CSV file itself is unusable (missing file/columns)."""


class StrictModeAbort(ImportErrorBase):
    """Raised in strict mode to trigger a rollback."""


@dataclass
class UserRecord:
    """A validated, normalised user row."""

    id: int
    name: str
    email: str
    phone: Optional[str]


@dataclass
class RowError:
    """Why a CSV row was rejected."""

    line: int
    raw: dict[str, str]
    reasons: list[str]


@dataclass
class ImportStats:
    """Statistics for one import run."""

    total_rows: int = 0
    inserted: int = 0
    rejected: int = 0
    missing_fields: int = 0
    invalid_email: int = 0
    invalid_other: int = 0
    duplicate_in_file: int = 0
    duplicate_in_db: int = 0
    status: str = "committed"
    errors: list[RowError] = field(default_factory=list)

    def as_text(self) -> str:
        """Human-readable summary."""
        return "\n".join([
            "Import statistics",
            "-----------------",
            f"Rows read            : {self.total_rows}",
            f"Inserted             : {self.inserted}",
            f"Rejected             : {self.rejected}",
            f"  missing fields     : {self.missing_fields}",
            f"  invalid email      : {self.invalid_email}",
            f"  other invalid data : {self.invalid_other}",
            f"  duplicate in file  : {self.duplicate_in_file}",
            f"  duplicate in DB    : {self.duplicate_in_db}",
            f"Transaction status   : {self.status}",
        ])


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def is_valid_email(email: str) -> bool:
    """Return True if ``email`` passes the pragmatic format check."""
    return len(email) <= 254 and EMAIL_RE.fullmatch(email) is not None


def normalise_phone(phone: str) -> Optional[str]:
    """Return phone digits (keeping a leading '+') or raise ``ValueError``."""
    phone = phone.strip()
    if not phone:
        return None
    if not PHONE_ALLOWED_RE.fullmatch(phone):
        raise ValueError(f"phone '{phone}' contains invalid characters")
    digits = re.sub(r"\D", "", phone)
    if not 7 <= len(digits) <= 15:  # E.164 allows max 15 digits
        raise ValueError(f"phone '{phone}' must have 7-15 digits")
    return ("+" if phone.startswith("+") else "") + digits


def validate_row(raw: dict[str, Optional[str]]) -> tuple[Optional[UserRecord], list[str], set[str]]:
    """Validate a CSV row.

    Returns ``(record_or_None, reasons, categories)`` where categories is a subset
    of ``{"missing", "email", "other"}`` used for statistics.
    """
    reasons: list[str] = []
    cats: set[str] = set()
    values = {k: " ".join((raw.get(k) or "").split()) for k in REQUIRED_COLUMNS}

    for required in ("id", "name", "email"):
        if not values[required]:
            reasons.append(f"missing {required}")
            cats.add("missing")

    user_id: Optional[int] = None
    if values["id"]:
        try:
            user_id = int(values["id"])
            if user_id <= 0:
                raise ValueError
        except ValueError:
            reasons.append(f"id '{values['id']}' is not a positive integer")
            cats.add("other")

    name = values["name"]
    if name and (len(name) > 100 or NAME_FORBIDDEN_RE.search(name)):
        reasons.append("name is too long or contains forbidden characters")
        cats.add("other")

    email = values["email"].lower()
    if email and not is_valid_email(email):
        reasons.append(f"invalid email '{values['email']}'")
        cats.add("email")

    phone: Optional[str] = None
    try:
        phone = normalise_phone(values["phone"])
    except ValueError as exc:
        reasons.append(str(exc))
        cats.add("other")

    if reasons or user_id is None:
        return None, reasons, cats
    return UserRecord(id=user_id, name=name, email=email, phone=phone), [], set()


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (and initialise) the SQLite database."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None -> we control BEGIN/COMMIT/ROLLBACK explicitly.
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(SCHEMA_SQL)
    return conn


def import_csv(csv_path: str | Path, conn: sqlite3.Connection, mode: str = "skip") -> ImportStats:
    """Import users from ``csv_path`` into ``conn`` within a single transaction."""
    if mode not in ("skip", "strict"):
        raise ValueError("mode must be 'skip' or 'strict'")
    path = Path(csv_path)
    if not path.is_file():
        raise CsvFormatError(f"CSV file not found: {path}")

    stats = ImportStats()
    seen_ids: set[int] = set()
    seen_emails: set[str] = set()

    def reject(line: int, raw: dict[str, str], reasons: list[str]) -> None:
        stats.rejected += 1
        stats.errors.append(RowError(line, dict(raw), reasons))
        if mode == "strict":
            raise StrictModeAbort(f"line {line}: {'; '.join(reasons)}")

    with path.open(newline="", encoding="utf-8-sig") as fh:  # utf-8-sig strips Excel BOM
        reader = csv.DictReader(fh)
        header = [h.strip().lower() for h in (reader.fieldnames or [])]
        missing_cols = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing_cols:
            raise CsvFormatError(f"CSV is missing required column(s): {', '.join(missing_cols)}")
        reader.fieldnames = header

        conn.execute("BEGIN IMMEDIATE")  # take the write lock up front
        try:
            for raw in reader:
                line = reader.line_num
                stats.total_rows += 1
                record, reasons, cats = validate_row(raw)
                if record is None:
                    stats.missing_fields += "missing" in cats
                    stats.invalid_email += "email" in cats
                    stats.invalid_other += "other" in cats
                    reject(line, raw, reasons)
                    continue

                if record.id in seen_ids or record.email in seen_emails:
                    stats.duplicate_in_file += 1
                    what = "id" if record.id in seen_ids else "email"
                    reject(line, raw, [f"duplicate {what} within file"])
                    continue

                exists = conn.execute(
                    "SELECT 1 FROM users WHERE id = ? OR email = ? LIMIT 1",
                    (record.id, record.email),
                ).fetchone()
                if exists:
                    stats.duplicate_in_db += 1
                    reject(line, raw, ["id or email already exists in database"])
                    continue

                conn.execute(
                    "INSERT INTO users (id, name, email, phone) VALUES (?, ?, ?, ?)",
                    (record.id, record.name, record.email, record.phone),
                )
                seen_ids.add(record.id)
                seen_emails.add(record.email)
                stats.inserted += 1

            conn.execute(
                "INSERT INTO import_runs (source_file, total_rows, inserted, rejected, status) "
                "VALUES (?, ?, ?, ?, 'committed')",
                (path.name, stats.total_rows, stats.inserted, stats.rejected),
            )
            conn.execute("COMMIT")
        except (StrictModeAbort, sqlite3.Error):
            conn.execute("ROLLBACK")
            stats.status = "rolled_back"
            stats.inserted = 0
            # Record the failed attempt in its own small transaction for auditability.
            conn.execute(
                "INSERT INTO import_runs (source_file, total_rows, inserted, rejected, status) "
                "VALUES (?, ?, 0, ?, 'rolled_back')",
                (path.name, stats.total_rows, stats.rejected),
            )
            if mode != "strict":
                raise
        except csv.Error as exc:
            conn.execute("ROLLBACK")
            raise CsvFormatError(f"Malformed CSV near line {reader.line_num}: {exc}") from exc
    return stats


def write_rejects(stats: ImportStats, path: Path) -> Path:
    """Write rejected rows and reasons to a CSV for follow-up."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["line", *REQUIRED_COLUMNS, "reasons"])
        for err in stats.errors:
            writer.writerow([err.line, *[err.raw.get(c, "") for c in REQUIRED_COLUMNS],
                             "; ".join(err.reasons)])
    return path


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Import users from CSV into SQLite")
    parser.add_argument("csv_file")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--mode", choices=("skip", "strict"), default="skip",
                        help="skip: import valid rows; strict: all-or-nothing")
    parser.add_argument("--rejects", help="Write rejected rows to this CSV path")
    args = parser.parse_args(argv)

    try:
        conn = connect(args.db)
        try:
            stats = import_csv(args.csv_file, conn, args.mode)
            rows = conn.execute("SELECT id, name, email, phone FROM users ORDER BY id").fetchall()
        finally:
            conn.close()
    except CsvFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 2

    for err in stats.errors:
        print(f"  rejected line {err.line}: {'; '.join(err.reasons)}")
    print(stats.as_text())
    if args.rejects:
        print(f"Rejected rows written to {write_rejects(stats, Path(args.rejects))}")
    print(f"\nUsers table now holds {len(rows)} row(s):")
    for r in rows:
        print(f"  {r[0]:>3}  {r[1]:<32} {r[2]:<30} {r[3] or ''}")
    return 0 if stats.status == "committed" else 3


if __name__ == "__main__":
    sys.exit(main())
