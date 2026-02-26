"""Worker Lambda handler.

Triggered by SQS. Each message contains a company name and careers URL.
Uses a Strands agent backed by AWS Bedrock (Claude Haiku) to scrape and
parse the careers page, then writes new job postings to the DynamoDB
`jobs` table. Deduplication is achieved by hashing company+title+url as
the DynamoDB partition key (job_id).

Environment variables expected:
    JOBS_TABLE      - DynamoDB table name for job postings
    BEDROCK_REGION  - AWS region for Bedrock (defaults to us-east-1)
    BEDROCK_MODEL   - Bedrock model ID (defaults to Claude Haiku)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")


def _make_job_id(company: str, title: str, url: str) -> str:
    """Derive a stable deduplication key from company, title, and URL."""
    raw = f"{company}|{title}|{url}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _scrape_jobs(company_name: str, careers_url: str) -> list[dict[str, str]]:
    """Use a Strands agent to scrape jobs from a careers page.

    TODO: implement Strands agent with Bedrock tool calls.

    Returns:
        List of dicts with keys: title, url, location.
    """
    # Placeholder - replace with actual Strands agent invocation
    return []


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point for the Worker Lambda.

    Processes each SQS record, scrapes the company's careers page via a
    Strands agent, and persists any new job postings to DynamoDB.

    Args:
        event: SQS event containing one or more Records.
        context: Lambda context object (unused).

    Returns:
        A summary dict with counts of records processed and jobs written.
    """
    jobs_table_name = os.environ["JOBS_TABLE"]
    table = dynamodb.Table(jobs_table_name)

    records_processed = 0
    jobs_written = 0

    for record in event.get("Records", []):
        body = json.loads(record["body"])
        company_name: str = body["company_name"]
        careers_url: str = body["careers_url"]

        logger.info("Processing company: %s (%s)", company_name, careers_url)

        jobs = _scrape_jobs(company_name, careers_url)

        for job in jobs:
            job_id = _make_job_id(company_name, job["title"], job["url"])
            item = {
                "job_id": job_id,
                "company": company_name,
                "title": job["title"],
                "url": job["url"],
                "location": job.get("location", ""),
                "discovered_at": datetime.now(UTC).isoformat(),
            }
            # condition_expression prevents overwriting existing items
            try:
                table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(job_id)",
                )
                jobs_written += 1
                logger.info("Wrote new job: %s at %s", job["title"], company_name)
            except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
                logger.debug("Duplicate skipped: %s", job_id)

        records_processed += 1

    logger.info("Worker done. records=%d new_jobs=%d", records_processed, jobs_written)
    return {"records_processed": records_processed, "jobs_written": jobs_written}
