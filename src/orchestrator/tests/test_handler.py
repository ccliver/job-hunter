"""Tests for the Orchestrator Lambda handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.handler import handler


@pytest.fixture()
def env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANIES_TABLE", "test-companies")
    monkeypatch.setenv("WORKER_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")


@patch("orchestrator.handler.sqs")
@patch("orchestrator.handler.dynamodb")
def test_handler_publishes_one_message_per_company(
    mock_dynamodb: MagicMock,
    mock_sqs: MagicMock,
    env_vars: None,
) -> None:
    """handler() should send one SQS message for each company in the table."""
    mock_table = MagicMock()
    mock_table.scan.return_value = {
        "Items": [
            {"company_name": "Acme Corp", "careers_url": "https://acme.com/jobs"},
            {"company_name": "Globex", "careers_url": "https://globex.com/careers"},
        ]
    }
    mock_dynamodb.Table.return_value = mock_table

    result = handler({}, MagicMock())

    assert result["published"] == 2
    assert mock_sqs.send_message.call_count == 2


@patch("orchestrator.handler.sqs")
@patch("orchestrator.handler.dynamodb")
def test_handler_empty_table(
    mock_dynamodb: MagicMock,
    mock_sqs: MagicMock,
    env_vars: None,
) -> None:
    """handler() should return 0 published when the companies table is empty."""
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": []}
    mock_dynamodb.Table.return_value = mock_table

    result = handler({}, MagicMock())

    assert result["published"] == 0
    mock_sqs.send_message.assert_not_called()


@patch("orchestrator.handler.sqs")
@patch("orchestrator.handler.dynamodb")
def test_handler_message_body_shape(
    mock_dynamodb: MagicMock,
    mock_sqs: MagicMock,
    env_vars: None,
) -> None:
    """Each SQS message body should contain company_name and careers_url."""
    mock_table = MagicMock()
    mock_table.scan.return_value = {
        "Items": [{"company_name": "Acme Corp", "careers_url": "https://acme.com/jobs"}]
    }
    mock_dynamodb.Table.return_value = mock_table

    handler({}, MagicMock())

    call_kwargs = mock_sqs.send_message.call_args.kwargs
    body = json.loads(call_kwargs["MessageBody"])
    assert body["company_name"] == "Acme Corp"
    assert body["careers_url"] == "https://acme.com/jobs"
