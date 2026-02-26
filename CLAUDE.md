# CLAUDE.md

## Project overview

job-hunter is an AWS serverless application that monitors company careers pages and emails a daily digest of new job postings. It uses three Lambda functions orchestrated by EventBridge, with SQS for fan-out, DynamoDB for storage, and SES for email delivery.

```
EventBridge cron → Orchestrator Lambda → SQS (1 msg/company)
                                              ↓
                              Worker Lambda (Strands + Bedrock/Claude Haiku)
                                              ↓
                                       DynamoDB jobs table
                                              ↑
EventBridge cron (+30min) → Notifier Lambda → SES email digest
```

## Repo layout

```
src/
  conftest.py                    # sets fake AWS creds at module level for pytest
  orchestrator/
    orchestrator/handler.py      # scans companies table, fans out SQS
    tests/test_handler.py
  worker/
    worker/handler.py            # Strands agent scrapes careers page, writes jobs
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
Taskfile.yml                     # task deploy / task destroy (uses AWS_PROFILE=lab)
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
task deploy                      # terraform init + apply (uses AWS_PROFILE=lab)
task destroy                     # terraform destroy
```

## Python conventions

- **uv workspace** with three packages under `src/`. Each Lambda has its own `pyproject.toml` so dependencies are isolated per function.
- All handlers follow the same pattern: module-level boto3 clients (`dynamodb = boto3.resource("dynamodb")`), env vars read inside the handler function.
- Job deduplication key: `SHA-256(company|title|url)` → `job_id` DynamoDB partition key. See `worker/handler.py:_make_job_id`.
- `_scrape_jobs()` in `worker/handler.py` is the main TODO — Strands agent not yet implemented.
- Lambda handlers return a summary dict (`{"published": n}` etc.) for easy CloudWatch Insights querying.

## Testing conventions

- Tests use **moto** (`@mock_aws` / `with mock_aws()`) for all AWS service mocking — no MagicMock for boto3 calls.
- `src/conftest.py` sets fake AWS credentials and `AWS_CONFIG_FILE=/dev/null` at **module level** (not in a fixture). This is required because handlers create boto3 clients at import time; a fixture would run too late.
- Each test file has an `aws_resources` pytest fixture that creates real moto-backed infrastructure inside `with mock_aws(): ... yield`. Tests run inside that context.
- `_scrape_jobs` is still patched with `unittest.mock.patch` since Bedrock is not part of moto's scope.
- pytest uses `--import-mode=importlib` (set in root `pyproject.toml`) to avoid module name collisions across the three `test_handler.py` files.
- Add tests for every new handler behaviour; verify state in DynamoDB/SQS directly rather than asserting on mock call counts.

## Terraform conventions

- All resources use `local.prefix` (`"job-hunter"`) for naming.
- Single deployment target — no environment variable, no workspaces.
- Backend config is in `backend.hcl` (gitignored, account-specific) and passed via `terraform init -backend-config=backend.hcl`.
- Lambda ZIPs are built by `archive_file` data sources in `main.tf` — packaging strategy is a known TODO (no pip install into zip yet).
- IAM policies follow least-privilege per Lambda; Bedrock policy is currently `Resource = "*"` pending a known model ARN.
- SQS queues use `sqs_managed_sse_enabled = true`.
- DynamoDB tables use `PAY_PER_REQUEST` billing.

## Open TODOs

- `worker/handler.py` `_scrape_jobs`: implement Strands agent with Bedrock tool calls.
- `orchestrator/handler.py`: add DynamoDB scan pagination for large company lists.
- `terraform/main.tf` jobs table: add GSI on `discovered_at` for efficient Notifier time-range queries (currently full table scan).
- `terraform/main.tf` Bedrock IAM: scope `Resource` to specific model ARN once known.
- Lambda packaging: replace `archive_file` source-dir zips with a proper packaging step that includes pip-installed dependencies.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs two jobs on PRs and pushes to main:
1. **pre-commit** — runs all hooks (`ruff`, `ty`, `terraform_fmt`, `terraform_validate`, `terraform_docs`, `terraform_tflint`, `terraform_trivy`, `check-merge-conflict`, `end-of-file-fixer`). Requires terraform, tflint (v0.55.0), trivy (v0.61.0), and terraform-docs (v0.19.0) installed as separate steps before the pre-commit run.
2. **Tests** — `uv run pytest --tb=short -q`

pytest (`stages: [pre-push]`) is excluded from the pre-commit job since it runs in the dedicated test job.
