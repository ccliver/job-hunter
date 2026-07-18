# CLAUDE.md

## Project overview

job-hunter is an AWS serverless application that monitors company careers pages and emails a daily digest of new job postings. It uses three Lambda functions orchestrated by EventBridge, with SQS for fan-out, DynamoDB for storage, and SES for email delivery.

```
EventBridge cron → Orchestrator Lambda → SQS (1 msg/company)
                                              ↓
                    Worker Lambda (Greenhouse/Lever/Workday/Built In APIs,
                                    or Playwright + Strands/Bedrock for unknown ATS)
                                              ↓
                                       DynamoDB jobs table
                                              ↑
EventBridge cron (+30min) → Notifier Lambda → SES email digest
```

## Repo layout

```
companies/
  companies.json                 # seed data for the companies table (task seed)
src/
  conftest.py                    # sets fake AWS creds at module level for pytest
  orchestrator/
    orchestrator/handler.py      # scans companies table, fans out SQS
    tests/test_handler.py
  worker/
    worker/handler.py            # _fetch_jobs dispatches to an ATS fetcher, writes jobs
    tests/test_handler.py
  notifier/
    notifier/handler.py          # queries recent jobs, sends SES digest
    tests/test_handler.py
terraform/
  main.tf                        # all resources (IAM, Lambda, SQS, DynamoDB, EventBridge)
  versions.tf                    # required_providers
  providers.tf                   # AWS provider + default_tags
  backend.tf                     # S3 backend stub (populated via backend.hcl)
  variables.tf
  outputs.tf
Taskfile.yml                     # task apply / task destroy (uses AWS_PROFILE=lab)
```

## Development commands

```bash
uv sync --all-packages           # install all workspace packages + dev deps
uv run pytest                    # run tests
uv run pytest --tb=short -q      # terse output
uv run ruff check src/           # lint
uv run ruff format src/          # format
uv run ty check src/             # type check
uv run pre-commit run --all-files  # run all pre-commit hooks manually
task apply                       # build all artifacts + terraform init/apply (uses AWS_PROFILE=lab)
task destroy                     # terraform destroy
```

## Python conventions

- **uv workspace** with three packages under `src/`. Each Lambda has its own `pyproject.toml` so dependencies are isolated per function.
- All handlers follow the same pattern: module-level boto3 clients (`dynamodb = boto3.resource("dynamodb")`), env vars read inside the handler function.
- Job deduplication key: `SHA-256(company|title|url)` → `job_id` DynamoDB partition key. See `worker/handler.py:_make_job_id`.
- `worker/handler.py:_fetch_jobs` dispatches on the `ats` field to one of: `greenhouse`, `lever`, `workday` (all JSON API calls, no LLM), `builtin` (scrapes a Built In search results page — server-rendered HTML, no LLM; aggregates across employers so each job carries its own `company` key and jobs from companies already tracked directly are skipped, via a `dynamodb:Scan` on `COMPANIES_TABLE`), or the `unknown` default which drives a Playwright + Strands/Bedrock (Claude Haiku) scrape.
- `worker/handler.py:_filter_relevant_jobs` also drops jobs indicating a clearance requirement above Public Trust (`_requires_excluded_clearance`), title-only and applied uniformly across every backend. `_fetch_greenhouse_jobs` additionally checks the full job description (Greenhouse's list API returns it for free via `content=true`). `_fetch_workday_jobs` does too, via a per-posting follow-up request to the Workday job-detail endpoint (`.../wday/cxs/{tenant}/{site}{externalPath}`) — but only for postings whose title already passes `_title_looks_relevant`, to avoid one extra request per irrelevant posting; a failed detail fetch falls back to title-only checking rather than dropping the job. Built In and the LLM path rely on title/page text only, except the LLM prompt is also instructed to exclude clearance-gated postings it sees in the full page text.
- `worker/handler.py:_filter_relevant_jobs` also drops jobs whose `location` matches `_is_non_us_location` — a word-boundary regex over a curated list of countries/regions/offshore-hub cities (`_NON_US_LOCATION_KEYWORDS`). Defaults to keeping ambiguous locations (bare "Remote", "N Locations", empty) rather than risk hiding a real US posting; deliberately omits US/non-US-ambiguous names (e.g. "Georgia") for the same reason.
- `notifier/handler.py:_build_email_body` renders an HTML digest (styled, table-based for email-client compatibility) plus a plain-text fallback, both grouped by company with location shown. All interpolated values are HTML-escaped.
- Lambda handlers return a summary dict (`{"published": n}` etc.) for easy CloudWatch Insights querying.

## Testing conventions

- Tests use **moto** (`@mock_aws` / `with mock_aws()`) for all AWS service mocking — no MagicMock for boto3 calls.
- `src/conftest.py` sets fake AWS credentials and `AWS_CONFIG_FILE=/dev/null` at **module level** (not in a fixture). This is required because handlers create boto3 clients at import time; a fixture would run too late.
- Each test file has an `aws_resources` pytest fixture that creates real moto-backed infrastructure inside `with mock_aws(): ... yield`. Tests run inside that context.
- `_fetch_default_jobs` (the Playwright/Strands/Bedrock path) is still patched with `unittest.mock.patch` since Bedrock is not part of moto's scope. The `greenhouse`/`lever`/`workday`/`builtin` fetchers make plain `requests` calls and are tested by mocking `requests` directly instead; `builtin` additionally touches DynamoDB (scans `COMPANIES_TABLE`), which is backed by moto like everything else.
- pytest uses `--import-mode=importlib` (set in root `pyproject.toml`) to avoid module name collisions across the three `test_handler.py` files.
- Add tests for every new handler behaviour; verify state in DynamoDB/SQS directly rather than asserting on mock call counts.

## Terraform conventions

- All resources use `local.prefix` (`"job-hunter"`) for naming.
- Single deployment target — no environment variable, no workspaces.
- Backend config is in `backend.hcl` (gitignored, account-specific) and passed via `terraform init -backend-config=backend.hcl`.
- Lambda ZIPs for orchestrator/notifier are built by `task build` (pip-installs dependencies into `terraform/.build/{name}`, then zips); `main.tf` just references the resulting `.build/*.zip` via `filebase64sha256` for `source_code_hash`. The worker ships as a container image instead (`task build-worker`: build, push to ECR, then `aws lambda update-function-code` — the Lambda resource's `image_uri` is a static `:latest` tag Terraform never sees change, so that explicit CLI call is what actually deploys new worker code).
- IAM policies follow least-privilege per Lambda; Bedrock policy is currently `Resource = "*"` pending a known model ARN.
- SQS queues use `sqs_managed_sse_enabled = true`.
- DynamoDB tables use `PAY_PER_REQUEST` billing.

## Open TODOs

- `orchestrator/handler.py`: add DynamoDB scan pagination for large company lists.
- `terraform/main.tf` jobs table: add GSI on `discovered_at` for efficient Notifier time-range queries (currently full table scan).
- `terraform/main.tf` Bedrock IAM: scope `Resource` to specific model ARN once known.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs two jobs on PRs and pushes to main:
1. **pre-commit** — runs all hooks (`ruff`, `ty`, `terraform_fmt`, `terraform_validate`, `terraform_docs`, `terraform_tflint`, `terraform_trivy`, `check-merge-conflict`, `end-of-file-fixer`). Requires terraform, tflint (v0.55.0), trivy (v0.61.0), and terraform-docs (v0.19.0) installed as separate steps before the pre-commit run.
2. **Tests** — `uv run pytest --tb=short -q`

pytest (`stages: [pre-push]`) is excluded from the pre-commit job since it runs in the dedicated test job.
