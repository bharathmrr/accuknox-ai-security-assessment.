"""Tests for api_to_sqlite.py (HTTP is mocked; no network required)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

import api_to_sqlite as app

SAMPLE = json.loads((app.Path(app.__file__).parent / "data" /
                     "sample_openlibrary_response.json").read_text(encoding="utf-8"))


def fake_response(status: int = 200, payload: object = SAMPLE, bad_json: bool = False):
    resp = MagicMock()
    resp.status_code = status
    if bad_json:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = payload
    return resp


@pytest.fixture
def repo():
    r = app.BookRepository(":memory:")
    yield r
    r.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda _s: None)


# ---- validation ---------------------------------------------------------- #
@pytest.mark.parametrize("query", ["", "   ", "x" * 201])
def test_invalid_query_rejected(query):
    with pytest.raises(app.ValidationError):
        app.validate_query(query)


@pytest.mark.parametrize("limit", [0, 101, "5", True])
def test_invalid_limit_rejected(limit):
    with pytest.raises(app.ValidationError):
        app.validate_limit(limit)


@pytest.mark.parametrize("year", ["abc", 999, 3000, 2008.5])
def test_invalid_year_rejected(year):
    with pytest.raises(app.ValidationError):
        app.validate_year(year)


def test_year_optional_and_coerced():
    assert app.validate_year(None) is None
    assert app.validate_year("2008") == 2008


# ---- API client ---------------------------------------------------------- #
def test_fetch_success_passes_params_and_timeout():
    session = MagicMock()
    session.get.return_value = fake_response()
    payload = app.fetch_books_json("clean code", 3, session=session)
    assert payload["docs"]
    _, kwargs = session.get.call_args
    assert kwargs["params"]["q"] == "clean code"
    assert kwargs["params"]["limit"] == 3
    assert kwargs["timeout"] == app.DEFAULT_TIMEOUT


def test_fetch_retries_on_timeout_then_succeeds():
    session = MagicMock()
    session.get.side_effect = [requests.Timeout(), fake_response()]
    assert app.fetch_books_json("dune", session=session)["docs"]
    assert session.get.call_count == 2


def test_fetch_gives_up_after_retries():
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError()
    with pytest.raises(app.ApiError, match="connect"):
        app.fetch_books_json("dune", session=session, retries=3)
    assert session.get.call_count == 3


def test_fetch_retries_on_5xx():
    session = MagicMock()
    session.get.side_effect = [fake_response(503), fake_response(429), fake_response()]
    app.fetch_books_json("dune", session=session)
    assert session.get.call_count == 3


def test_fetch_does_not_retry_on_4xx():
    session = MagicMock()
    session.get.return_value = fake_response(400)
    with pytest.raises(app.ApiError, match="rejected"):
        app.fetch_books_json("dune", session=session)
    assert session.get.call_count == 1


@pytest.mark.parametrize("resp", [fake_response(bad_json=True),
                                  fake_response(payload={"unexpected": 1})])
def test_fetch_rejects_bad_body(resp):
    session = MagicMock()
    session.get.return_value = resp
    with pytest.raises(app.ApiError):
        app.fetch_books_json("dune", session=session)


def test_parse_books_skips_incomplete_docs():
    payload = {"docs": [
        {"title": "Good", "author_name": ["A", "B"], "first_publish_year": 2001, "key": "/w/1"},
        {"title": "", "author_name": ["A"]},
        {"title": "No author"},
        {"title": "Bad year", "author_name": ["A"], "first_publish_year": "abc"},
        "not a dict",
    ]}
    books, skipped = app.parse_books(payload)
    assert [b.title for b in books] == ["Good"]
    assert books[0].author == "A, B"
    assert skipped == 4


# ---- CRUD ---------------------------------------------------------------- #
def test_crud_roundtrip(repo):
    book_id = repo.create(app.make_book("Clean Code", "Robert C. Martin", 2008))
    assert repo.get(book_id).title == "Clean Code"
    updated = repo.update(book_id, publication_year=2009)
    assert updated.publication_year == 2009
    assert len(repo.list()) == 1
    repo.delete(book_id)
    with pytest.raises(app.NotFoundError):
        repo.get(book_id)
    with pytest.raises(app.NotFoundError):
        repo.delete(book_id)


def test_duplicate_create_raises(repo):
    repo.create(app.make_book("T", "A", 2000))
    with pytest.raises(app.ValidationError):
        repo.create(app.make_book("T", "A", 2000))


def test_bulk_upsert_ignores_duplicates(repo):
    books, _ = app.parse_books(SAMPLE)
    assert repo.bulk_upsert(books) == (len(books), 0)
    assert repo.bulk_upsert(books) == (0, len(books))


def test_update_rejects_unknown_columns(repo):
    book_id = repo.create(app.make_book("T", "A", 2000))
    with pytest.raises(app.ValidationError):
        repo.update(book_id, **{"id = 0; --": "x"})


def test_sql_injection_is_stored_literally(repo):
    evil = "x'); DROP TABLE books; --"
    book_id = repo.create(app.make_book(evil, "Mallory", 2020))
    assert repo.get(book_id).title == evil
    assert repo.list(author_contains="' OR '1'='1") == []
    assert len(repo.list()) == 1  # table still exists


def test_like_wildcards_are_escaped(repo):
    repo.create(app.make_book("T1", "Alice", 2000))
    assert repo.list(author_contains="%") == []
    assert len(repo.list(author_contains="lic")) == 1


# ---- CLI ----------------------------------------------------------------- #
def test_cli_add_list_delete(tmp_path: Path, capsys):
    db = str(tmp_path / "b.db")
    assert app.main(["--db", db, "add", "--title", "Dune", "--author", "Frank Herbert",
                     "--year", "1965"]) == 0
    assert app.main(["--db", db, "list"]) == 0
    assert "Dune" in capsys.readouterr().out
    assert app.main(["--db", db, "delete", "1"]) == 0
    assert app.main(["--db", db, "get", "1"]) == 1


def test_cli_fetch_uses_api(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(app, "fetch_books_json", lambda q, n: SAMPLE)
    assert app.main(["--db", str(tmp_path / "b.db"), "fetch", "clean code", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert "3 inserted" in out and "Clean Code" in out
