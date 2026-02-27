"""Tests for the Worker Lambda handler."""

from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from worker.handler import _filter_relevant_jobs, _make_job_id, handler

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
def test_handler_no_jobs_found(mock_scrape, aws_resources: dict, lambda_context) -> None:
    """handler() should return 0 jobs_written when the scraper finds nothing."""
    result = handler(_sqs_event("Acme Corp", "https://acme.com/jobs"), lambda_context)

    assert result["records_processed"] == 1
    assert result["jobs_written"] == 0
    assert aws_resources["table"].scan()["Count"] == 0


@patch("worker.handler._scrape_jobs")
def test_handler_writes_new_jobs(mock_scrape, aws_resources: dict, lambda_context) -> None:
    """handler() should write each scraped job that passes the title filter."""
    mock_scrape.return_value = [
        {"title": "Platform Engineer", "url": "https://acme.com/jobs/1", "location": "Remote"},
    ]

    result = handler(_sqs_event("Acme Corp", "https://acme.com/jobs"), lambda_context)

    assert result["jobs_written"] == 1
    items = aws_resources["table"].scan()["Items"]
    assert len(items) == 1
    assert items[0]["title"] == "Platform Engineer"
    assert items[0]["company"] == "Acme Corp"
    assert items[0]["location"] == "Remote"
    assert "discovered_at" in items[0]


@patch("worker.handler._scrape_jobs")
def test_handler_deduplicates_jobs(mock_scrape, aws_resources: dict, lambda_context) -> None:
    """Calling handler twice with the same job should only write it once."""
    mock_scrape.return_value = [
        {"title": "Platform Engineer", "url": "https://acme.com/jobs/1", "location": "Remote"},
    ]
    event = _sqs_event("Acme Corp", "https://acme.com/jobs")

    first = handler(event, lambda_context)
    second = handler(event, lambda_context)

    assert first["jobs_written"] == 1
    assert second["jobs_written"] == 0
    assert aws_resources["table"].scan()["Count"] == 1


@patch("worker.handler._scrape_jobs")
def test_handler_drops_irrelevant_jobs(mock_scrape, aws_resources: dict, lambda_context) -> None:
    """handler() should not write jobs whose title doesn't match target keywords."""
    mock_scrape.return_value = [
        {"title": "Software Engineer", "url": "https://acme.com/jobs/1", "location": "Remote"},
        {"title": "Product Manager", "url": "https://acme.com/jobs/2", "location": "Remote"},
    ]

    result = handler(_sqs_event("Acme Corp", "https://acme.com/jobs"), lambda_context)

    assert result["jobs_written"] == 0
    assert aws_resources["table"].scan()["Count"] == 0


# --- _filter_relevant_jobs unit tests ---

def _job(title: str) -> dict:
    return {"title": title, "url": f"https://example.com/{title}", "location": "Remote"}


@pytest.mark.parametrize("title", [
    "Platform Engineer",
    "Senior Platform Engineer",
    "Staff Engineer, Infrastructure",
    "Site Reliability Engineer",
    "SRE - Production",
    "Sr. SRE",
    "DevOps Engineer",
    "Lead DevOps Engineer",
    "Cloud Engineer",
    "Senior Cloud Engineer",
    "Infrastructure Engineer",
    "Staff Engineer",
])
def test_filter_passes_relevant_titles(title: str) -> None:
    """_filter_relevant_jobs should keep titles matching a target keyword."""
    result = _filter_relevant_jobs([_job(title)], "Acme")
    assert len(result) == 1


@pytest.mark.parametrize("title", [
    "Software Engineer",
    "Product Manager",
    "Data Scientist",
    "Frontend Developer",
    "Sales Engineer",
    "Recruiting Coordinator",
])
def test_filter_drops_irrelevant_titles(title: str) -> None:
    """_filter_relevant_jobs should drop titles that don't match any keyword."""
    result = _filter_relevant_jobs([_job(title)], "Acme")
    assert len(result) == 0


def test_filter_mixed_batch_keeps_only_matches() -> None:
    """_filter_relevant_jobs should keep only the matching subset of a mixed list."""
    jobs = [
        _job("Platform Engineer"),
        _job("Software Engineer"),
        _job("DevOps Engineer"),
        _job("Product Manager"),
    ]
    result = _filter_relevant_jobs(jobs, "Acme")
    assert len(result) == 2
    titles = {j["title"] for j in result}
    assert titles == {"Platform Engineer", "DevOps Engineer"}


def test_filter_empty_input_returns_empty() -> None:
    """_filter_relevant_jobs should handle an empty input list gracefully."""
    assert _filter_relevant_jobs([], "Acme") == []
