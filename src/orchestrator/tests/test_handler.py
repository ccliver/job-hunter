"""Tests for the Orchestrator Lambda handler."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from orchestrator.handler import handler

REGION = "us-east-1"


@pytest.fixture()
def aws_resources(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        sqs = boto3.client("sqs", region_name=REGION)
        queue_url = sqs.create_queue(QueueName="test-worker-queue")["QueueUrl"]

        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.create_table(
            TableName="test-companies",
            KeySchema=[{"AttributeName": "company_name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "company_name", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        monkeypatch.setenv("COMPANIES_TABLE", "test-companies")
        monkeypatch.setenv("WORKER_QUEUE_URL", queue_url)

        yield {"table": table, "sqs": sqs, "queue_url": queue_url}


def _messages(aws_resources: dict) -> list[dict]:
    raw = aws_resources["sqs"].receive_message(QueueUrl=aws_resources["queue_url"], MaxNumberOfMessages=10)
    return [json.loads(m["Body"]) for m in raw.get("Messages", [])]


def test_handler_publishes_one_message_per_company(aws_resources: dict, lambda_context) -> None:
    """handler() should send one SQS message for each company in the table."""
    aws_resources["table"].put_item(Item={"company_name": "Acme Corp", "careers_url": "https://acme.com/jobs"})
    aws_resources["table"].put_item(Item={"company_name": "Globex", "careers_url": "https://globex.com/careers"})

    result = handler({}, lambda_context)

    assert result["published"] == 2
    assert len(_messages(aws_resources)) == 2


def test_handler_empty_table(aws_resources: dict, lambda_context) -> None:
    """handler() should return 0 published when the companies table is empty."""
    result = handler({}, lambda_context)

    assert result["published"] == 0
    assert _messages(aws_resources) == []


def test_handler_message_body_contains_required_fields(aws_resources: dict, lambda_context) -> None:
    """Each SQS message body should contain company_name, careers_url, and ats."""
    aws_resources["table"].put_item(Item={"company_name": "Acme Corp", "careers_url": "https://acme.com/jobs"})

    handler({}, lambda_context)

    body = _messages(aws_resources)[0]
    assert body["company_name"] == "Acme Corp"
    assert body["careers_url"] == "https://acme.com/jobs"
    assert body["ats"] == "unknown"


def test_handler_message_passes_through_ats_field(aws_resources: dict, lambda_context) -> None:
    """handler() should include the ats value from DynamoDB in the SQS message."""
    aws_resources["table"].put_item(
        Item={"company_name": "Datadog", "careers_url": "https://boards.greenhouse.io/datadog", "ats": "greenhouse"}
    )

    handler({}, lambda_context)

    body = _messages(aws_resources)[0]
    assert body["ats"] == "greenhouse"


def test_handler_missing_ats_defaults_to_unknown(aws_resources: dict, lambda_context) -> None:
    """handler() should default ats to 'unknown' when the field is absent."""
    aws_resources["table"].put_item(Item={"company_name": "Acme Corp", "careers_url": "https://acme.com/jobs"})

    handler({}, lambda_context)

    body = _messages(aws_resources)[0]
    assert body["ats"] == "unknown"
