"""Tests for the Worker Lambda handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
import requests
from moto import mock_aws

from worker.handler import (
    _builtin_location_matches,
    _fetch_builtin_jobs,
    _fetch_greenhouse_jobs,
    _fetch_jobs,
    _fetch_workday_jobs,
    _filter_relevant_jobs,
    _is_non_us_location,
    _location_matches,
    _make_job_id,
    _requires_excluded_clearance,
    handler,
)

REGION = "us-east-1"


def test_make_job_id_is_deterministic() -> None:
    """Same inputs should always produce the same job_id."""
    id1 = _make_job_id("Acme", "Engineer", "https://acme.com/jobs/1")
    id2 = _make_job_id("Acme", "Engineer", "https://acme.com/jobs/1")
    assert id1 == id2


def test_make_job_id_differs_for_different_inputs() -> None:
    """Different inputs should produce different job_ids."""
    id1 = _make_job_id("Acme", "Engineer", "https://acme.com/jobs/1")
    id2 = _make_job_id("Acme", "Engineer", "https://acme.com/jobs/2")
    assert id1 != id2


@pytest.fixture()
def aws_resources(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.create_table(
            TableName="test-jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        companies_table = dynamodb.create_table(
            TableName="test-companies",
            KeySchema=[{"AttributeName": "company_name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "company_name", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        monkeypatch.setenv("JOBS_TABLE", "test-jobs")
        monkeypatch.setenv("COMPANIES_TABLE", "test-companies")
        monkeypatch.setenv("BEDROCK_REGION", REGION)
        monkeypatch.setenv("BEDROCK_MODEL", "anthropic.claude-haiku-4-5-20251001-v1:0")

        yield {"table": table, "companies_table": companies_table}


def _sqs_event(company_name: str, careers_url: str, ats: str = "unknown") -> dict:
    return {"Records": [{"body": json.dumps({"company_name": company_name, "careers_url": careers_url, "ats": ats})}]}


# --- handler integration tests (ATS dispatch mocked at _fetch_jobs) ---


@patch("worker.handler._fetch_jobs", return_value=[])
def test_handler_no_jobs_found(mock_fetch, aws_resources: dict, lambda_context) -> None:
    """handler() should return 0 jobs_written when the fetcher finds nothing."""
    result = handler(_sqs_event("Acme Corp", "https://acme.com/jobs"), lambda_context)

    assert result["records_processed"] == 1
    assert result["jobs_written"] == 0
    assert aws_resources["table"].scan()["Count"] == 0


@patch("worker.handler._fetch_jobs")
def test_handler_writes_new_jobs(mock_fetch, aws_resources: dict, lambda_context) -> None:
    """handler() should write each fetched job that passes the title filter."""
    mock_fetch.return_value = [
        {"title": "Platform Engineer", "url": "https://acme.com/jobs/1", "location": "Remote"},
    ]

    result = handler(_sqs_event("Acme Corp", "https://acme.com/jobs"), lambda_context)

    assert result["jobs_written"] == 1
    items = aws_resources["table"].scan()["Items"]
    assert len(items) == 1
    assert items[0]["title"] == "Platform Engineer"
    assert items[0]["company"] == "Acme Corp"
    assert items[0]["location"] == "Remote"
    assert "discovered_at" in items[0]


@patch("worker.handler._fetch_jobs")
def test_handler_deduplicates_jobs(mock_fetch, aws_resources: dict, lambda_context) -> None:
    """Calling handler twice with the same job should only write it once."""
    mock_fetch.return_value = [
        {"title": "Platform Engineer", "url": "https://acme.com/jobs/1", "location": "Remote"},
    ]
    event = _sqs_event("Acme Corp", "https://acme.com/jobs")

    first = handler(event, lambda_context)
    second = handler(event, lambda_context)

    assert first["jobs_written"] == 1
    assert second["jobs_written"] == 0
    assert aws_resources["table"].scan()["Count"] == 1


@patch("worker.handler._fetch_jobs")
def test_handler_drops_irrelevant_jobs(mock_fetch, aws_resources: dict, lambda_context) -> None:
    """handler() should not write jobs whose title doesn't match target keywords."""
    mock_fetch.return_value = [
        {"title": "Software Engineer", "url": "https://acme.com/jobs/1", "location": "Remote"},
        {"title": "Product Manager", "url": "https://acme.com/jobs/2", "location": "Remote"},
    ]

    result = handler(_sqs_event("Acme Corp", "https://acme.com/jobs"), lambda_context)

    assert result["jobs_written"] == 0
    assert aws_resources["table"].scan()["Count"] == 0


@patch("worker.handler._fetch_jobs")
def test_handler_passes_ats_to_fetch(mock_fetch, aws_resources: dict, lambda_context) -> None:
    """handler() should forward the ats field from the SQS message to _fetch_jobs."""
    mock_fetch.return_value = []

    handler(_sqs_event("Datadog", "https://boards.greenhouse.io/datadog", ats="greenhouse"), lambda_context)

    mock_fetch.assert_called_once_with("Datadog", "https://boards.greenhouse.io/datadog", "greenhouse")


