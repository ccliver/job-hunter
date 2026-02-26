# job-hunter

Automated job board monitor. An EventBridge cron fans out one Lambda per company to scrape careers pages using a Strands/Bedrock agent, deduplicates results in DynamoDB, and emails a daily digest via SES.

## Architecture

```
EventBridge (cron)
       │
       ▼
┌─────────────────┐     scan      ┌──────────────────┐
│  Orchestrator   │──────────────▶│  DynamoDB        │
│  Lambda         │               │  companies table │
└────────┬────────┘               └──────────────────┘
         │ SendMessage (1 per company)
         ▼
┌─────────────────┐
│   SQS Queue     │
└────────┬────────┘
         │ trigger (batch_size=1)
         ▼
┌─────────────────┐   Bedrock     ┌──────────────────┐
│  Worker Lambda  │──────────────▶│  Claude Haiku    │
│  (Strands agent)│               │  (via Bedrock)   │
└────────┬────────┘               └──────────────────┘
         │ PutItem (deduplicated)
         ▼
┌──────────────────┐
│  DynamoDB        │
│  jobs table      │
└──────────────────┘
         ▲
         │ Scan (recent jobs)
┌────────┴────────┐
│  Notifier       │◀── EventBridge (cron, +30min)
│  Lambda         │
└────────┬────────┘
         │ SendEmail
         ▼
       SES → your inbox
```

## DynamoDB Tables

### `companies`
| Attribute    | Type | Role          |
|-------------|------|---------------|
| company_name | S    | Partition key |
| careers_url  | S    | Attribute     |

### `jobs`
| Attribute     | Type | Role          |
|--------------|------|---------------|
| job_id        | S    | Partition key (SHA-256 of company+title+url) |
| company       | S    | Attribute     |
| title         | S    | Attribute     |
| url           | S    | Attribute     |
| location      | S    | Attribute     |
| discovered_at | S    | ISO-8601 timestamp |

## Project Layout

```
job-hunter/
├── pyproject.toml              # uv workspace root
├── .python-version             # 3.13
├── .pre-commit-config.yaml
├── .tflint.hcl                 # tflint AWS ruleset config
├── Taskfile.yml                # deploy / destroy tasks
├── .github/
│   └── workflows/ci.yml
├── src/
│   ├── conftest.py             # pytest AWS credential setup
│   ├── orchestrator/
│   │   ├── pyproject.toml
│   │   ├── orchestrator/
│   │   │   └── handler.py
│   │   └── tests/
│   │       └── test_handler.py
│   ├── worker/
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
    ├── main.tf                 # all resources
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

Deploys are managed via [Task](https://taskfile.dev). The Terraform backend is configured via a gitignored `backend.hcl` file.

```bash
# First-time setup: create backend.hcl with your S3 state bucket details
cat > backend.hcl <<EOF
bucket = "your-terraform-state-bucket"
key    = "job-hunter/terraform.tfstate"
region = "us-east-1"
EOF

# Create terraform.tfvars with required variables
cat > terraform/terraform.tfvars <<EOF
ses_from_address = "you@yourdomain.com"
ses_to_address   = "you@yourdomain.com"
EOF

task deploy   # terraform init + apply
task destroy  # terraform destroy
```

## Seeding Companies

Add rows to the DynamoDB `companies` table manually or via script:

```bash
aws dynamodb put-item \
  --table-name job-hunter-companies \
  --item '{"company_name":{"S":"Acme Corp"},"careers_url":{"S":"https://acme.com/jobs"}}'
```

## CI

Pull requests run two jobs: **pre-commit** (ruff, ty, terraform fmt/validate/docs/tflint/trivy) and **Tests** (pytest). All must pass before merge.
