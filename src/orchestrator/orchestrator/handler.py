"""Orchestrator Lambda handler.

Triggered by EventBridge cron. Scans the DynamoDB `companies` table and
publishes one SQS message per company so the Worker Lambda can scrape each
careers page independently.

Environment variables expected:
    COMPANIES_TABLE  - DynamoDB table name for companies
    WORKER_QUEUE_URL - SQS queue URL that triggers the Worker Lambda
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
from aws_lambda_powertools import Logger

logger = Logger(service="orchestrator")

dynamodb = boto3.resource("dynamodb")
sqs = boto3.client("sqs")


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point for the Orchestrator Lambda.

    Scans the companies table and sends one SQS message per company.

    Args:
        event: EventBridge scheduled event payload (unused).
        context: Lambda context object (unused).

    Returns:
        A summary dict with the count of messages published.
    """
    companies_table_name = os.environ["COMPANIES_TABLE"]
    queue_url = os.environ["WORKER_QUEUE_URL"]

    table = dynamodb.Table(companies_table_name)

    # TODO: implement pagination for large company lists
    response = table.scan()
    companies = response.get("Items", [])

    published = 0
    for company in companies:
        message = {
            "company_name": company["company_name"],
            "careers_url": company["careers_url"],
        }
        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message),
        )
        published += 1
        logger.info("Queued company", company=company["company_name"])

    logger.info("Orchestrator published messages", count=published)
    return {"published": published}
