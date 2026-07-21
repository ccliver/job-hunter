"""Worker Lambda handler.

Triggered by SQS. Each message contains a company name, careers URL, and
optional ATS type. Dispatches to the appropriate ATS handler to fetch and
parse job listings, applies a keyword filter, then writes new postings to
the DynamoDB `jobs` table. Deduplication is achieved by hashing
company+title+url as the DynamoDB partition key (job_id).

ATS backends:
    greenhouse - JSON API; no LLM required
    lever      - JSON API; no LLM required
    workday    - Unofficial JSON API (cxs); no LLM required
    builtin    - Built In (builtin.com) search results page; no LLM required.
                 Aggregates postings across many employers, so each returned
                 job carries its own "company" key instead of relying on the
                 SQS message's company_name. Jobs from companies already
                 tracked directly elsewhere in companies.json are skipped.
    unknown    - Custom careers page; uses Strands/Bedrock (Claude Haiku)

Environment variables expected:
    JOBS_TABLE      - DynamoDB table name for job postings
    COMPANIES_TABLE - DynamoDB table name for tracked companies (used by the
                       builtin ATS backend to skip already-tracked companies)
    BEDROCK_REGION  - AWS region for Bedrock (defaults to us-east-1)
    BEDROCK_MODEL   - Bedrock model ID (defaults to Claude Haiku cross-region inference profile)
    LOCATION          - Location substring to additionally keep for every ATS
                         backend except builtin (defaults to "" — disabled,
                         i.e. remote-only)
    WORK_TYPE         - Work-type keyword to keep for every ATS backend except
                         builtin: "remote", "hybrid", "office", "any", or any
                         other literal substring to match (defaults to "remote")
    BUILTIN_LOCATION  - Same as LOCATION, but for the builtin ATS backend only
                         — independent setting (defaults to "" — disabled)
    BUILTIN_WORK_TYPE - Same as WORK_TYPE, but for the builtin ATS backend only
                         — independent setting (defaults to "remote")
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
from bs4.element import Tag
from playwright.sync_api import sync_playwright
from strands import Agent
from strands.models import BedrockModel

logger = Logger(service="worker")

dynamodb = boto3.resource("dynamodb")

_DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_DEFAULT_REGION = "us-east-1"
_PAGE_CHAR_LIMIT = 15_000

_WORKDAY_URL_RE = re.compile(r"^https://([^./]+)\.(wd\d+)\.myworkdayjobs\.com/([^/?#]+)")
_WORKDAY_PAGE_SIZE = 20
_WORKDAY_MAX_JOBS_PER_KEYWORD = 1000

_BUILTIN_BASE_URL = "https://builtin.com"
_BUILTIN_MAX_PAGES = 15

# Defaults for the LOCATION/WORK_TYPE and BUILTIN_LOCATION/BUILTIN_WORK_TYPE
# env var pairs (see _location_matches / _builtin_location_matches). Kept
# deliberately independent: Built In is a broad discovery search where
# remote-only is a sensible default, while the curated company list includes
# companies chosen for their proximity to a specific future location (e.g.
# NoVA defense contractors), so filtering both off the same setting would
# suppress exactly the hybrid/on-site roles those companies were added for.
# Both default location to blank (disabled) and work type to "remote".
_DEFAULT_LOCATION = ""
_DEFAULT_WORK_TYPE = "remote"
_BUILTIN_DEFAULT_LOCATION = ""
_BUILTIN_DEFAULT_WORK_TYPE = "remote"

_WORK_TYPE_KEYWORDS = {
    "remote": ["remote", "distributed", "anywhere"],
    "hybrid": ["hybrid"],
    "office": ["in-office", "in office", "on-site", "onsite"],
}

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

# Titles matching any of these (case-insensitive) are dropped even if they
# also match _TITLE_KEYWORDS — management/leadership roles, not IC roles.
_EXCLUDE_TITLE_KEYWORDS = [
    "manager",
    "director",
]

# Clearance tiers above Public Trust — the highest tier the user will pursue.
# A "public trust" mention (with none of these) is explicitly allowed.
_HIGH_CLEARANCE_KEYWORDS = [
    "top secret",
    "ts/sci",
    "ts-sci",
    "secret clearance",
    "dod secret",
    "interim secret",
    "polygraph",
    "full scope poly",
    "ci poly",
    "sci clearance",
    "special access program",
    "sap clearance",
    "q clearance",
    "l clearance",
]

# Unspecified/generic clearance mentions with no level given are treated as
# excluded too — unspecified clearance postings at defense contractors
# conventionally mean Secret or above — unless "public trust" is also present.
_GENERIC_CLEARANCE_KEYWORDS = [
    "security clearance",
    "active clearance",
    "clearance required",
    "clearance sponsorship",
    "must possess a clearance",
    "must obtain a clearance",
    "eligible for a clearance",
    "clearable",
]

# Explicit negations checked before _GENERIC_CLEARANCE_KEYWORDS, since e.g.
# "no clearance required" would otherwise substring-match "clearance required".
_NO_CLEARANCE_PHRASES = [
    "no clearance required",
    "no security clearance required",
    "clearance not required",
    "clearance is not required",
    "does not require a clearance",
    "does not require a security clearance",
]

# Standard US employment-law notice boilerplate that would otherwise
# false-positive match a clearance keyword despite having nothing to do with
# government clearance — e.g. the required EPPA notice mentions "polygraph"
# and is present on nearly every US company's careers page.
_CLEARANCE_FALSE_POSITIVE_PHRASES = [
    "employee polygraph protection act",
]


def _requires_excluded_clearance(text: str) -> bool:
    """Check whether text indicates a clearance requirement above Public Trust.

    Public Trust is the one clearance level the user will pursue, so an
    explicit "public trust" mention (with no higher-tier keyword present) is
    allowed, as is an explicit "no clearance required" negation. A
    generic/unspecified clearance mention with no level given is treated as
    excluded by default. Known false-positive boilerplate (e.g. the EPPA
    notice) is stripped before matching.
    """
    text_lower = text.lower()
    for phrase in _CLEARANCE_FALSE_POSITIVE_PHRASES:
        text_lower = text_lower.replace(phrase, "")
    if any(kw in text_lower for kw in _HIGH_CLEARANCE_KEYWORDS):
        return True
    if "public trust" in text_lower:
        return False
    if any(phrase in text_lower for phrase in _NO_CLEARANCE_PHRASES):
        return False
    return any(kw in text_lower for kw in _GENERIC_CLEARANCE_KEYWORDS)


# Countries, business regions, and common offshore/nearshore tech-hub cities
# that indicate a non-US location. Deliberately excludes ambiguous names that
# collide with US places (e.g. "Georgia" the country vs. the US state,
# "Jordan" the country vs. a common name) — those are left included by
# default rather than risk hiding a real US posting. Matched with word
# boundaries (see _NON_US_LOCATION_RE) so short entries like "uk" don't
# false-positive inside words like "Milwaukee".
_NON_US_LOCATION_KEYWORDS = [
    # Business regions
    "emea",
    "apac",
    "latam",
    # Countries
    "india",
    "canada",
    "united kingdom",
    "uk",
    "england",
    "scotland",
    "wales",
    "ireland",
    "germany",
    "france",
    "spain",
    "italy",
    "netherlands",
    "poland",
    "portugal",
    "romania",
    "ukraine",
    "israel",
    "australia",
    "new zealand",
    "singapore",
    "japan",
    "china",
    "hong kong",
    "taiwan",
    "korea",
    "philippines",
    "vietnam",
    "thailand",
    "malaysia",
    "indonesia",
    "pakistan",
    "bangladesh",
    "mexico",
    "brazil",
    "argentina",
    "chile",
    "colombia",
    "peru",
    "costa rica",
    "south africa",
    "nigeria",
    "kenya",
    "egypt",
    "united arab emirates",
    "uae",
    "saudi arabia",
    "turkey",
    "switzerland",
    "austria",
    "belgium",
    "denmark",
    "sweden",
    "norway",
    "finland",
    "czech republic",
    "hungary",
    "greece",
    "russia",
    # Common offshore/nearshore tech-hub cities (no country name attached)
    "bangalore",
    "bengaluru",
    "hyderabad",
    "pune",
    "mumbai",
    "gurgaon",
    "gurugram",
    "noida",
    "toronto",
    "vancouver",
    "montreal",
    "ottawa",
    "london",
    "dublin",
    "manchester",
    "edinburgh",
    "belfast",
    "berlin",
    "munich",
    "frankfurt",
    "hamburg",
    "paris",
    "madrid",
    "barcelona",
    "milan",
    "amsterdam",
    "warsaw",
    "krakow",
    "prague",
    "budapest",
    "bucharest",
    "tel aviv",
    "herzliya",
    "tokyo",
    "seoul",
    "shanghai",
    "beijing",
    "shenzhen",
    "manila",
    "ho chi minh",
    "hanoi",
    "bangkok",
    "jakarta",
    "kuala lumpur",
    "sydney",
    "melbourne",
    "auckland",
    "wellington",
    "sao paulo",
    "são paulo",
    "mexico city",
    "bogota",
    "buenos aires",
    "cape town",
    "johannesburg",
    "lagos",
    "nairobi",
    "cairo",
    "heredia",
]

_NON_US_LOCATION_RE = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in _NON_US_LOCATION_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def _is_non_us_location(location: str) -> bool:
    """Check whether a location string indicates a non-US location.

    Defaults to False (kept) for ambiguous or unhelpful strings like a bare
    "Remote" or "N Locations" — a false negative (a non-US job slipping
    through) is preferable to a false positive (hiding a real US job over an
    incidental keyword match).
    """
    if not location:
        return False
    return bool(_NON_US_LOCATION_RE.search(location))


def _title_looks_relevant(title: str) -> bool:
    """Cheap title-only pre-check mirroring _filter_relevant_jobs's keyword logic.

    Used by fetchers that can fetch a full job description at the cost of an
    extra request per posting (e.g. Workday), to avoid paying that cost for
    postings that would be dropped by _filter_relevant_jobs anyway.
    """
    title_lower = title.lower()
    if not any(kw in title_lower for kw in _TITLE_KEYWORDS):
        return False
    return not any(kw in title_lower for kw in _EXCLUDE_TITLE_KEYWORDS)


def _make_job_id(company: str, title: str, url: str) -> str:
    """Derive a stable deduplication key from company, title, and URL."""
    raw = f"{company}|{title}|{url}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _filter_relevant_jobs(jobs: list[dict[str, str]], company: str) -> list[dict[str, str]]:
    """Drop jobs whose title doesn't match a target-role keyword, or matches an excluded one.

    Performs case-insensitive substring matching against _TITLE_KEYWORDS,
    then drops any of those matches whose title also hits _EXCLUDE_TITLE_KEYWORDS
    (management/leadership roles), indicates a clearance requirement above
    Public Trust, has a location indicating a non-US posting, or (for every
    backend except "builtin") doesn't match the configured LOCATION/WORK_TYPE.
    Built In jobs are exempt from that last check since they're already
    filtered by their own independent BUILTIN_LOCATION/BUILTIN_WORK_TYPE
    config in _fetch_builtin_jobs — detected here via the per-job "company"
    key, which only the builtin backend sets. The clearance check is
    title-only here and applies uniformly across every ATS backend;
    _fetch_greenhouse_jobs, _fetch_workday_jobs, and _fetch_builtin_jobs
    additionally check the full job description. Logs extracted vs. matched
    counts so the keyword lists can be tuned.

    Args:
        jobs: Raw list of job dicts with at least a "title" key.
        company: Company name used for structured log context.

    Returns:
        Subset of jobs whose title matched a target keyword, hit no exclude
        or excluded-clearance keyword, whose location isn't non-US, and
        (unless from the builtin backend) matches the configured work type.
    """
    matched = [j for j in jobs if any(kw in j.get("title", "").lower() for kw in _TITLE_KEYWORDS)]
    filtered = [j for j in matched if not any(kw in j["title"].lower() for kw in _EXCLUDE_TITLE_KEYWORDS)]
    cleared = [j for j in filtered if not _requires_excluded_clearance(j["title"])]
    us_only = [j for j in cleared if not _is_non_us_location(j.get("location", ""))]
    work_type_matched = [j for j in us_only if "company" in j or _location_matches(j.get("location", ""))]
    logger.info(
        "Job filter complete",
        company=company,
        extracted=len(jobs),
        matched=len(matched),
        excluded=len(matched) - len(filtered),
        clearance_excluded=len(filtered) - len(cleared),
        non_us_excluded=len(cleared) - len(us_only),
        work_type_excluded=len(us_only) - len(work_type_matched),
        dropped=len(jobs) - len(work_type_matched),
    )
    return work_type_matched


def _fetch_greenhouse_jobs(careers_url: str) -> list[dict[str, str]]:
    """Fetch job listings from a Greenhouse JSON API endpoint.

    Requests full job descriptions (content=true) at no extra cost — the
    Greenhouse list endpoint includes them in the same response — so postings
    requiring a clearance above Public Trust can be dropped even when the
    title alone doesn't say so.

    Args:
        careers_url: Greenhouse board API URL (already returns JSON).

    Returns:
        Normalised list of job dicts with title, url, location keys.
    """
    try:
        resp = requests.get(careers_url, params={"content": "true"}, timeout=30)
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
    clearance_skipped = 0
    for posting in data.get("jobs", []):
        title = posting.get("title", "")
        if _requires_excluded_clearance(f"{title} {posting.get('content', '')}"):
            clearance_skipped += 1
            continue
        jobs.append(
            {
                "title": title,
                "url": posting.get("absolute_url", careers_url),
                "location": posting.get("location", {}).get("name", ""),
            }
        )
    logger.info("Greenhouse jobs fetched", url=careers_url, count=len(jobs), clearance_skipped=clearance_skipped)
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


def _fetch_workday_job_description(tenant: str, wd: str, site: str, external_path: str) -> str:
    """Fetch a single Workday posting's full description via its detail endpoint.

    Returns "" on any failure — callers fall back to title-only clearance
    checking in that case, rather than dropping the job outright over a
    transient error.
    """
    detail_url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{external_path}"
    try:
        resp = requests.get(detail_url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, requests.exceptions.JSONDecodeError) as exc:
        logger.warning("Workday job detail fetch failed", url=detail_url, error=str(exc))
        return ""
    return data.get("jobPostingInfo", {}).get("jobDescription", "")


def _fetch_workday_jobs(careers_url: str) -> list[dict[str, str]]:
    """Fetch job listings from a Workday-hosted careers site via its unofficial JSON API.

    Parses the tenant/site from a myworkdayjobs.com careers URL, then issues
    one paginated search per _TITLE_KEYWORDS entry (via the `searchText`
    param) instead of paginating the company's entire board unfiltered.
    Company board sizes vary enormously — a few hundred postings for a
    startup vs. 17,000+ for a national retail chain with a posting per store
    — but Workday's search narrows results server-side, so the keyword-
    scoped subset stays a manageable size regardless of company size (e.g.
    empirically, CVS's ~17,700 total postings narrow to under 300 for any
    single one of these keywords). Workday's search is a fuzzy full-text
    match, not an exact substring one (e.g. searching "platform" surfaces
    unrelated titles too), so every result is still re-checked with the
    exact _title_looks_relevant filter before being kept — this only saves
    us from scanning thousands of irrelevant postings to find the relevant
    ones. The same posting can surface under multiple keywords, so seen_paths
    dedupes across searches to avoid double-processing (and double-fetching
    descriptions for) the same posting. For postings whose title already
    looks relevant, a follow-up request fetches the full description to
    catch clearance requirements that aren't mentioned in the title.

    Args:
        careers_url: Careers URL of the form
            https://{tenant}.wd{N}.myworkdayjobs.com/{site}.

    Returns:
        Normalised list of job dicts with title, url, location keys.
    """
    match = _WORKDAY_URL_RE.match(careers_url)
    if not match:
        logger.warning("Not a parseable myworkdayjobs.com URL", url=careers_url)
        return []
    tenant, wd, site = match.groups()
    base_url = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
    api_url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"

    jobs = []
    clearance_skipped = 0
    seen_paths: set[str] = set()

    for keyword in _TITLE_KEYWORDS:
        offset = 0
        while offset < _WORKDAY_MAX_JOBS_PER_KEYWORD:
            try:
                resp = requests.post(
                    api_url,
                    json={"limit": _WORKDAY_PAGE_SIZE, "offset": offset, "searchText": keyword},
                    headers={"Content-Type": "application/json"},
                    timeout=30,
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                logger.warning("Workday fetch failed", url=api_url, keyword=keyword, error=str(exc))
                break

            try:
                data = resp.json()
            except requests.exceptions.JSONDecodeError:
                logger.warning("Workday response is not JSON", url=api_url, keyword=keyword)
                break

            postings = data.get("jobPostings", [])
            if not postings:
                break

            for posting in postings:
                external_path = posting.get("externalPath", "")
                if external_path in seen_paths:
                    continue
                title = posting.get("title", "")
                if not _title_looks_relevant(title):
                    continue
                seen_paths.add(external_path)
                description = _fetch_workday_job_description(tenant, wd, site, external_path)
                if _requires_excluded_clearance(f"{title} {description}"):
                    clearance_skipped += 1
                    continue
                jobs.append(
                    {
                        "title": title,
                        "url": base_url + external_path,
                        "location": posting.get("locationsText", ""),
                    }
                )

            offset += _WORKDAY_PAGE_SIZE
            if offset >= data.get("total", 0):
                break

    logger.info("Workday jobs fetched", url=careers_url, count=len(jobs), clearance_skipped=clearance_skipped)
    return jobs


def _get_known_company_names() -> set[str]:
    """Return the lowercased names of companies already tracked in COMPANIES_TABLE."""
    table = dynamodb.Table(os.environ["COMPANIES_TABLE"])
    items = table.scan(ProjectionExpression="company_name").get("Items", [])
    return {item["company_name"].lower() for item in items}


def _is_known_company(company: str, known_companies: set[str]) -> bool:
    """Check whether a Built In company name matches an already-tracked company.

    Uses substring containment (not just exact match) since Built In's display
    name for a company often differs slightly from companies.json (e.g. "CACI"
    vs "CACI International", "Coinbase Global, Inc." vs "Coinbase").
    """
    company_lower = company.lower()
    return any(known in company_lower or company_lower in known for known in known_companies)


def _work_type_matches(
    location: str, location_env_var: str, work_type_env_var: str, default_location: str, default_work_type: str
) -> bool:
    """Shared implementation behind _location_matches and _builtin_location_matches.

    A job is kept if its location contains the configured target location
    substring, or its location indicates the configured work type (or the
    work type env var is "any", or its value isn't a recognised keyword, in
    which case it's matched literally as a substring too). If the work type
    is "any" and no target location is configured, the whole check is
    disabled and every job passes, blank location included. Otherwise an
    empty location fails the match — this filter narrows down to a specific
    set, unlike the fail-open non-US location filter.
    """
    target_location = os.environ.get(location_env_var, default_location)
    work_type = os.environ.get(work_type_env_var, default_work_type).lower()

    if not target_location and work_type == "any":
        return True

    if not location:
        return False
    location_lower = location.lower()

    if target_location and target_location.lower() in location_lower:
        return True
    if work_type == "any":
        return True
    keywords = _WORK_TYPE_KEYWORDS.get(work_type, [work_type])
    return any(kw in location_lower for kw in keywords)


def _location_matches(location: str) -> bool:
    """Check a job's location against the configured LOCATION / WORK_TYPE env vars.

    Applies to every ATS backend except "builtin", which has its own
    independent BUILTIN_LOCATION / BUILTIN_WORK_TYPE config (see
    _builtin_location_matches) — kept separate because the curated company
    list includes companies chosen for proximity to a specific future
    location, so a hybrid/on-site preference there shouldn't be governed by
    the same "remote only" default that makes sense for Built In's broad
    discovery search. Defaults to remote-only. See _work_type_matches for
    the shared matching rules.
    """
    return _work_type_matches(location, "LOCATION", "WORK_TYPE", _DEFAULT_LOCATION, _DEFAULT_WORK_TYPE)


def _builtin_location_matches(location: str) -> bool:
    """Check a Built In job's location against the configured target location or work type.

    Controlled by the BUILTIN_LOCATION (default "" — disabled) and
    BUILTIN_WORK_TYPE (default "remote") env vars, independent of the
    LOCATION / WORK_TYPE env vars used by every other backend (see
    _location_matches). Defaults to pure remote-only filtering, matching the
    user's own manual search practice on builtin.com (leave location blank,
    filter to Remote). See _work_type_matches for the shared matching rules.
    """
    return _work_type_matches(
        location, "BUILTIN_LOCATION", "BUILTIN_WORK_TYPE", _BUILTIN_DEFAULT_LOCATION, _BUILTIN_DEFAULT_WORK_TYPE
    )


def _builtin_card_text_by_icon(card: Tag, icon_class: str) -> str:
    """Extract the text sibling next to a Font Awesome icon within a Built In job card."""
    icon = card.select_one(f".{icon_class}")
    if not icon:
        return ""
    parent = icon.find_parent("div")
    sibling = parent.find_next_sibling() if parent else None
    return sibling.get_text(strip=True) if sibling else ""


def _fetch_builtin_job_description(url: str) -> str:
    """Fetch a single Built In job's detail page and return its cleaned text.

    The full description is present in the server-rendered page — no special
    container selector needed. Returns "" on any failure — callers fall back
    to title-only clearance checking in that case, rather than dropping the
    job outright over a transient error.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Built In job detail fetch failed", url=url, error=str(exc))
        return ""
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def _fetch_builtin_jobs(careers_url: str) -> list[dict[str, str]]:
    """Fetch job listings from a Built In (builtin.com) search results page.

    The search page is server-rendered — no Playwright/JS execution needed.
    Paginates via the `page` query param until a page returns no job cards.
    Built In aggregates postings across many employers, so each job dict
    carries its own "company" key; jobs from companies already tracked
    directly elsewhere in companies.json are skipped (they're covered, often
    more completely, by their own direct fetch). The search results don't
    include job descriptions, so for postings whose title already looks
    relevant (_title_looks_relevant), a follow-up request to the job's own
    detail page fetches the full description to catch clearance requirements
    that aren't mentioned in the title — same pattern as _fetch_workday_jobs,
    and for the same reason (avoid an extra request per irrelevant posting).
    Postings are also dropped by _builtin_location_matches (BUILTIN_LOCATION /
    BUILTIN_WORK_TYPE env vars) before the description fetch, for the same
    cost-avoidance reason.

    Args:
        careers_url: A Built In search URL, e.g.
            https://builtin.com/jobs?search=AWS&daysSinceUpdated=3

    Returns:
        Normalised list of job dicts with title, url, location, and company keys.
    """
    known_companies = _get_known_company_names()

    jobs = []
    location_skipped = 0
    clearance_skipped = 0
    for page in range(1, _BUILTIN_MAX_PAGES + 1):
        try:
            resp = requests.get(
                careers_url,
                params={"page": page},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Built In fetch failed", url=careers_url, page=page, error=str(exc))
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select('[data-id="job-card"]')
        if not cards:
            break

        for card in cards:
            title_el = card.select_one('[data-id="job-card-title"]')
            company_el = card.select_one('[data-id="company-title"]')
            if not title_el or not company_el:
                continue
            company = company_el.get_text(strip=True)
            if _is_known_company(company, known_companies):
                continue
            title = title_el.get_text(strip=True)
            if not _title_looks_relevant(title):
                continue
            location = _builtin_card_text_by_icon(card, "fa-location-dot")
            if not _builtin_location_matches(location):
                location_skipped += 1
                continue
            href = title_el.get("href", "")
            job_url = _BUILTIN_BASE_URL + (href if isinstance(href, str) else "")
            description = _fetch_builtin_job_description(job_url)
            if _requires_excluded_clearance(f"{title} {description}"):
                clearance_skipped += 1
                continue
            jobs.append(
                {
                    "title": title,
                    "url": job_url,
                    "location": location,
                    "company": company,
                }
            )

    logger.info(
        "Built In jobs fetched",
        url=careers_url,
        count=len(jobs),
        location_skipped=location_skipped,
        clearance_skipped=clearance_skipped,
    )
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
        "4. Omit any job that requires a security clearance above Public Trust (e.g. Secret, Top Secret, "
        "TS/SCI, polygraph). Jobs requiring only a Public Trust clearance, or no clearance at all, are fine "
        "to include.\n"
        "5. Return results as a JSON array with fields: title, url, location.\n"
        "6. If no relevant jobs are found, return an empty array.\n\n"
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
        ats: ATS backend identifier ("greenhouse", "lever", "workday", "builtin", or "unknown").

    Returns:
        Normalised list of job dicts with title, url, location keys (plus a
        "company" key for the "builtin" backend, which aggregates postings
        across many employers).
    """
    if ats == "greenhouse":
        return _fetch_greenhouse_jobs(careers_url)
    if ats == "lever":
        return _fetch_lever_jobs(careers_url)
    if ats == "workday":
        return _fetch_workday_jobs(careers_url)
    if ats == "builtin":
        return _fetch_builtin_jobs(careers_url)
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
            # "builtin" jobs carry their own company (Built In aggregates across
            # employers); every other backend's jobs belong to company_name.
            job_company = job.get("company") or company_name
            job_id = _make_job_id(job_company, job["title"], job["url"])
            item = {
                "job_id": job_id,
                "company": job_company,
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
                logger.info("Wrote new job", title=job["title"], company=job_company)
            except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
                logger.debug("Duplicate skipped", job_id=job_id)

        records_processed += 1

    logger.info("Worker done", records_processed=records_processed, jobs_written=jobs_written)
    return {"records_processed": records_processed, "jobs_written": jobs_written}
