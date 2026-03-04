"""Worker Lambda handler.

Triggered by SQS. Each message contains a company name, careers URL, and
optional ATS type. Dispatches to the appropriate ATS handler to fetch and
parse job listings, applies a keyword filter, then writes new postings to
the DynamoDB `jobs` table. Deduplication is achieved by hashing
company+title+url as the DynamoDB partition key (job_id).

ATS backends:
    greenhouse - JSON API; no LLM required
    lever      - JSON API; no LLM required
    unknown    - Custom careers page; uses Strands/Bedrock (Claude Haiku)

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
from playwright.sync_api import sync_playwright
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
    matched = [j for j in jobs if any(kw in j.get("title", "").lower() for kw in _TITLE_KEYWORDS)]
    logger.info(
        "Job filter complete",
        company=company,
        extracted=len(jobs),
        matched=len(matched),
        dropped=len(jobs) - len(matched),
    )
    return matched


def _fetch_greenhouse_jobs(careers_url: str) -> list[dict[str, str]]:
    """Fetch job listings from a Greenhouse JSON API endpoint.

    Args:
        careers_url: Greenhouse board API URL (already returns JSON).

    Returns:
        Normalised list of job dicts with title, url, location keys.
    """
    try:
        resp = requests.get(careers_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Greenhouse fetch failed", url=careers_url, error=str(exc))
        return []

    try:
        data = resp.json()
    except requests.exceptions.JSONDecodeError:
        logger.warning(
            "Greenhouse response is not JSON — careers_url must be the board API endpoint "
            "(e.g. https://boards-api.greenhouse.io/v1/boards/{slug}/jobs), not the human-facing page",
            url=careers_url,
        )
        return []

    jobs = []
    for posting in data.get("jobs", []):
        jobs.append(
            {
                "title": posting.get("title", ""),
                "url": posting.get("absolute_url", careers_url),
                "location": posting.get("location", {}).get("name", ""),
            }
        )
    logger.info("Greenhouse jobs fetched", url=careers_url, count=len(jobs))
    return jobs


def _fetch_lever_jobs(careers_url: str) -> list[dict[str, str]]:
    """Fetch job listings from a Lever JSON API endpoint.

    Args:
        careers_url: Lever postings API URL (already returns JSON).

    Returns:
        Normalised list of job dicts with title, url, location keys.
    """
    try:
        resp = requests.get(careers_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Lever fetch failed", url=careers_url, error=str(exc))
        return []

    try:
        data = resp.json()
    except requests.exceptions.JSONDecodeError:
        logger.warning(
            "Lever response is not JSON — careers_url must be the postings API endpoint "
            "(e.g. https://api.lever.co/v0/postings/{slug}), not the human-facing page",
            url=careers_url,
        )
        return []

    jobs = []
    for posting in data:
        jobs.append(
            {
                "title": posting.get("text", ""),
                "url": posting.get("hostedUrl", careers_url),
                "location": posting.get("categories", {}).get("location", ""),
            }
        )
    logger.info("Lever jobs fetched", url=careers_url, count=len(jobs))
    return jobs


def _fetch_default_jobs(company_name: str, careers_url: str) -> list[dict[str, str]]:
    """Fetch job listings from a custom careers page using Playwright and Strands/Bedrock.

    Renders the page with a headless Chromium browser (handles JS-rendered
    content), strips noise with BeautifulSoup, then asks a Bedrock-backed
    agent to extract structured job listings.

    Args:
        company_name: Used in the prompt for context.
        careers_url: URL of the custom careers page to scrape.

    Returns:
        Normalised list of job dicts with title, url, location keys.
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-zygote",  # prevents forking a zygote process (required in Lambda)
                    "--single-process",  # runs renderer in the browser process (required in Lambda)
                ],
            )
            page = browser.new_page()
            page.goto(careers_url, wait_until="networkidle", timeout=30_000)
            html = page.content()
            browser.close()
    except Exception as exc:
        logger.warning("Playwright fetch failed", url=careers_url, error=str(exc))
        return []

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    page_text = soup.get_text(separator="\n", strip=True)[:_PAGE_CHAR_LIMIT]

    model = BedrockModel(
        model_id=os.environ.get("BEDROCK_MODEL", _DEFAULT_MODEL),
        region_name=os.environ.get("BEDROCK_REGION", _DEFAULT_REGION),
    )
    agent = Agent(model=model)

    prompt = (
        "You are a job listing extractor. Extract job listings from the provided page content.\n"
        "Rules:\n"
        "1. Only return URLs that appear verbatim in the page content. Never construct, infer, or modify URLs.\n"
        "2. If you cannot find a complete, valid URL for a job listing, omit that job entirely.\n"
        "3. Return only jobs relevant to platform engineering, SRE, DevOps, cloud, or infrastructure roles.\n"
        "4. Return results as a JSON array with fields: title, url, location.\n"
        "5. If no relevant jobs are found, return an empty array.\n\n"
        f"Page content:\n{page_text}"
    )

    response = agent(prompt)
    raw = str(response)

    try:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            logger.warning("No JSON array found in agent response", company=company_name)
            return []
        jobs: list[Any] = json.loads(match.group())
        result = [j for j in jobs if isinstance(j, dict) and j.get("title") and j.get("url")]
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse agent response as JSON", error=str(exc), company=company_name)
        return []

    # Drop any job whose URL doesn't appear verbatim in the fetched page text.
    # The LLM will hallucinate plausible-looking URLs despite prompt instructions;
    # this is the only reliable guard against that.
    verified = [j for j in result if j["url"] in page_text]
    if len(verified) < len(result):
        logger.warning(
            "Dropped jobs with hallucinated URLs",
            company=company_name,
            dropped=len(result) - len(verified),
            kept=len(verified),
        )

    logger.info("Default (LLM) jobs fetched", company=company_name, url=careers_url, count=len(verified))
    return verified


def _fetch_jobs(company_name: str, careers_url: str, ats: str) -> list[dict[str, str]]:
    """Dispatch to the appropriate ATS handler and return normalised job dicts.

    Args:
        company_name: Used for logging and LLM prompt context.
        careers_url: URL passed to the ATS handler.
        ats: ATS backend identifier ("greenhouse", "lever", or "unknown").

    Returns:
        Normalised list of job dicts with title, url, location keys.
    """
    if ats == "greenhouse":
        return _fetch_greenhouse_jobs(careers_url)
    if ats == "lever":
        return _fetch_lever_jobs(careers_url)
    return _fetch_default_jobs(company_name, careers_url)


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point for the Worker Lambda.

    Processes each SQS record, fetches jobs via the appropriate ATS handler,
    applies the relevance filter, and persists new job postings to DynamoDB.

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
        ats: str = body.get("ats", "unknown")

        logger.info("Processing company", company=company_name, url=careers_url, ats=ats)

        jobs = _filter_relevant_jobs(
            _fetch_jobs(company_name, careers_url, ats),
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
