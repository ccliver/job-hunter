"""Tests for the Notifier Lambda handler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from notifier.handler import _build_email_body, handler


@pytest.fixture()
def env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBS_TABLE", "test-jobs")
    monkeypatch.setenv("SES_FROM_ADDRESS", "noreply@example.com")
    monkeypatch.setenv("SES_TO_ADDRESS", "me@example.com")
    monkeypatch.setenv("LOOKBACK_MINUTES", "60")
    monkeypatch.setenv("SES_REGION", "us-east-1")


def test_build_email_body_contains_job_info() -> None:
    """Email body should include job title, company, and URL."""
    jobs = [{"title": "SWE", "company": "Acme", "url": "https://acme.com/1", "location": "Remote"}]
    text, html = _build_email_body(jobs)

    assert "SWE" in text
    assert "Acme" in text
    assert "https://acme.com/1" in text
    assert "SWE" in html
    assert "https://acme.com/1" in html


@patch("notifier.handler._query_recent_jobs", return_value=[])
@patch("notifier.handler.dynamodb")
def test_handler_no_jobs_skips_email(
    mock_dynamodb: MagicMock,
    mock_query: MagicMock,
    env_vars: None,
) -> None:
    """handler() should not call SES when no recent jobs are found."""
    mock_dynamodb.Table.return_value = MagicMock()

    with patch("notifier.handler.boto3") as mock_boto3:
        result = handler({}, MagicMock())
        mock_boto3.client.assert_not_called()

    assert result["jobs_emailed"] == 0


@patch("notifier.handler._query_recent_jobs")
@patch("notifier.handler.dynamodb")
def test_handler_sends_email_when_jobs_found(
    mock_dynamodb: MagicMock,
    mock_query: MagicMock,
    env_vars: None,
) -> None:
    """handler() should call SES send_email when recent jobs exist."""
    mock_query.return_value = [
        {"title": "SWE", "company": "Acme", "url": "https://acme.com/1", "location": "Remote"},
    ]
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table
    mock_ses = MagicMock()

    with patch("notifier.handler.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_ses
        result = handler({}, MagicMock())

    mock_ses.send_email.assert_called_once()
    assert result["jobs_emailed"] == 1
