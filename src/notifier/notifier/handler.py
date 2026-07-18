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
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from html import escape
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
    """Render plain-text and HTML email bodies from a list of job dicts, grouped by company.

    Returns:
        Tuple of (text_body, html_body).
    """
    by_company: dict[str, list[dict[str, str]]] = defaultdict(list)
    for job in jobs:
        by_company[job["company"]].append(job)

    date_str = datetime.now(UTC).strftime("%B %-d, %Y")
    header = f"Job Hunter Digest — {len(jobs)} new posting(s), {date_str}"

    text_sections = []
    html_sections = []
    for company in sorted(by_company):
        company_jobs = by_company[company]

        text_lines = []
        html_rows = []
        for job in company_jobs:
            location = job.get("location", "").strip()
            text_lines.append(f"  - {job['title']}" + (f" ({location})" if location else "") + f"\n    {job['url']}")
            location_html = (
                f'<p style="margin:4px 0 0;font-size:13px;color:#8a8a9e;">{escape(location)}</p>' if location else ""
            )
            html_rows.append(
                f'<div style="padding:12px 0;border-bottom:1px solid #eeeef2;">'
                f'<a href="{escape(job["url"])}" '
                f'style="font-size:15px;font-weight:600;color:#3454d1;text-decoration:none;">'
                f"{escape(job['title'])}</a>{location_html}</div>"
            )

        text_sections.append(f"{company} ({len(company_jobs)})\n" + "\n".join(text_lines))
        html_sections.append(
            f'<div style="margin-top:24px;">'
            f'<p style="margin:0 0 4px;font-size:13px;font-weight:600;color:#6b6b80;'
            f'text-transform:uppercase;letter-spacing:0.05em;">'
            f"{escape(company)} &middot; {len(company_jobs)}</p>"
            f"{''.join(html_rows)}</div>"
        )

    text_body = header + "\n\n" + "\n\n".join(text_sections)

    html_body = (
        '<!DOCTYPE html><html><body style="margin:0;padding:0;background-color:#f4f4f7;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;\">"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background-color:#f4f4f7;padding:24px 0;"><tr><td align="center">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="background-color:#ffffff;border-radius:8px;overflow:hidden;max-width:600px;">'
        '<tr><td style="background-color:#1a1a2e;padding:24px 32px;">'
        f'<p style="margin:0;color:#ffffff;font-size:20px;font-weight:600;">Job Hunter Digest</p>'
        f'<p style="margin:4px 0 0;color:#a0a0b8;font-size:13px;">'
        f"{len(jobs)} new posting(s) &middot; {date_str}</p></td></tr>"
        f'<tr><td style="padding:8px 32px 32px;">{"".join(html_sections)}</td></tr>'
        "</table></td></tr></table></body></html>"
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
