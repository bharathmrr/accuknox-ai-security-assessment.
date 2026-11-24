"""Task 1 - Fetch books from the Open Library Search API and store them in SQLite.

Features
--------
* Calls the public Open Library Search API (https://openlibrary.org/dev/docs/api/search)
  with a connect/read timeout and bounded retries with exponential backoff.
* Extracts title, author(s) and first publication year, validating each field.
* Persists books in SQLite with NOT NULL / CHECK / UNIQUE constraints.
* Full CRUD through the ``BookRepository`` class; every statement is parameterised,
  so user input is never concatenated into SQL (prevents SQL injection).
* Small CLI built with ``argparse``.

Usage
-----
    python api_to_sqlite.py fetch "clean code" --limit 5
    python api_to_sqlite.py list
    python api_to_sqlite.py get 1
    python api_to_sqlite.py add --title "My Book" --author "Me" --year 2024
    python api_to_sqlite.py update 1 --year 2009
    python api_to_sqlite.py delete 1
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

LOGGER = logging.getLogger("api_to_sqlite")

OPEN_LIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
DEFAULT_DB_PATH = Path(__file__).parent / "output" / "books.db"
DEFAULT_TIMEOUT: tuple[float, float] = (5.0, 10.0)  # (connect, read) seconds
MAX_QUERY_LENGTH = 200
MAX_LIMIT = 100
MIN_YEAR = 1000  # Open Library contains some very old works; reject obviously bad data.

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS books (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT    NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 500),
    author            TEXT    NOT NULL CHECK (length(trim(author)) BETWEEN 1 AND 500),
    publication_year  INTEGER CHECK (publication_year IS NULL
                                     OR publication_year BETWEEN 1000 AND 2100),
    source            TEXT    NOT NULL DEFAULT 'manual'
                              CHECK (source IN ('openlibrary', 'manual')),
    source_key        TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (title, author, publication_year)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_books_source_key
    ON books (source_key) WHERE source_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_books_author ON books (author);
"""


# --------------------------------------------------------------------------- #
# Exceptions and data model
# --------------------------------------------------------------------------- #
class BookAppError(Exception):
    """Base class for all application errors."""


class ValidationError(BookAppError):
    """Raised when user input or API data fails validation."""


class ApiError(BookAppError):
    """Raised when the remote API cannot be reached or returns bad data."""


class RetryableApiError(ApiError):
    """Transient API failure (timeout, connection error, HTTP 429/5xx)."""


class NotFoundError(BookAppError):
    """Raised when a requested record does not exist."""


@dataclass(frozen=True)
class Book:
    """A validated book record."""

    title: str
    author: str
    publication_year: Optional[int]
    source: str = "manual"
    source_key: Optional[str] = None
    id: Optional[int] = None


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_query(query: str) -> str:
    """Return a cleaned search query or raise ``ValidationError``."""
    if not isinstance(query, str):
        raise ValidationError("Search query must be a string.")
    cleaned = " ".join(query.split())
    if not cleaned:
        raise ValidationError("Search query must not be empty.")
    if len(cleaned) > MAX_QUERY_LENGTH:
        raise ValidationError(f"Search query must be at most {MAX_QUERY_LENGTH} characters.")
    return cleaned


