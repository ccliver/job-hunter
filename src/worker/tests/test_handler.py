"""Tests for the Worker Lambda handler."""

from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from worker.handler import _make_job_id, handler

REGION = "us-east-1"


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
def aws_resources(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.create_table(
            TableName="test-jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        monkeypatch.setenv("JOBS_TABLE", "test-jobs")
        monkeypatch.setenv("BEDROCK_REGION", REGION)
        monkeypatch.setenv("BEDROCK_MODEL", "anthropic.claude-haiku-4-5-20251001-v1:0")

        yield {"table": table}


def _sqs_event(company_name: str, careers_url: str) -> dict:
    return {
        "Records": [
            {"body": json.dumps({"company_name": company_name, "careers_url": careers_url})}
        ]
    }


@patch("worker.handler._scrape_jobs", return_value=[])
def test_handler_no_jobs_found(mock_scrape, aws_resources: dict) -> None:
    """handler() should return 0 jobs_written when the scraper finds nothing."""
    result = handler(_sqs_event("Acme Corp", "https://acme.com/jobs"), None)

    assert result["records_processed"] == 1
    assert result["jobs_written"] == 0
    assert aws_resources["table"].scan()["Count"] == 0


@patch("worker.handler._scrape_jobs")
def test_handler_writes_new_jobs(mock_scrape, aws_resources: dict) -> None:
    """handler() should write each scraped job to DynamoDB."""
    mock_scrape.return_value = [
        {"title": "SWE", "url": "https://acme.com/jobs/1", "location": "Remote"},
    ]

    result = handler(_sqs_event("Acme Corp", "https://acme.com/jobs"), None)

    assert result["jobs_written"] == 1
    items = aws_resources["table"].scan()["Items"]
    assert len(items) == 1
    assert items[0]["title"] == "SWE"
    assert items[0]["company"] == "Acme Corp"
    assert items[0]["location"] == "Remote"
    assert "discovered_at" in items[0]


@patch("worker.handler._scrape_jobs")
def test_handler_deduplicates_jobs(mock_scrape, aws_resources: dict) -> None:
    """Calling handler twice with the same job should only write it once."""
    mock_scrape.return_value = [
        {"title": "SWE", "url": "https://acme.com/jobs/1", "location": "Remote"},
    ]
    event = _sqs_event("Acme Corp", "https://acme.com/jobs")

    first = handler(event, None)
    second = handler(event, None)

    assert first["jobs_written"] == 1
    assert second["jobs_written"] == 0
    assert aws_resources["table"].scan()["Count"] == 1
