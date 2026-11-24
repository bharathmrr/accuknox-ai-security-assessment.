"""Tests for student_scores.py."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

import student_scores as ss


@pytest.mark.parametrize("value,expected", [
    (88, 88.0), ("84", 84.0), (79.5, 79.5), (0, 0.0), (100, 100.0)])
def test_clean_score_valid(value, expected):
    assert ss.clean_score(value) == (expected, None)


@pytest.mark.parametrize("value,reason", [
    (None, "missing"), ("absent", "invalid"), (105, "out of range"), (-5, "out of range"),
    (True, "invalid"), (math.nan, "invalid"), ([1], "invalid")])
def test_clean_score_invalid(value, reason):
    score, problem = ss.clean_score(value)
    assert score is None and reason in problem


def _payload():
    return {"subjects": ["A", "B"], "students": [
        {"id": "1", "name": "Top", "scores": {"A": 90, "B": 100}},
        {"id": "2", "name": "Tie", "scores": {"A": 95, "B": 95}},
        {"id": "3", "name": "Low", "scores": {"A": 40, "B": None}},
        {"id": "4", "name": "", "scores": {"A": 10, "B": 10}},
        {"id": "5", "name": "Nothing", "scores": {"A": "x"}},
    ]}


def test_summary_handles_ties_missing_and_exclusions():
    results, subjects = ss.process_students(_payload())
    summary = ss.summarise(results, subjects)
    assert {r.name for r in summary.highest} == {"Top", "Tie"}
    assert [r.name for r in summary.lowest] == ["Low"]
    assert {r.student_id for r in summary.excluded} == {"4", "5"}
    # overall = mean of valid scores of rankable students: 90,100,95,95,40
    assert summary.overall_average == pytest.approx(84.0)
    assert summary.subject_averages == {"A": 75.0, "B": pytest.approx(97.5)}


def test_subjects_derived_when_missing():
    payload = {"students": [{"id": "1", "name": "X", "scores": {"M": 1, "P": 2}}]}
    _, subjects = ss.process_students(payload)
    assert subjects == ["M", "P"]


def test_empty_class():
    summary = ss.summarise([], ["A"])
    assert summary.overall_average is None and summary.highest == []


def test_load_source_errors(tmp_path: Path):
    with pytest.raises(ss.DataSourceError):
        ss.load_source(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ss.DataSourceError):
        ss.load_source(bad)
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"x": 1}), encoding="utf-8")
    with pytest.raises(ss.DataSourceError):
        ss.load_source(wrong)


def test_load_source_http(monkeypatch):
    resp = MagicMock()
    resp.json.return_value = _payload()
    monkeypatch.setattr(ss.requests, "get", MagicMock(return_value=resp))
    assert ss.load_source("https://mock.local/scores")["students"]
    monkeypatch.setattr(ss.requests, "get", MagicMock(side_effect=requests.Timeout()))
    with pytest.raises(ss.DataSourceError, match="Timed out"):
        ss.load_source("https://mock.local/scores")


def test_full_run_writes_csv_and_chart(tmp_path: Path):
    results, summary = ss.run(ss.DEFAULT_SOURCE, tmp_path)
    assert summary.highest[0].name == "Diya Patel"
    assert summary.lowest[0].name == "Karthik Reddy"
    png = tmp_path / "student_averages.png"
    assert png.exists() and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    with (tmp_path / "student_results.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(results) == 10
    meera = next(r for r in rows if r["name"] == "Meera Nair")
    assert meera["Programming"] == "" and "out of range" in meera["issues"]
