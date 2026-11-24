"""Security tests for the Qdrant ACL demo (skipped if qdrant-client is not installed)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("qdrant_client")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "assignment-2-ai-llm"))
import qdrant_acl_demo as demo  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return demo.setup()


def test_engineer_cannot_see_hr_or_exec_docs(client):
    titles = demo.secure_search(client, {"groups": ["grp-all-employees", "grp-eng"],
                                         "clearance_level": 1})
    assert "Executive salary bands" not in titles
    assert "M&A target shortlist" not in titles
    assert "XR-200 error E-4021 fix" in titles


def test_group_membership_is_not_enough_without_clearance(client):
    titles = demo.secure_search(client, {"groups": ["grp-hr"], "clearance_level": 2})
    assert "Executive salary bands" not in titles


def test_no_groups_means_no_results(client):
    assert demo.secure_search(client, {"groups": [], "clearance_level": 4}) == []
