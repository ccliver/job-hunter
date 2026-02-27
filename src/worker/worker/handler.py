"""Worker Lambda handler.

Triggered by SQS. Each message contains a company name and careers URL.
Uses a Strands agent backed by AWS Bedrock (Claude Haiku) to scrape and
parse the careers page, then writes new job postings to the DynamoDB
`jobs` table. Deduplication is achieved by hashing company+title+url as
the DynamoDB partition key (job_id).

Environment variables expected:
    JOBS_TABLE      - DynamoDB table name for job postings
    BEDROCK_REGION  - AWS region for Bedrock (defaults to us-east-1)
    BEDROCK_MODEL   - Bedrock model ID (defaults to Claude Haiku cross-region inference profile)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from typing import Any

import boto3
import requests
from aws_lambda_powertools import Logger
from bs4 import BeautifulSoup
from strands import Agent
from strands.models import BedrockModel

logger = Logger(service="worker")

dynamodb = boto3.resource("dynamodb")

_DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_DEFAULT_REGION = "us-east-1"
_PAGE_CHAR_LIMIT = 15_000

# Roles the agent is instructed to extract (used in prompt and post-filter).
_TARGET_ROLES = [
    "Platform Engineer",
    "Site Reliability Engineer",
    "SRE",
    "DevOps Engineer",
    "Cloud Engineer",
    "Infrastructure Engineer",
    "Staff Engineer",
]

# Keywords used for post-extraction title matching (case-insensitive).
_TITLE_KEYWORDS = [
    "platform",
    "sre",
    "site reliability",
    "devops",
    "cloud engineer",
    "infrastructure",
    "staff engineer",
]


def _make_job_id(company: str, title: str, url: str) -> str:
    """Derive a stable deduplication key from company, title, and URL."""
    raw = f"{company}|{title}|{url}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _filter_relevant_jobs(jobs: list[dict[str, str]], company: str) -> list[dict[str, str]]:
    """Drop jobs whose title doesn't match any of the target-role keywords.

    Performs case-insensitive substring matching against _TITLE_KEYWORDS.
    Logs extracted vs. matched counts so the keyword list can be tuned.

    Args:
        jobs: Raw list of job dicts with at least a "title" key.
        company: Company name used for structured log context.

    Returns:
        Subset of jobs whose title matched at least one keyword.
    """
    matched = [
        j for j in jobs
        if any(kw in j.get("title", "").lower() for kw in _TITLE_KEYWORDS)
    ]
    logger.info(
        "Job filter complete",
        company=company,
        extracted=len(jobs),
        matched=len(matched),
        dropped=len(jobs) - len(matched),
    )
    return matched


def _scrape_jobs(company_name: str, careers_url: str) -> list[dict[str, str]]:
    """Use a Strands agent to scrape jobs from a careers page.

    Fetches the careers page with requests, strips noise with BeautifulSoup,
    then asks a Bedrock-backed agent to extract structured job listings.

    Returns:
        List of dicts with keys: title, url, location.
    """
    # 1. Fetch the careers page
    try:
        resp = requests.get(
            careers_url,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (compatible; job-hunter/1.0)"},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Failed to fetch careers page", url=careers_url, error=str(exc))
        return []

    # 2. Strip scripts/styles and extract readable text
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    page_text = soup.get_text(separator="\n", strip=True)[:_PAGE_CHAR_LIMIT]

    # 3. Ask the Strands/Bedrock agent to extract structured job listings
    model = BedrockModel(
        model_id=os.environ.get("BEDROCK_MODEL", _DEFAULT_MODEL),
        region_name=os.environ.get("BEDROCK_REGION", _DEFAULT_REGION),
    )
    agent = Agent(model=model)

    target_roles = ", ".join(_TARGET_ROLES)
    prompt = (
        f"You are extracting job listings from the careers page of {company_name} ({careers_url}).\n"
        f"Only extract roles that match these types: {target_roles}.\n"
        "Skip all other roles entirely — do not include them in the output.\n\n"
        "Return a JSON array where each element has exactly these keys:\n"
        '  - "title": job title string\n'
        '  - "url": absolute URL to the job posting '
        f'(fall back to "{careers_url}" if no specific posting URL exists)\n'
        '  - "location": location string, or "Remote" if unspecified\n'
        "Return ONLY the JSON array — no markdown fences, no explanation.\n"
        "If no matching roles are found, return an empty array: []\n\n"
        f"Page content:\n{page_text}"
    )

    response = agent(prompt)
    raw = str(response)

    # 4. Parse the JSON array out of the agent response
    try:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            logger.warning("No JSON array found in agent response", company=company_name)
            return []
        jobs: list[Any] = json.loads(match.group())
        return [
            j
            for j in jobs
            if isinstance(j, dict) and j.get("title") and j.get("url")
        ]
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse agent response as JSON", error=str(exc), company=company_name)
        return []


@logger.inject_lambda_context
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

        logger.info("Processing company", company=company_name, url=careers_url)

        jobs = _filter_relevant_jobs(
            _scrape_jobs(company_name, careers_url),
            company_name,
        )

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
                logger.info("Wrote new job", title=job["title"], company=company_name)
            except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
                logger.debug("Duplicate skipped", job_id=job_id)

        records_processed += 1

    logger.info("Worker done", records_processed=records_processed, jobs_written=jobs_written)
    return {"records_processed": records_processed, "jobs_written": jobs_written}