@patch("worker.handler.requests.get")
@patch("worker.handler.requests.post")
def test_handler_writes_workday_jobs_across_pages(mock_post, mock_get, aws_resources: dict, lambda_context) -> None:
    """handler() should paginate the Workday API and persist all matching jobs to DynamoDB."""
    page1_postings = [_workday_posting("Store Associate", f"R{i}") for i in range(19)]
    page1_postings.append(_workday_posting("Platform Engineer", "R001"))
    page1 = _workday_page(page1_postings, total=21)
    page2 = _workday_page([_workday_posting("Store Associate", "R999")], total=21)
    responses = [page1, page2]

    def fake_post(*args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.json.side_effect = lambda: responses.pop(0)
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    mock_post.side_effect = fake_post
    mock_get.return_value.json.return_value = _workday_job_detail("No clearance required.")
    mock_get.return_value.raise_for_status.return_value = None

    result = handler(
        _sqs_event("Acme", "https://acme.wd1.myworkdayjobs.com/acme-careers", ats="workday"), lambda_context
    )

    assert mock_post.call_count == 2
    # Only the one relevant-titled posting ("Platform Engineer") triggers a description fetch;
    # the 20 "Store Associate" postings are dropped by the title pre-filter first.
    assert mock_get.call_count == 1
    assert result["jobs_written"] == 1
    items = aws_resources["table"].scan()["Items"]
    assert len(items) == 1
    assert items[0]["title"] == "Platform Engineer"
    assert items[0]["url"] == "https://acme.wd1.myworkdayjobs.com/acme-careers/job/Remote/Platform-Engineer_R001"


@patch("worker.handler._fetch_jobs")
def test_handler_uses_per_job_company_when_present(mock_fetch, aws_resources: dict, lambda_context) -> None:
    """handler() should prefer a job's own "company" key (e.g. from the builtin fetcher) over company_name."""
    mock_fetch.return_value = [
        {"title": "Platform Engineer", "url": "https://builtin.com/job/1", "location": "Remote", "company": "ZS"},
    ]

    result = handler(
        _sqs_event("Built In - AWS Search", "https://builtin.com/jobs?search=AWS", ats="builtin"), lambda_context
    )

    assert result["jobs_written"] == 1
    items = aws_resources["table"].scan()["Items"]
    assert len(items) == 1
    assert items[0]["company"] == "ZS"


@patch("worker.handler._fetch_jobs")
def test_handler_defaults_ats_to_unknown(mock_fetch, aws_resources: dict, lambda_context) -> None:
    """handler() should default ats to 'unknown' when not present in the SQS message."""
    mock_fetch.return_value = []
    event = {"Records": [{"body": json.dumps({"company_name": "Acme", "careers_url": "https://acme.com/jobs"})}]}

    handler(event, lambda_context)

    mock_fetch.assert_called_once_with("Acme", "https://acme.com/jobs", "unknown")


# --- _fetch_jobs dispatch unit tests ---


@patch("worker.handler._fetch_greenhouse_jobs")
def test_fetch_jobs_dispatches_greenhouse(mock_gh) -> None:
    """_fetch_jobs should call _fetch_greenhouse_jobs for ats='greenhouse'."""
    mock_gh.return_value = []
    _fetch_jobs("Acme", "https://boards.greenhouse.io/acme", "greenhouse")
    mock_gh.assert_called_once_with("https://boards.greenhouse.io/acme")


@patch("worker.handler._fetch_lever_jobs")
def test_fetch_jobs_dispatches_lever(mock_lv) -> None:
    """_fetch_jobs should call _fetch_lever_jobs for ats='lever'."""
    mock_lv.return_value = []
    _fetch_jobs("Acme", "https://jobs.lever.co/acme", "lever")
    mock_lv.assert_called_once_with("https://jobs.lever.co/acme")


@patch("worker.handler._fetch_default_jobs")
def test_fetch_jobs_dispatches_unknown(mock_def) -> None:
    """_fetch_jobs should call _fetch_default_jobs for ats='unknown'."""
    mock_def.return_value = []
    _fetch_jobs("Acme", "https://acme.com/jobs", "unknown")
    mock_def.assert_called_once_with("Acme", "https://acme.com/jobs")


@patch("worker.handler._fetch_workday_jobs")
def test_fetch_jobs_dispatches_workday(mock_wd) -> None:
    """_fetch_jobs should call _fetch_workday_jobs for ats='workday'."""
    mock_wd.return_value = []
    _fetch_jobs("Acme", "https://acme.wd1.myworkdayjobs.com/acme", "workday")
    mock_wd.assert_called_once_with("https://acme.wd1.myworkdayjobs.com/acme")


@patch("worker.handler._fetch_builtin_jobs")
def test_fetch_jobs_dispatches_builtin(mock_bi) -> None:
    """_fetch_jobs should call _fetch_builtin_jobs for ats='builtin'."""
    mock_bi.return_value = []
    _fetch_jobs("Built In - AWS Search", "https://builtin.com/jobs?search=AWS", "builtin")
    mock_bi.assert_called_once_with("https://builtin.com/jobs?search=AWS")


@patch("worker.handler._fetch_default_jobs")
def test_fetch_jobs_dispatches_unrecognised_ats(mock_def) -> None:
    """_fetch_jobs should fall back to the default handler for unknown ATS values."""
    mock_def.return_value = []
    _fetch_jobs("Acme", "https://acme.com/jobs", "some-other-ats")
    mock_def.assert_called_once_with("Acme", "https://acme.com/jobs")


# --- _fetch_greenhouse_jobs unit tests ---


def _greenhouse_posting(title: str, content: str = "") -> dict:
    return {
        "title": title,
        "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{title}",
        "location": {"name": "Remote"},
        "content": content,
    }


@patch("worker.handler.requests.get")
def test_fetch_greenhouse_jobs_requests_full_content(mock_get) -> None:
    """_fetch_greenhouse_jobs should request content=true to get full descriptions for free."""
    mock_get.return_value.json.return_value = {"jobs": [_greenhouse_posting("Platform Engineer")]}
    mock_get.return_value.raise_for_status.return_value = None

    _fetch_greenhouse_jobs("https://boards-api.greenhouse.io/v1/boards/acme/jobs")

    assert mock_get.call_args.kwargs["params"] == {"content": "true"}


@patch("worker.handler.requests.get")
def test_fetch_greenhouse_jobs_excludes_high_clearance_description(mock_get) -> None:
    """_fetch_greenhouse_jobs should drop postings whose description requires a high clearance."""
    mock_get.return_value.json.return_value = {
        "jobs": [
            _greenhouse_posting("Cloud Engineer", content="Must hold an active Top Secret clearance."),
            _greenhouse_posting("Platform Engineer", content="No clearance required."),
        ]
    }
    mock_get.return_value.raise_for_status.return_value = None

    jobs = _fetch_greenhouse_jobs("https://boards-api.greenhouse.io/v1/boards/acme/jobs")

    assert [j["title"] for j in jobs] == ["Platform Engineer"]


@patch("worker.handler.requests.get")
def test_fetch_greenhouse_jobs_allows_public_trust_description(mock_get) -> None:
    """_fetch_greenhouse_jobs should keep postings whose description only requires Public Trust."""
    mock_get.return_value.json.return_value = {
        "jobs": [_greenhouse_posting("Cloud Engineer", content="Requires a Public Trust clearance.")]
    }
    mock_get.return_value.raise_for_status.return_value = None

    jobs = _fetch_greenhouse_jobs("https://boards-api.greenhouse.io/v1/boards/acme/jobs")

    assert [j["title"] for j in jobs] == ["Cloud Engineer"]


# --- _fetch_workday_jobs unit tests ---


def _workday_page(postings: list[dict], total: int) -> dict:
    return {"total": total, "jobPostings": postings}


def _workday_posting(title: str, req: str, location: str = "Remote") -> dict:
    return {
        "title": title,
        "externalPath": f"/job/{location}/{title.replace(' ', '-')}_{req}",
        "locationsText": location,
        "postedOn": "Posted Today",
    }


def _workday_job_detail(description: str) -> dict:
    return {"jobPostingInfo": {"jobDescription": description}}


@patch("worker.handler.requests.get")
@patch("worker.handler.requests.post")
def test_fetch_workday_jobs_single_page(mock_post, mock_get) -> None:
    """_fetch_workday_jobs should normalise postings from a single page of results."""
    mock_post.return_value.json.return_value = _workday_page([_workday_posting("Platform Engineer", "R001")], total=1)
    mock_post.return_value.raise_for_status = lambda: None
    mock_get.return_value.json.return_value = _workday_job_detail("No clearance required.")
    mock_get.return_value.raise_for_status.return_value = None

    jobs = _fetch_workday_jobs("https://acme.wd1.myworkdayjobs.com/acme-careers")

    assert jobs == [
        {
            "title": "Platform Engineer",
            "url": "https://acme.wd1.myworkdayjobs.com/acme-careers/job/Remote/Platform-Engineer_R001",
            "location": "Remote",
        }
    ]
    called_url = mock_post.call_args.args[0]
    assert called_url == "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/acme-careers/jobs"
    assert mock_post.call_args.kwargs["json"] == {"limit": 20, "offset": 0, "searchText": ""}
    assert mock_post.call_args.kwargs["headers"] == {"Content-Type": "application/json"}
    assert mock_get.call_args.args[0] == (
        "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/acme-careers/job/Remote/Platform-Engineer_R001"
    )


@patch("worker.handler.requests.get")
@patch("worker.handler.requests.post")
def test_fetch_workday_jobs_paginates_across_pages(mock_post, mock_get) -> None:
    """_fetch_workday_jobs should keep requesting pages until all postings are collected."""
    page1 = _workday_page([_workday_posting(f"Platform Engineer {i}", f"R00{i}") for i in range(20)], total=25)
    page2 = _workday_page([_workday_posting(f"Platform Engineer {i}", f"R00{i}") for i in range(20, 25)], total=25)

    responses = [page1, page2]

    def fake_post(*args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.json.side_effect = lambda: responses.pop(0)
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    mock_post.side_effect = fake_post
    mock_get.return_value.json.return_value = _workday_job_detail("No clearance required.")
    mock_get.return_value.raise_for_status.return_value = None

    jobs = _fetch_workday_jobs("https://acme.wd1.myworkdayjobs.com/acme-careers")

    assert len(jobs) == 25
    assert mock_post.call_count == 2
    offsets = [call.kwargs["json"]["offset"] for call in mock_post.call_args_list]
    assert offsets == [0, 20]
    assert mock_get.call_count == 25


def test_fetch_workday_jobs_non_workday_url_returns_empty() -> None:
    """_fetch_workday_jobs should return [] and not attempt a request for a non-myworkdayjobs.com URL."""
    assert _fetch_workday_jobs("https://acme.com/careers") == []


@patch("worker.handler.requests.post")
def test_fetch_workday_jobs_request_failure_returns_empty(mock_post) -> None:
    """_fetch_workday_jobs should return [] when the HTTP request raises."""
    mock_post.side_effect = requests.RequestException("boom")

    jobs = _fetch_workday_jobs("https://acme.wd1.myworkdayjobs.com/acme-careers")

    assert jobs == []


@patch("worker.handler.requests.get")
@patch("worker.handler.requests.post")
def test_fetch_workday_jobs_skips_irrelevant_titles_without_description_fetch(mock_post, mock_get) -> None:
    """_fetch_workday_jobs should never fetch a description for a title that isn't relevant."""
    mock_post.return_value.json.return_value = _workday_page([_workday_posting("Store Associate", "R001")], total=1)
    mock_post.return_value.raise_for_status.return_value = None

    jobs = _fetch_workday_jobs("https://acme.wd1.myworkdayjobs.com/acme-careers")

    assert jobs == []
    mock_get.assert_not_called()


@patch("worker.handler.requests.get")
@patch("worker.handler.requests.post")
def test_fetch_workday_jobs_excludes_high_clearance_description(mock_post, mock_get) -> None:
    """_fetch_workday_jobs should drop a posting whose description requires a high clearance,
    even when the title itself gives no indication (the real CACI bug this guards against)."""
    mock_post.return_value.json.return_value = _workday_page(
        [_workday_posting("Infrastructure Observability and Monitoring Specialist", "R001")], total=1
    )
    mock_post.return_value.raise_for_status.return_value = None
    mock_get.return_value.json.return_value = _workday_job_detail("Minimum Clearance Required to Start: TS/SCI")
    mock_get.return_value.raise_for_status.return_value = None

    jobs = _fetch_workday_jobs("https://acme.wd1.myworkdayjobs.com/acme-careers")

    assert jobs == []


@patch("worker.handler.requests.get")
@patch("worker.handler.requests.post")
def test_fetch_workday_jobs_allows_public_trust_description(mock_post, mock_get) -> None:
    """_fetch_workday_jobs should keep a posting whose description only requires Public Trust."""
    mock_post.return_value.json.return_value = _workday_page([_workday_posting("Cloud Engineer", "R001")], total=1)
    mock_post.return_value.raise_for_status.return_value = None
    mock_get.return_value.json.return_value = _workday_job_detail("Requires a Public Trust clearance.")
    mock_get.return_value.raise_for_status.return_value = None

    jobs = _fetch_workday_jobs("https://acme.wd1.myworkdayjobs.com/acme-careers")

    assert len(jobs) == 1


@patch("worker.handler.requests.get")
@patch("worker.handler.requests.post")
def test_fetch_workday_jobs_description_fetch_failure_falls_back_to_title(mock_post, mock_get) -> None:
    """_fetch_workday_jobs should keep a relevant, clean-titled posting even if the detail fetch fails."""
    mock_post.return_value.json.return_value = _workday_page([_workday_posting("Platform Engineer", "R001")], total=1)
    mock_post.return_value.raise_for_status.return_value = None
    mock_get.side_effect = requests.RequestException("boom")

    jobs = _fetch_workday_jobs("https://acme.wd1.myworkdayjobs.com/acme-careers")

    assert len(jobs) == 1


# --- _fetch_builtin_jobs unit tests ---


def _builtin_card_html(title: str, href: str, company: str, location: str) -> str:
    return f"""
    <div data-id="job-card">
        <a data-id="company-title"><span>{company}</span></a>
        <a href="{href}" data-id="job-card-title">{title}</a>
        <div class="d-flex align-items-start gap-sm">
            <div class="d-flex justify-content-center align-items-center h-lg min-w-md">
                <i class="fa-regular fa-location-dot fs-xs text-pretty-blue"></i>
            </div>
            <div><span class="font-barlow text-gray-04">{location}</span></div>
        </div>
    </div>
    """


def _builtin_page_html(cards: list[str]) -> str:
    return f"<html><body><div class='row'>{''.join(cards)}</div></body></html>"


def _seed_companies(companies_table, *names: str) -> None:
    for name in names:
        companies_table.put_item(Item={"company_name": name})


def _mock_builtin_gets(mock_get, pages: list[str], description: str = "No clearance required.") -> None:
    """Wire mock_get.side_effect for both search-page and job-detail-page calls.

    Search-page requests carry a "page" key in their params kwarg; job-detail
    requests (_fetch_builtin_job_description) don't pass params at all, so
    responses are dispatched based on that.
    """
    responses = list(pages) + [_builtin_page_html([])]

    def fake_get(*args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        if kwargs.get("params", {}).get("page"):
            mock_resp.text = responses.pop(0) if len(responses) > 1 else responses[0]
        else:
            mock_resp.text = f"<html><body>{description}</body></html>"
        return mock_resp

    mock_get.side_effect = fake_get


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_single_page(mock_get, aws_resources: dict) -> None:
    """_fetch_builtin_jobs should normalise job cards, including a per-job company key."""
    _mock_builtin_gets(
        mock_get,
        [
            _builtin_page_html(
                [_builtin_card_html("Senior Platform Engineer", "/job/senior-platform-engineer/123", "ZS", "Remote")]
            )
        ],
    )

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert jobs == [
        {
            "title": "Senior Platform Engineer",
            "url": "https://builtin.com/job/senior-platform-engineer/123",
            "location": "Remote",
            "company": "ZS",
        }
    ]
    assert mock_get.call_args_list[0].kwargs["params"] == {"page": 1}
    # Second call is the description fetch for the one relevant-titled posting.
    assert mock_get.call_args_list[1].args[0] == "https://builtin.com/job/senior-platform-engineer/123"


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_paginates_until_empty_page(mock_get, aws_resources: dict) -> None:
    """_fetch_builtin_jobs should keep requesting pages until one comes back with no job cards."""
    _mock_builtin_gets(
        mock_get,
        [
            _builtin_page_html([_builtin_card_html("Platform Engineer", "/job/platform-engineer/1", "Acme", "Remote")]),
            _builtin_page_html([_builtin_card_html("SRE", "/job/sre/2", "Beta Corp", "Remote")]),
        ],
    )

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert len(jobs) == 2
    search_calls = [c for c in mock_get.call_args_list if c.kwargs.get("params", {}).get("page")]
    pages = [c.kwargs["params"]["page"] for c in search_calls]
    assert pages == [1, 2, 3]


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_skips_known_companies(mock_get, aws_resources: dict) -> None:
    """_fetch_builtin_jobs should drop jobs whose company is already tracked in companies.json."""
    _seed_companies(aws_resources["companies_table"], "Datadog")
    _mock_builtin_gets(
        mock_get,
        [
            _builtin_page_html(
                [
                    _builtin_card_html("Platform Engineer", "/job/platform-engineer/1", "Datadog", "Remote"),
                    _builtin_card_html("SRE", "/job/sre/2", "Some New Startup", "Remote"),
                ]
            )
        ],
    )

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert [j["company"] for j in jobs] == ["Some New Startup"]


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_skips_known_companies_by_substring(mock_get, aws_resources: dict) -> None:
    """_fetch_builtin_jobs should match tracked companies even with a differing display name."""
    _seed_companies(aws_resources["companies_table"], "CACI International")
    _mock_builtin_gets(
        mock_get,
        [_builtin_page_html([_builtin_card_html("Cloud Engineer", "/job/cloud-engineer/1", "CACI", "Remote")])],
    )

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert jobs == []


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_request_failure_returns_empty(mock_get, aws_resources: dict) -> None:
    """_fetch_builtin_jobs should return [] when the HTTP request raises."""
    mock_get.side_effect = requests.RequestException("boom")

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert jobs == []


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_skips_irrelevant_titles_without_description_fetch(mock_get, aws_resources: dict) -> None:
    """_fetch_builtin_jobs should never fetch a description for a title that isn't relevant."""
    _mock_builtin_gets(
        mock_get,
        [_builtin_page_html([_builtin_card_html("Store Associate", "/job/store-associate/1", "Acme", "Remote")])],
    )

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert jobs == []
    # Every call made should be a paginated search-page call; none should be
    # a description fetch (those never pass a "page" param).
    assert all(c.kwargs.get("params", {}).get("page") for c in mock_get.call_args_list)


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_skips_non_matching_location_without_description_fetch(
    mock_get, aws_resources: dict
) -> None:
    """_fetch_builtin_jobs should drop a relevant job whose location doesn't match, without a description fetch."""
    _mock_builtin_gets(
        mock_get,
        [_builtin_page_html([_builtin_card_html("Platform Engineer", "/job/platform-engineer/1", "Acme", "Hybrid")])],
    )

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert jobs == []
    assert all(c.kwargs.get("params", {}).get("page") for c in mock_get.call_args_list)


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_keeps_remote_by_default(mock_get, aws_resources: dict) -> None:
    """_fetch_builtin_jobs should keep a Remote job under the default (location-blank) config."""
    _mock_builtin_gets(
        mock_get,
        [_builtin_page_html([_builtin_card_html("Platform Engineer", "/job/platform-engineer/1", "Acme", "Remote")])],
    )

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert len(jobs) == 1


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_drops_non_remote_by_default(mock_get, aws_resources: dict) -> None:
    """_fetch_builtin_jobs should drop a specific-city job under the default (location-blank) config."""
    _mock_builtin_gets(
        mock_get,
        [
            _builtin_page_html(
                [_builtin_card_html("Platform Engineer", "/job/platform-engineer/1", "Acme", "Reston, VA, USA")]
            )
        ],
    )

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert jobs == []


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_respects_custom_location_env(mock_get, aws_resources: dict, monkeypatch) -> None:
    """_fetch_builtin_jobs should keep a job in a specific place when BUILTIN_LOCATION is configured."""
    monkeypatch.setenv("BUILTIN_LOCATION", "Reston, VA")
    _mock_builtin_gets(
        mock_get,
        [
            _builtin_page_html(
                [_builtin_card_html("Platform Engineer", "/job/platform-engineer/1", "Acme", "Reston, VA, USA")]
            )
        ],
    )

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert len(jobs) == 1


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_respects_custom_work_type_env(mock_get, aws_resources: dict, monkeypatch) -> None:
    """_fetch_builtin_jobs should honor a custom BUILTIN_WORK_TYPE env var."""
    monkeypatch.setenv("BUILTIN_LOCATION", "")
    monkeypatch.setenv("BUILTIN_WORK_TYPE", "hybrid")
    _mock_builtin_gets(
        mock_get,
        [_builtin_page_html([_builtin_card_html("Platform Engineer", "/job/platform-engineer/1", "Acme", "Hybrid")])],
    )

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert len(jobs) == 1


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_excludes_high_clearance_description(mock_get, aws_resources: dict) -> None:
    """_fetch_builtin_jobs should drop a posting whose description requires a high clearance,
    even when the title itself gives no indication."""
    _mock_builtin_gets(
        mock_get,
        [_builtin_page_html([_builtin_card_html("Cloud Architect", "/job/cloud-architect/1", "Acme", "Remote")])],
        description="CLEARANCE TYPE: Top Secret",
    )

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert jobs == []


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_allows_public_trust_description(mock_get, aws_resources: dict) -> None:
    """_fetch_builtin_jobs should keep a posting whose description only requires Public Trust."""
    _mock_builtin_gets(
        mock_get,
        [_builtin_page_html([_builtin_card_html("Cloud Engineer", "/job/cloud-engineer/1", "Acme", "Remote")])],
        description="Requires a Public Trust clearance.",
    )

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert len(jobs) == 1


@patch("worker.handler.requests.get")
def test_fetch_builtin_jobs_description_fetch_failure_falls_back_to_title(mock_get, aws_resources: dict) -> None:
    """_fetch_builtin_jobs should keep a relevant, clean-titled posting even if the detail fetch fails."""
    page = _builtin_page_html([_builtin_card_html("Platform Engineer", "/job/platform-engineer/1", "Acme", "Remote")])
    responses = [page, _builtin_page_html([])]

    def fake_get(*args, **kwargs):
        if kwargs.get("params", {}).get("page"):
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.text = responses.pop(0) if len(responses) > 1 else responses[0]
            return mock_resp
        raise requests.RequestException("boom")

    mock_get.side_effect = fake_get

    jobs = _fetch_builtin_jobs("https://builtin.com/jobs?search=AWS")

    assert len(jobs) == 1


# --- _filter_relevant_jobs unit tests ---


def _job(title: str, location: str = "Remote") -> dict:
    return {"title": title, "url": f"https://example.com/{title}", "location": location}


@pytest.mark.parametrize(
    "title",
    [
        "Platform Engineer",
        "Senior Platform Engineer",
        "Staff Engineer, Infrastructure",
        "Site Reliability Engineer",
        "SRE - Production",
        "Sr. SRE",
        "DevOps Engineer",
        "Lead DevOps Engineer",
        "Cloud Engineer",
        "Senior Cloud Engineer",
        "Infrastructure Engineer",
        "Staff Engineer",
    ],
)
def test_filter_passes_relevant_titles(title: str) -> None:
    """_filter_relevant_jobs should keep titles matching a target keyword."""
    result = _filter_relevant_jobs([_job(title)], "Acme")
    assert len(result) == 1


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer",
        "Product Manager",
        "Data Scientist",
        "Frontend Developer",
        "Sales Engineer",
        "Recruiting Coordinator",
    ],
)
def test_filter_drops_irrelevant_titles(title: str) -> None:
    """_filter_relevant_jobs should drop titles that don't match any keyword."""
    result = _filter_relevant_jobs([_job(title)], "Acme")
    assert len(result) == 0


@pytest.mark.parametrize(
    "title",
    [
        "Senior Manager, Platform Engineering",
        "Manager I, Engineering - Platform",
        "Director, Engineering - Infrastructure",
        "Staff Product Manager, Observability Data Platforms",
        "Senior Product Manager - Platform",
    ],
)
def test_filter_drops_management_titles_despite_keyword_match(title: str) -> None:
    """_filter_relevant_jobs should drop management/leadership titles even if they match a target keyword."""
    result = _filter_relevant_jobs([_job(title)], "Acme")
    assert len(result) == 0


@pytest.mark.parametrize(
    "title",
    [
        "Cloud Engineer (Top Secret Required)",
        "Platform Engineer - TS/SCI Required",
        "Senior DevOps Engineer (Secret Clearance)",
        "Cloud Engineer, Polygraph Required",
        "Infrastructure Engineer (Active Clearance Required)",
        "Platform Engineer (Clearance Required)",
    ],
)
def test_filter_drops_clearance_gated_titles(title: str) -> None:
    """_filter_relevant_jobs should drop titles indicating a clearance above Public Trust."""
    result = _filter_relevant_jobs([_job(title)], "Acme")
    assert len(result) == 0


def test_filter_keeps_public_trust_titles() -> None:
    """_filter_relevant_jobs should keep titles that only require a Public Trust clearance."""
    result = _filter_relevant_jobs([_job("Cloud Engineer (Public Trust)")], "Acme")
    assert len(result) == 1


@pytest.mark.parametrize(
    "location",
    [
        "Bangalore, India",
        "London, UK",
        "Toronto, Canada",
        "Tel Aviv, Israel",
        "Sydney, Australia",
        "Dublin",
        "EMEA - Remote",
        "Remote (APAC)",
        "São Paulo, Brazil",
    ],
)
def test_filter_drops_non_us_locations(location: str) -> None:
    """_filter_relevant_jobs should drop jobs whose location indicates a non-US posting."""
    result = _filter_relevant_jobs([_job("Platform Engineer", location=location)], "Acme")
    assert len(result) == 0


@pytest.mark.parametrize(
    "location",
    [
        "Remote",
        "Arlington, VA",
        "New York, NY, USA",
        "2 Locations",
        "",
        "Indianapolis, IN",
    ],
)
def test_filter_keeps_ambiguous_or_us_locations(location: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """_filter_relevant_jobs should keep US and ambiguous (no country signal) locations.

    WORK_TYPE=any disables the separate remote/hybrid/office filter so this
    test isolates the non-US location check specifically.
    """
    monkeypatch.setenv("WORK_TYPE", "any")
    result = _filter_relevant_jobs([_job("Platform Engineer", location=location)], "Acme")
    assert len(result) == 1


def test_filter_drops_non_remote_jobs_by_default() -> None:
    """_filter_relevant_jobs should drop a non-Built-In job whose location isn't remote by default."""
    result = _filter_relevant_jobs([_job("Platform Engineer", location="Arlington, VA")], "Acme")
    assert result == []


def test_filter_keeps_remote_jobs_by_default() -> None:
    """_filter_relevant_jobs should keep a non-Built-In job whose location is remote."""
    result = _filter_relevant_jobs([_job("Platform Engineer", location="Remote")], "Acme")
    assert len(result) == 1


def test_filter_exempts_builtin_jobs_from_work_type_check() -> None:
    """_filter_relevant_jobs should not apply LOCATION/WORK_TYPE to jobs carrying their own "company" key.

    Built In jobs set this key (see _fetch_builtin_jobs) and are already
    filtered by their own independent BUILTIN_LOCATION/BUILTIN_WORK_TYPE
    config before reaching here — they shouldn't also be gated by the
    LOCATION/WORK_TYPE defaults meant for the curated company list.
    """
    job = {
        "title": "Platform Engineer",
        "url": "https://builtin.com/job/1",
        "location": "Arlington, VA",
        "company": "ZS",
    }
    result = _filter_relevant_jobs([job], "Built In - AWS Search")
    assert len(result) == 1


def test_filter_respects_custom_work_type_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_filter_relevant_jobs should honor a custom WORK_TYPE for non-Built-In jobs."""
    monkeypatch.setenv("WORK_TYPE", "hybrid")
    result = _filter_relevant_jobs([_job("Platform Engineer", location="Hybrid")], "Acme")
    assert len(result) == 1
    result = _filter_relevant_jobs([_job("Platform Engineer", location="Remote")], "Acme")
    assert result == []


def test_filter_mixed_batch_keeps_only_matches() -> None:
    """_filter_relevant_jobs should keep only the matching subset of a mixed list."""
    jobs = [
        _job("Platform Engineer"),
        _job("Software Engineer"),
        _job("DevOps Engineer"),
        _job("Product Manager"),
        _job("Senior Manager, Platform Engineering"),
    ]
    result = _filter_relevant_jobs(jobs, "Acme")
    assert len(result) == 2
    titles = {j["title"] for j in result}
    assert titles == {"Platform Engineer", "DevOps Engineer"}


def test_filter_empty_input_returns_empty() -> None:
    """_filter_relevant_jobs should handle an empty input list gracefully."""
    assert _filter_relevant_jobs([], "Acme") == []


# --- _requires_excluded_clearance unit tests ---


@pytest.mark.parametrize(
    "text",
    [
        "Must have an active Top Secret clearance.",
        "TS/SCI required for this role.",
        "Candidates must hold a current Secret clearance.",
        "Full scope polygraph required.",
        "This role requires a CI Poly.",
        "SCI clearance is required.",
        "Active clearance required to start.",
        "Security clearance is required for this position.",
        "Clearance sponsorship available for the right candidate.",
    ],
)
def test_requires_excluded_clearance_true_for_high_or_unspecified(text: str) -> None:
    """_requires_excluded_clearance should flag high-tier and unspecified clearance mentions."""
    assert _requires_excluded_clearance(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "No clearance required for this role.",
        "This position requires a Public Trust clearance.",
        "Candidates must be eligible for Public Trust.",
        "Remote-friendly software engineering role.",
    ],
)
def test_requires_excluded_clearance_false_for_public_trust_or_none(text: str) -> None:
    """_requires_excluded_clearance should allow Public Trust and clearance-free postings."""
    assert _requires_excluded_clearance(text) is False


def test_requires_excluded_clearance_high_tier_wins_over_public_trust_mention() -> None:
    """A posting mentioning both Public Trust and a higher tier should still be excluded."""
    text = "Public Trust for some roles; this position requires an active Top Secret clearance."
    assert _requires_excluded_clearance(text) is True


def test_requires_excluded_clearance_ignores_eppa_boilerplate() -> None:
    """The standard EPPA legal notice mentions 'polygraph' but isn't a clearance requirement.

    Regression test: this boilerplate is present on nearly every US company's
    careers page and previously flagged 100% of one company's postings.
    """
    text = (
        "Software Engineer. We are an equal opportunity employer. "
        "Employee Polygraph Protection Act (EPPA) Poster and other required notices apply."
    )
    assert _requires_excluded_clearance(text) is False


def test_requires_excluded_clearance_still_catches_real_polygraph_mention() -> None:
    """A genuine clearance-related polygraph mention outside the EPPA notice should still exclude."""
    assert _requires_excluded_clearance("Must be willing to submit to a polygraph examination.") is True


# --- _is_non_us_location unit tests ---


@pytest.mark.parametrize(
    "location",
    [
        "Bangalore, India",
        "London, United Kingdom",
        "London, UK",
        "Toronto, Canada",
        "Tel Aviv, Israel",
        "2 Locations - EMEA",
        "Remote (APAC)",
        "Berlin, Germany",
        "Dublin, Ireland",
        "Singapore",
    ],
)
def test_is_non_us_location_true(location: str) -> None:
    """_is_non_us_location should flag known non-US countries, regions, and cities."""
    assert _is_non_us_location(location) is True


@pytest.mark.parametrize(
    "location",
    [
        "",
        "Remote",
        "Arlington, VA",
        "New York, NY, USA",
        "2 Locations",
        "Milwaukee, WI",
        "Indianapolis, IN",
    ],
)
def test_is_non_us_location_false(location: str) -> None:
    """_is_non_us_location should not flag US locations or ambiguous strings.

    Milwaukee/Indianapolis are regression cases: naive substring matching
    (without word boundaries) would incorrectly flag them via "uk" and
    "india" respectively.
    """
    assert _is_non_us_location(location) is False


# --- _builtin_location_matches unit tests ---


@pytest.mark.parametrize(
    "location",
    ["Remote", "Remote - USA", "Fully Distributed", "Anywhere in the US"],
)
def test_builtin_location_matches_default_remote(location: str) -> None:
    """_builtin_location_matches should keep remote jobs under the default BUILTIN_WORK_TYPE."""
    assert _builtin_location_matches(location) is True


@pytest.mark.parametrize("location", ["Reston, VA", "Arlington, VA", "Hybrid", "New York, NY", "", "In-Office"])
def test_builtin_location_matches_default_excludes_non_remote_locations(location: str) -> None:
    """_builtin_location_matches should drop any non-remote location by default (location match is disabled)."""
    assert _builtin_location_matches(location) is False


def test_builtin_location_matches_custom_location_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_builtin_location_matches should honor a custom BUILTIN_LOCATION."""
    monkeypatch.setenv("BUILTIN_LOCATION", "Austin, TX")
    monkeypatch.setenv("BUILTIN_WORK_TYPE", "any")
    assert _builtin_location_matches("Austin, TX, USA") is True


def test_builtin_location_matches_custom_work_type_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_builtin_location_matches should honor a custom BUILTIN_WORK_TYPE."""
    monkeypatch.setenv("BUILTIN_LOCATION", "")
    monkeypatch.setenv("BUILTIN_WORK_TYPE", "hybrid")
    assert _builtin_location_matches("Hybrid") is True
    assert _builtin_location_matches("Remote") is False


def test_builtin_location_matches_work_type_any_matches_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """BUILTIN_WORK_TYPE=any should disable the work-type half of the check."""
    monkeypatch.setenv("BUILTIN_LOCATION", "")
    monkeypatch.setenv("BUILTIN_WORK_TYPE", "any")
    assert _builtin_location_matches("Wherever, XY") is True


def test_builtin_location_matches_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """_builtin_location_matches should match regardless of case."""
    assert _builtin_location_matches("REMOTE") is True

    monkeypatch.setenv("BUILTIN_LOCATION", "Reston, VA")
    assert _builtin_location_matches("reston, va") is True


def test_builtin_location_matches_any_with_no_location_ignores_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """BUILTIN_WORK_TYPE=any with no BUILTIN_LOCATION should disable the check entirely, blank location included."""
    monkeypatch.setenv("BUILTIN_LOCATION", "")
    monkeypatch.setenv("BUILTIN_WORK_TYPE", "any")
    assert _builtin_location_matches("") is True


# --- _location_matches unit tests ---


@pytest.mark.parametrize(
    "location",
    ["Remote", "Remote - USA", "Fully Distributed", "Anywhere in the US"],
)
def test_location_matches_default_remote(location: str) -> None:
    """_location_matches should keep remote jobs under the default WORK_TYPE."""
    assert _location_matches(location) is True


@pytest.mark.parametrize("location", ["Reston, VA", "Arlington, VA", "Hybrid", "New York, NY", "", "In-Office"])
def test_location_matches_default_excludes_non_remote_locations(location: str) -> None:
    """_location_matches should drop any non-remote location by default (location match is disabled)."""
    assert _location_matches(location) is False


def test_location_matches_custom_location_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_location_matches should honor a custom LOCATION, independent of BUILTIN_LOCATION."""
    monkeypatch.setenv("LOCATION", "Reston, VA")
    monkeypatch.setenv("WORK_TYPE", "any")
    assert _location_matches("Reston, VA, USA") is True


def test_location_matches_custom_work_type_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_location_matches should honor a custom WORK_TYPE, independent of BUILTIN_WORK_TYPE."""
    monkeypatch.setenv("WORK_TYPE", "hybrid")
    assert _location_matches("Hybrid") is True
    assert _location_matches("Remote") is False


def test_location_matches_is_independent_of_builtin_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """LOCATION/WORK_TYPE and BUILTIN_LOCATION/BUILTIN_WORK_TYPE should be entirely independent settings."""
    monkeypatch.setenv("BUILTIN_LOCATION", "Reston, VA")
    monkeypatch.setenv("BUILTIN_WORK_TYPE", "any")
    # LOCATION/WORK_TYPE are untouched, so _location_matches still uses its own defaults.
    assert _location_matches("Reston, VA, USA") is False
    assert _location_matches("Remote") is True
