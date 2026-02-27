"""Notifier Lambda handler.

Triggered by EventBridge cron, 30 minutes after the Orchestrator.
Queries the DynamoDB `jobs` table for postings discovered in the last N
minutes and sends a single SES email digest to the configured recipient.

Environment variables expected:
    JOBS_TABLE          - DynamoDB table name for job postings
    SES_FROM_ADDRESS    - Verified SES sender email address
    SES_TO_ADDRESS      - Recipient email address
    LOOKBACK_MINUTES    - How far back to query for new jobs (default: 60)
    SES_REGION          - AWS region for SES (defaults to us-east-1)
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Attr

logger = Logger(service="notifier")

dynamodb = boto3.resource("dynamodb")


def _query_recent_jobs(table: Any, lookback_minutes: int) -> list[dict[str, str]]:
    """Scan jobs table for items discovered within the lookback window.

    TODO: add a GSI on discovered_at for efficient time-range queries
    instead of a full table scan.

    Args:
        table: boto3 DynamoDB Table resource.
        lookback_minutes: Number of minutes to look back.

    Returns:
        List of job item dicts.
    """
    cutoff = (datetime.now(UTC) - timedelta(minutes=lookback_minutes)).isoformat()
    response = table.scan(
        FilterExpression=Attr("discovered_at").gte(cutoff),
    )
    return response.get("Items", [])


def _build_email_body(jobs: list[dict[str, str]]) -> tuple[str, str]:
    """Render plain-text and HTML email bodies from a list of job dicts.

    Returns:
        Tuple of (text_body, html_body).
    """
    # TODO: implement proper HTML template
    lines = [f"- {j['title']} at {j['company']} | {j['url']}" for j in jobs]
    text_body = "New job postings found:\n\n" + "\n".join(lines)
    html_body = (
        "<p>New job postings found:</p><ul>"
        + "".join(f"<li><a href='{j['url']}'>{j['title']}</a> at {j['company']}</li>" for j in jobs)
        + "</ul>"
    )
    return text_body, html_body


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point for the Notifier Lambda.

    Queries recent jobs and sends an SES email digest if any were found.

    Args:
        event: EventBridge scheduled event payload (unused).
        context: Lambda context object (unused).

    Returns:
        A summary dict with the count of jobs emailed.
    """
    jobs_table_name = os.environ["JOBS_TABLE"]
    from_address = os.environ["SES_FROM_ADDRESS"]
    to_address = os.environ["SES_TO_ADDRESS"]
    lookback_minutes = int(os.environ.get("LOOKBACK_MINUTES", "60"))
    ses_region = os.environ.get("SES_REGION", "us-east-1")

    table = dynamodb.Table(jobs_table_name)
    jobs = _query_recent_jobs(table, lookback_minutes)

    if not jobs:
        logger.info("No new jobs found", lookback_minutes=lookback_minutes)
        return {"jobs_emailed": 0}

    ses = boto3.client("ses", region_name=ses_region)
    text_body, html_body = _build_email_body(jobs)

    ses.send_email(
        Source=from_address,
        Destination={"ToAddresses": [to_address]},
        Message={
            "Subject": {"Data": f"Job Hunter: {len(jobs)} new posting(s) found"},
            "Body": {
                "Text": {"Data": text_body},
                "Html": {"Data": html_body},
            },
        },
    )

    logger.info("Sent digest", job_count=len(jobs), recipient=to_address)
    return {"jobs_emailed": len(jobs)}