def validate_limit(limit: int) -> int:
    """Ensure ``limit`` is an integer between 1 and ``MAX_LIMIT``."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValidationError("Limit must be an integer.")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValidationError(f"Limit must be between 1 and {MAX_LIMIT}.")
    return limit


def validate_text(value: Any, field: str, max_len: int = 500) -> str:
    """Validate a required free-text field (title/author)."""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string.")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValidationError(f"{field} must not be empty.")
    if len(cleaned) > max_len:
        raise ValidationError(f"{field} must be at most {max_len} characters.")
    return cleaned


def validate_year(value: Any) -> Optional[int]:
    """Validate an optional publication year."""
    if value is None or value == "":
        return None
    try:
        year = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Publication year '{value}' is not an integer.") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValidationError("Publication year must be a whole number.")
    max_year = date.today().year + 1
    if not MIN_YEAR <= year <= max_year:
        raise ValidationError(f"Publication year must be between {MIN_YEAR} and {max_year}.")
    return year


def make_book(title: Any, author: Any, year: Any, source: str = "manual",
              source_key: Optional[str] = None) -> Book:
    """Build a ``Book`` after validating every field."""
    return Book(
        title=validate_text(title, "Title"),
        author=validate_text(author, "Author"),
        publication_year=validate_year(year),
        source=source,
        source_key=source_key,
    )


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #
def fetch_books_json(query: str, limit: int = 10, *, session: Optional[requests.Session] = None,
                     timeout: tuple[float, float] = DEFAULT_TIMEOUT, retries: int = 3,
                     backoff: float = 0.5) -> dict[str, Any]:
    """Call the Open Library Search API and return the decoded JSON body.

    Retries on timeouts, connection errors and HTTP 429/5xx with exponential backoff.
    Raises ``ApiError`` when all attempts fail or the body is not valid JSON.
    """
    query = validate_query(query)
    limit = validate_limit(limit)
    http = session or requests.Session()
    params = {
        "q": query,  # requests URL-encodes params; nothing is string-concatenated.
        "limit": limit,
        "fields": "key,title,author_name,first_publish_year",
    }
    headers = {"User-Agent": "accuknox-assessment-demo/1.0 (educational use)"}

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            response = http.get(OPEN_LIBRARY_SEARCH_URL, params=params,
                                headers=headers, timeout=timeout)
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableApiError(f"Open Library returned HTTP {response.status_code}.")
            if response.status_code != 200:
                # 4xx other than 429 will not succeed on retry.
                raise ApiError(f"Open Library rejected the request (HTTP {response.status_code}).")
            try:
                payload = response.json()
            except ValueError as exc:
                raise ApiError("Open Library returned a response that is not valid JSON.") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("docs"), list):
                raise ApiError("Unexpected response shape: missing 'docs' list.")
            return payload
        except requests.Timeout as exc:
            last_error = RetryableApiError(f"Request timed out after {timeout} seconds.")
            last_error.__cause__ = exc
        except requests.ConnectionError as exc:
            last_error = RetryableApiError("Could not connect to Open Library (network error).")
            last_error.__cause__ = exc
        except RetryableApiError as exc:
            last_error = exc
        except requests.RequestException as exc:
            raise ApiError(f"HTTP request failed: {exc}") from exc

        if attempt < retries:
            sleep_for = backoff * (2 ** (attempt - 1))
            LOGGER.warning("Attempt %d/%d failed (%s); retrying in %.1fs",
                           attempt, retries, last_error, sleep_for)
            time.sleep(sleep_for)

    raise last_error if last_error else ApiError("Request failed.")


def parse_books(payload: dict[str, Any]) -> tuple[list[Book], int]:
    """Convert an Open Library payload into validated ``Book`` objects.

    Returns ``(books, skipped_count)``. Documents with no title or author are
    skipped rather than aborting the whole batch.
    """
    books: list[Book] = []
    skipped = 0
    for doc in payload.get("docs", []):
        if not isinstance(doc, dict):
            skipped += 1
            continue
        authors = doc.get("author_name") or []
        author = ", ".join(a for a in authors if isinstance(a, str) and a.strip())
        try:
            books.append(make_book(doc.get("title"), author, doc.get("first_publish_year"),
                                   source="openlibrary", source_key=doc.get("key")))
        except ValidationError as exc:
            LOGGER.info("Skipping document %s: %s", doc.get("key"), exc)
            skipped += 1
    return books, skipped


# --------------------------------------------------------------------------- #
# Repository (CRUD)
# --------------------------------------------------------------------------- #
class BookRepository:
    """SQLite-backed CRUD repository. All SQL uses ``?`` placeholders."""

    UPDATABLE_FIELDS = ("title", "author", "publication_year")

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        """Close the underlying connection."""
        self.conn.close()

    def __enter__(self) -> "BookRepository":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @staticmethod
    def _row_to_book(row: sqlite3.Row) -> Book:
        return Book(id=row["id"], title=row["title"], author=row["author"],
                    publication_year=row["publication_year"], source=row["source"],
                    source_key=row["source_key"])

    # Create -------------------------------------------------------------- #
    def create(self, book: Book) -> int:
        """Insert one book and return its id. Raises ``ValidationError`` on duplicates."""
        try:
            with self.conn:  # commits on success, rolls back on exception
                cur = self.conn.execute(
                    "INSERT INTO books (title, author, publication_year, source, source_key) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (book.title, book.author, book.publication_year, book.source, book.source_key),
                )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"Book already exists or violates a constraint: {exc}") from exc

    def bulk_upsert(self, books: list[Book]) -> tuple[int, int]:
        """Insert many books in ONE transaction, ignoring duplicates.

        Returns ``(inserted, duplicates)``.
        """
        inserted = 0
        with self.conn:
            for book in books:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO books "
                    "(title, author, publication_year, source, source_key) VALUES (?, ?, ?, ?, ?)",
                    (book.title, book.author, book.publication_year, book.source, book.source_key),
                )
                inserted += cur.rowcount
        return inserted, len(books) - inserted

    # Read ---------------------------------------------------------------- #
    def get(self, book_id: int) -> Book:
        """Return one book or raise ``NotFoundError``."""
        row = self.conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"No book with id {book_id}.")
        return self._row_to_book(row)

    def list(self, author_contains: Optional[str] = None, limit: int = 100) -> list[Book]:
        """List books, optionally filtering by a substring of the author name."""
        if author_contains:
            # Escape LIKE wildcards so user input is treated literally.
            pattern = "%" + author_contains.replace("\\", "\\\\").replace("%", "\\%") \
                .replace("_", "\\_") + "%"
            rows = self.conn.execute(
                "SELECT * FROM books WHERE author LIKE ? ESCAPE '\\' ORDER BY id LIMIT ?",
                (pattern, limit),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM books ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [self._row_to_book(r) for r in rows]

    def iter_all(self) -> Iterator[Book]:
        """Yield every book lazily."""
        for row in self.conn.execute("SELECT * FROM books ORDER BY id"):
            yield self._row_to_book(row)

    # Update -------------------------------------------------------------- #
    def update(self, book_id: int, **changes: Any) -> Book:
        """Update whitelisted fields of a book and return the new record."""
        unknown = set(changes) - set(self.UPDATABLE_FIELDS)
        if unknown:
            raise ValidationError(f"Cannot update field(s): {', '.join(sorted(unknown))}")
        current = self.get(book_id)
        updated = make_book(
            changes.get("title", current.title),
            changes.get("author", current.author),
            changes.get("publication_year", current.publication_year),
        )
        try:
            with self.conn:
                # Column names come from a fixed whitelist, values are bound parameters.
                self.conn.execute(
                    "UPDATE books SET title = ?, author = ?, publication_year = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (updated.title, updated.author, updated.publication_year, book_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"Update violates a constraint: {exc}") from exc
        return self.get(book_id)

    # Delete -------------------------------------------------------------- #
    def delete(self, book_id: int) -> None:
        """Delete a book or raise ``NotFoundError``."""
        with self.conn:
            cur = self.conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        if cur.rowcount == 0:
            raise NotFoundError(f"No book with id {book_id}.")


# --------------------------------------------------------------------------- #
# Presentation / CLI
# --------------------------------------------------------------------------- #
def format_table(books: list[Book]) -> str:
    """Render books as a fixed-width text table."""
    if not books:
        return "(no books stored)"
    header = f"{'ID':>4}  {'Year':>4}  {'Title':<42}  Author"
    lines = [header, "-" * len(header) + "-" * 20]
    for b in books:
        title = b.title if len(b.title) <= 42 else b.title[:39] + "..."
        year = str(b.publication_year) if b.publication_year else "n/a"
        lines.append(f"{b.id:>4}  {year:>4}  {title:<42}  {b.author}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(description="Open Library -> SQLite book manager")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Search Open Library and store results")
    p_fetch.add_argument("query")
    p_fetch.add_argument("--limit", type=int, default=10)
    p_fetch.add_argument("--save-raw", metavar="FILE",
                         help="Also write the raw API JSON to FILE (useful for documentation)")

    p_list = sub.add_parser("list", help="List stored books")
    p_list.add_argument("--author", help="Filter by author substring")

    p_get = sub.add_parser("get", help="Show one book")
    p_get.add_argument("id", type=int)

    p_add = sub.add_parser("add", help="Add a book manually")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--author", required=True)
    p_add.add_argument("--year", type=int)

    p_upd = sub.add_parser("update", help="Update a book")
    p_upd.add_argument("id", type=int)
    p_upd.add_argument("--title")
    p_upd.add_argument("--author")
    p_upd.add_argument("--year", type=int, dest="publication_year")

    p_del = sub.add_parser("delete", help="Delete a book")
    p_del.add_argument("id", type=int)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        with BookRepository(args.db) as repo:
            if args.command == "fetch":
                payload = fetch_books_json(args.query, args.limit)
                if args.save_raw:
                    Path(args.save_raw).write_text(json.dumps(payload, indent=2), encoding="utf-8")
                books, skipped = parse_books(payload)
                inserted, dupes = repo.bulk_upsert(books)
                print(f"Fetched {len(payload['docs'])} documents for '{args.query}': "
                      f"{inserted} inserted, {dupes} duplicates ignored, "
                      f"{skipped} invalid skipped.")
                print(format_table(repo.list()))
            elif args.command == "list":
                print(format_table(repo.list(author_contains=args.author)))
            elif args.command == "get":
                print(format_table([repo.get(args.id)]))
            elif args.command == "add":
                new_id = repo.create(make_book(args.title, args.author, args.year))
                print(f"Created book {new_id}.")
                print(format_table([repo.get(new_id)]))
            elif args.command == "update":
                changes = {k: v for k, v in (("title", args.title), ("author", args.author),
                                             ("publication_year", args.publication_year))
                           if v is not None}
                if not changes:
                    raise ValidationError("Nothing to update; pass --title, --author or --year.")
                print(format_table([repo.update(args.id, **changes)]))
            elif args.command == "delete":
                repo.delete(args.id)
                print(f"Deleted book {args.id}.")
        return 0
    except BookAppError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
