"""Tests for the Task 4 (async pipeline) and Task 5 (SQL) demonstrations."""

from __future__ import annotations

import asyncio
import random
import sqlite3
from pathlib import Path

import pytest

import advanced_python_example as ape

SQL_FILE = Path(ape.__file__).parent / "advanced_database.sql"


# ---- Task 5: SQL ---------------------------------------------------------- #
def run_sql_file() -> tuple[sqlite3.Connection, list[tuple[str, list[tuple]]]]:
    """Execute the SQL file statement by statement, collecting result sets."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    results, buf = [], ""
    for line in SQL_FILE.read_text(encoding="utf-8").splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt, buf = buf.strip(), ""
            results.append((stmt, conn.execute(stmt).fetchall()))
    return conn, results


def test_sql_file_executes_and_is_idempotent():
    conn, _ = run_sql_file()
    conn.executescript(SQL_FILE.read_text(encoding="utf-8"))  # DROP IF EXISTS makes it rerunnable
    assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 8


def test_missing_documents_query():
    _, results = run_sql_file()
    missing = next(rows for stmt, rows in results if "missing_document" in stmt)
    assert missing == [(102, "insurance_quote", "consent_form"),
                       (103, "loan_application", "income_proof"),
                       (105, "loan_application", "consent_form"),
                       (105, "loan_application", "income_proof")]


def test_trigger_and_transaction_effects():
    conn, _ = run_sql_file()
    status = dict(conn.execute("SELECT call_id, status FROM calls").fetchall())
    assert status[105] == "non_compliant"   # set by trigger (critical finding)
    assert status[107] == "compliant"       # resolved inside the transaction


def test_constraints_reject_bad_data():
    conn, _ = run_sql_file()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO customers (phone_e164) VALUES ('98450')")
    with pytest.raises(sqlite3.IntegrityError):   # FK
        conn.execute("INSERT INTO calls (provider, provider_call_ref, customer_id, agent_id, "
                     "intent, started_at, duration_sec) VALUES "
                     "('twilio','X',999,1,'unknown','2026-11-01 10:00:00',10)")
    with pytest.raises(sqlite3.IntegrityError):   # webhook idempotency
        conn.execute("INSERT INTO calls (provider, provider_call_ref, customer_id, agent_id, "
                     "intent, started_at, duration_sec) VALUES "
                     "('twilio','CA001',1,1,'unknown','2026-11-01 10:00:00',10)")


def test_query_plans_use_indexes():
    _, results = run_sql_file()
    plans = [rows for stmt, rows in results if "EXPLAIN QUERY PLAN" in stmt]
    details = " | ".join(str(r[-1]) for rows in plans for r in rows)
    assert "ix_calls_agent_started" in details
    assert "ix_findings_open_severe" in details


# ---- Task 4: async pipeline ---------------------------------------------- #
def test_normalise_all_providers():
    tw = ape.normalise_payload("twilio", {"CallSid": "1", "From": "+919845000001",
                                          "AgentId": "A", "TranscriptionText": "a" * 20})
    vn = ape.normalise_payload("vonage", {"uuid": "1", "from": {"number": "919845000001"},
                                          "agent": {"id": "A"}, "transcript": "a" * 20})
    rc = ape.normalise_payload("ringcentral", {"session": {"id": "1", "caller": {
        "phoneNumber": "+919845000001"}, "agentExtension": "A"}, "utterances": ["a" * 20]})
    assert tw.customer_phone == vn.customer_phone == rc.customer_phone
    assert len({tw.idempotency_key, vn.idempotency_key, rc.idempotency_key}) == 3


@pytest.mark.parametrize("provider,payload", [
    ("zoom", {}),
    ("twilio", {"CallSid": "1"}),
    ("twilio", {"CallSid": "1", "From": "12345", "AgentId": "A", "TranscriptionText": "a" * 20}),
])
def test_normalise_rejects_bad_payloads(provider, payload):
    with pytest.raises(ape.PayloadValidationError):
        ape.normalise_payload(provider, payload)


def test_pii_masking():
    masked = ape.mask_pii("mail me at a.b@example.com or +91 98450 12345, PAN ABCDE1234F")
    assert "example.com" not in masked and "12345" not in masked and "ABCDE" not in masked


def test_compliance_rules():
    call = ape.CallRecord(ape.Provider.TWILIO, "1", "+919845000001", "A",
                          "we offer guaranteed returns on this loan", frozenset({"kyc_id"}))
    ext = ape.Extraction(ape.Intent.LOAN_APPLICATION, None, [], 0.9)
    res = ape.evaluate_compliance(call, ext)
    assert res.missing_documents == ["consent_form", "income_proof"]
    assert res.prohibited_phrases == ["guaranteed returns"]
    assert res.needs_human_review and not res.compliant


def test_retry_eventually_succeeds_and_gives_up():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ape.TransientError("x")
        return "ok"

    assert asyncio.run(ape.retry_async(flaky, base_delay=0, rng=random.Random(1))) == "ok"

    async def always_fail():
        raise ape.TransientError("x")

    with pytest.raises(ape.TransientError):
        asyncio.run(ape.retry_async(always_fail, attempts=2, base_delay=0))


def test_circuit_breaker_opens():
    async def scenario():
        breaker = ape.CircuitBreaker(failure_threshold=2, reset_timeout=10)

        async def fail():
            raise ape.TransientError("down")

        for _ in range(2):
            with pytest.raises(ape.TransientError):
                await breaker.call(fail)
        with pytest.raises(ape.CircuitOpenError):
            await breaker.call(fail)
        return breaker.trips

    assert asyncio.run(scenario()) == 1


def test_pipeline_end_to_end_is_idempotent():
    stats = asyncio.run(ape.main_async(n_calls=30, seed=3))
    assert stats.outcomes["duplicate_skipped"] + stats.outcomes["dead_lettered"] >= 1
    assert stats.outcomes["rejected_invalid"] == 2
    assert sum(stats.outcomes.values()) == 30 + 5 + 2
