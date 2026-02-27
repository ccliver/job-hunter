"""Tests for the Notifier Lambda handler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from notifier.handler import _build_email_body, handler

REGION = "us-east-1"
FROM_ADDRESS = "noreply@example.com"
TO_ADDRESS = "me@example.com"


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

        ses = boto3.client("ses", region_name=REGION)
        ses.verify_email_identity(EmailAddress=FROM_ADDRESS)
        ses.verify_email_identity(EmailAddress=TO_ADDRESS)

        monkeypatch.setenv("JOBS_TABLE", "test-jobs")
        monkeypatch.setenv("SES_FROM_ADDRESS", FROM_ADDRESS)
        monkeypatch.setenv("SES_TO_ADDRESS", TO_ADDRESS)
        monkeypatch.setenv("LOOKBACK_MINUTES", "60")
        monkeypatch.setenv("SES_REGION", REGION)

        yield {"table": table, "ses": ses}


def _recent_job(job_id: str, title: str, minutes_ago: int = 5) -> dict:
    return {
        "job_id": job_id,
        "company": "Acme",
        "title": title,
        "url": f"https://acme.com/{job_id}",
        "location": "Remote",
        "discovered_at": (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(),
    }


def test_build_email_body_contains_job_info() -> None:
    """Email body should include job title, company, and URL."""
    jobs = [{"title": "SWE", "company": "Acme", "url": "https://acme.com/1", "location": "Remote"}]
    text, html = _build_email_body(jobs)

    assert "SWE" in text
    assert "Acme" in text
    assert "https://acme.com/1" in text
    assert "SWE" in html
    assert "https://acme.com/1" in html


def test_handler_no_jobs_skips_email(aws_resources: dict, lambda_context) -> None:
    """handler() should not send an email when no recent jobs are found."""
    result = handler({}, lambda_context)

    assert result["jobs_emailed"] == 0
    send_stats = aws_resources["ses"].get_send_statistics()
    delivery_attempts = sum(p["DeliveryAttempts"] for p in send_stats["SendDataPoints"])
    assert delivery_attempts == 0


def test_handler_sends_email_when_jobs_found(aws_resources: dict, lambda_context) -> None:
    """handler() should send one SES email when recent jobs exist."""
    aws_resources["table"].put_item(Item=_recent_job("job-1", "SWE"))

    result = handler({}, lambda_context)

    assert result["jobs_emailed"] == 1
    send_stats = aws_resources["ses"].get_send_statistics()
    assert send_stats["SendDataPoints"] != []


def test_handler_ignores_old_jobs(aws_resources: dict, lambda_context) -> None:
    """handler() should not email jobs outside the lookback window."""
    aws_resources["table"].put_item(Item=_recent_job("job-old", "SWE", minutes_ago=90))

    result = handler({}, lambda_context)

    assert result["jobs_emailed"] == 0


def test_handler_emails_all_recent_jobs(aws_resources: dict, lambda_context) -> None:
    """handler() should include all jobs within the lookback window in one email."""
    aws_resources["table"].put_item(Item=_recent_job("job-1", "SWE"))
    aws_resources["table"].put_item(Item=_recent_job("job-2", "SRE"))

    result = handler({}, lambda_context)

    assert result["jobs_emailed"] == 2
