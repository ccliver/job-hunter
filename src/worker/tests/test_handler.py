"""Tests for the Worker Lambda handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from worker.handler import _make_job_id, handler


def test_make_job_id_is_deterministic() -> None:
    """Same inputs should always produce the same job_id."""
    id1 = _make_job_id("Acme", "Engineer", "https://acme.com/jobs/1")
    id2 = _make_job_id("Acme", "Engineer", "https://acme.com/jobs/1")
    assert id1 == id2


def test_make_job_id_differs_for_different_inputs() -> None:
    """Different inputs should produce different job_ids."""
    id1 = _make_job_id("Acme", "Engineer", "https://acme.com/jobs/1")
    id2 = _make_job_id("Acme", "Engineer", "https://acme.com/jobs/2")
    assert id1 != id2


@pytest.fixture()
def env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBS_TABLE", "test-jobs")
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_MODEL", "anthropic.claude-haiku-4-5-20251001-v1:0")


def _make_sqs_event(company_name: str, careers_url: str) -> dict:
    return {
        "Records": [
            {"body": json.dumps({"company_name": company_name, "careers_url": careers_url})}
        ]
    }


@patch("worker.handler._scrape_jobs", return_value=[])
@patch("worker.handler.dynamodb")
def test_handler_no_jobs_found(
    mock_dynamodb: MagicMock,
    mock_scrape: MagicMock,
    env_vars: None,
) -> None:
    """handler() should return 0 jobs_written when scraper finds nothing."""
    mock_dynamodb.Table.return_value = MagicMock()
    event = _make_sqs_event("Acme Corp", "https://acme.com/jobs")

    result = handler(event, MagicMock())

    assert result["records_processed"] == 1
    assert result["jobs_written"] == 0


@patch("worker.handler._scrape_jobs")
@patch("worker.handler.dynamodb")
def test_handler_writes_new_jobs(
    mock_dynamodb: MagicMock,
    mock_scrape: MagicMock,
    env_vars: None,
) -> None:
    """handler() should write each scraped job to DynamoDB."""
    mock_scrape.return_value = [
        {"title": "SWE", "url": "https://acme.com/jobs/1", "location": "Remote"},
    ]
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    event = _make_sqs_event("Acme Corp", "https://acme.com/jobs")
    result = handler(event, MagicMock())

    assert result["jobs_written"] == 1
    mock_table.put_item.assert_called_once()
