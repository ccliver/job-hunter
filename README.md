# job-hunter

Automated job board monitor. An EventBridge cron fans out one Lambda per company to scrape careers pages, deduplicates results in DynamoDB, and emails a daily digest via SES.

The worker supports three scraping backends:
- **Greenhouse / Lever** — direct JSON API calls, no LLM needed
- **Custom careers pages** — headless Chromium (Playwright) renders the page, then a Strands/Bedrock agent (Claude Haiku) extracts structured listings

## Architecture

```
EventBridge (cron, 09:00 UTC)
       │
       ▼
┌─────────────────┐     scan      ┌──────────────────┐
│  Orchestrator   │──────────────▶│  DynamoDB        │
│  Lambda         │               │  companies table │
└────────┬────────┘               └──────────────────┘
         │ SendMessage (1 per company)
         ▼
┌─────────────────┐     ┌──────────────────┐
│   SQS Queue     │────▶│  SQS DLQ         │
└────────┬────────┘     └──────────────────┘
         │ trigger (batch_size=1)
         ▼
┌─────────────────┐
│  Worker Lambda  │── Greenhouse/Lever ──▶ JSON API
│  (container)    │
│                 │── Custom page ──▶ Playwright + Strands/Bedrock (Claude Haiku)
└────────┬────────┘
         │ PutItem (deduplicated)
         ▼
┌──────────────────┐
│  DynamoDB        │
│  jobs table      │
└──────────────────┘
         ▲
         │ Scan (recent jobs)
┌────────┴────────┐
│  Notifier       │◀── EventBridge (cron, 09:30 UTC)
│  Lambda         │
└────────┬────────┘
         │ SendEmail
         ▼
       SES → your inbox
```

## DynamoDB Tables

### `job-hunter-companies`
| Attribute    | Type | Role          |
|-------------|------|---------------|
| company_name | S    | Partition key |
| careers_url  | S    | Careers page URL |
| ats          | S    | ATS backend (`greenhouse`, `lever`, or `unknown`) |

### `job-hunter-jobs`
| Attribute     | Type | Role          |
|--------------|------|---------------|
| job_id        | S    | Partition key — SHA-256 of `company\|title\|url` |
| company       | S    | Company name |
| title         | S    | Job title |
| url           | S    | Job posting URL |
| location      | S    | Location string |
| discovered_at | S    | ISO-8601 timestamp |

## Project Layout

```
job-hunter/
├── pyproject.toml              # uv workspace root
├── .python-version             # 3.13
├── .pre-commit-config.yaml
├── .tflint.hcl                 # tflint AWS ruleset config
├── Taskfile.yml                # operational tasks (see below)
├── companies/
│   └── companies.json          # seed data for the companies table
├── .github/
│   └── workflows/ci.yml
├── src/
│   ├── conftest.py             # sets fake AWS creds at module level for pytest
│   ├── orchestrator/
│   │   ├── pyproject.toml
│   │   ├── orchestrator/
│   │   │   └── handler.py
│   │   └── tests/
│   │       └── test_handler.py
│   ├── worker/
│   │   ├── Dockerfile          # container image Lambda (Playwright + Chromium)
│   │   ├── pyproject.toml
│   │   ├── worker/
│   │   │   └── handler.py
│   │   └── tests/
│   │       └── test_handler.py
│   └── notifier/
│       ├── pyproject.toml
│       ├── notifier/
│       │   └── handler.py
│       └── tests/
│           └── test_handler.py
└── terraform/
    ├── main.tf                 # all resources (IAM, Lambda, SQS, DynamoDB, ECR, EventBridge)
    ├── versions.tf
    ├── providers.tf
    ├── backend.tf
    ├── variables.tf
    └── outputs.tf
```

## Local Development

```bash
# Install uv (https://docs.astral.sh/uv/)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install all workspace packages + dev deps
uv sync --all-packages

# Run tests
uv run pytest

# Lint + format
uv run ruff check src/
uv run ruff format src/

# Type check
uv run ty check src/

# Install pre-commit hooks
uv run pre-commit install
uv run pre-commit install --hook-type pre-push  # for pytest
```

## Infrastructure

Deploys are managed via [Task](https://taskfile.dev). The Terraform backend is configured via a gitignored `terraform/backend.hcl` file.

### First-time setup

```bash
# 1. Create terraform/backend.hcl with your S3 state bucket details
cat > terraform/backend.hcl <<EOF
bucket = "your-terraform-state-bucket"
key    = "job-hunter/terraform.tfstate"
region = "us-east-1"
EOF

# 2. Create terraform/terraform.tfvars with required variables
cat > terraform/terraform.tfvars <<EOF
ses_from_address = "you@yourdomain.com"
ses_to_address   = "you@yourdomain.com"
EOF

# 3. Enable Claude Haiku model access in the AWS Bedrock console
#    (us-east-1 → Model access → Anthropic Claude Haiku)

# 4. Deploy
task apply    # terraform init + apply (creates ECR, builds & pushes worker image, then full apply)
```

### Task reference

| Task | Description |
|------|-------------|
| `task apply` | Build all artifacts and deploy infrastructure |
| `task destroy` | Destroy all infrastructure |
| `task build` | Build orchestrator and notifier Lambda ZIPs |
| `task build-worker` | Build and push the worker container image to ECR |
| `task ecr-login` | Authenticate Docker to ECR |
| `task invoke` | Full end-to-end test: orchestrator → workers → notifier |
| `task logs-worker` | Print the worker Lambda's most recent CloudWatch log streams |
| `task seed` | Seed the DynamoDB companies table from `companies/companies.json` |
| `task flush-jobs` | Delete all items from the DynamoDB jobs table |
| `task dynamo-disable-protection` | Disable deletion protection on the companies table (run before `destroy`) |
| `task ecr-delete-images` | Delete all images from the worker ECR repository (run before `destroy`) |

### Teardown

DynamoDB deletion protection and a non-empty ECR repository will cause `terraform destroy` to fail. Run these first:

```bash
task dynamo-disable-protection
task ecr-delete-images
task destroy
```

## Seeding Companies

Edit `companies/companies.json` and run:

```bash
task seed
```

Each entry requires `company_name`, `careers_url`, and `ats` (`greenhouse`, `lever`, or `unknown`):

```json
[
  {"company_name": "Acme Corp", "careers_url": "https://boards-api.greenhouse.io/v1/boards/acme/jobs", "ats": "greenhouse"},
  {"company_name": "Example Inc", "careers_url": "https://example.com/careers", "ats": "unknown"}
]
```

## CI

Pull requests run two jobs: **pre-commit** (ruff, ty, terraform fmt/validate/docs/tflint/trivy) and **Tests** (pytest). All must pass before merge.
