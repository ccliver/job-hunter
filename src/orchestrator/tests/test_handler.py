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


def test_handler_publishes_one_message_per_company(aws_resources: dict) -> None:
    """handler() should send one SQS message for each company in the table."""
    aws_resources["table"].put_item(
        Item={"company_name": "Acme Corp", "careers_url": "https://acme.com/jobs"}
    )
    aws_resources["table"].put_item(
        Item={"company_name": "Globex", "careers_url": "https://globex.com/careers"}
    )

    result = handler({}, None)

    assert result["published"] == 2
    messages = (
        aws_resources["sqs"]
        .receive_message(QueueUrl=aws_resources["queue_url"], MaxNumberOfMessages=10)
        .get("Messages", [])
    )
    assert len(messages) == 2


def test_handler_empty_table(aws_resources: dict) -> None:
    """handler() should return 0 published when the companies table is empty."""
    result = handler({}, None)

    assert result["published"] == 0
    messages = (
        aws_resources["sqs"]
        .receive_message(QueueUrl=aws_resources["queue_url"], MaxNumberOfMessages=10)
        .get("Messages", [])
    )
    assert len(messages) == 0


def test_handler_message_body_shape(aws_resources: dict) -> None:
    """Each SQS message body should contain company_name and careers_url."""
    aws_resources["table"].put_item(
        Item={"company_name": "Acme Corp", "careers_url": "https://acme.com/jobs"}
    )

    handler({}, None)

    messages = (
        aws_resources["sqs"]
        .receive_message(QueueUrl=aws_resources["queue_url"], MaxNumberOfMessages=1)
        .get("Messages", [])
    )
    body = json.loads(messages[0]["Body"])
    assert body["company_name"] == "Acme Corp"
    assert body["careers_url"] == "https://acme.com/jobs"
