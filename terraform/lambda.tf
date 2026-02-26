# Lambda deployment packages are built by packaging each src/* package.
# The CI/CD pipeline should build these ZIPs and place them at these paths,
# or use a tool like `uv build --wheel` + layer approach.
# TODO: replace local_file data sources with your chosen packaging strategy
# (e.g. Terraform null_resource + pip install, or S3 object references).

data "archive_file" "orchestrator" {
  type        = "zip"
  source_dir  = "${path.module}/../src/orchestrator"
  output_path = "${path.module}/.build/orchestrator.zip"
  excludes    = ["__pycache__", "*.pyc", "tests"]
}

data "archive_file" "worker" {
  type        = "zip"
  source_dir  = "${path.module}/../src/worker"
  output_path = "${path.module}/.build/worker.zip"
  excludes    = ["__pycache__", "*.pyc", "tests"]
}

data "archive_file" "notifier" {
  type        = "zip"
  source_dir  = "${path.module}/../src/notifier"
  output_path = "${path.module}/.build/notifier.zip"
  excludes    = ["__pycache__", "*.pyc", "tests"]
}

# ── Orchestrator Lambda ───────────────────────────────────────────────────────

resource "aws_lambda_function" "orchestrator" {
  function_name    = "${local.prefix}-orchestrator"
  role             = aws_iam_role.orchestrator.arn
  handler          = "orchestrator.handler.handler"
  runtime          = "python3.13"
  filename         = data.archive_file.orchestrator.output_path
  source_code_hash = data.archive_file.orchestrator.output_base64sha256
  timeout          = var.lambda_timeout_seconds
  memory_size      = var.lambda_memory_mb

  environment {
    variables = {
      COMPANIES_TABLE  = aws_dynamodb_table.companies.name
      WORKER_QUEUE_URL = aws_sqs_queue.worker.url
    }
  }
}

resource "aws_cloudwatch_log_group" "orchestrator" {
  name              = "/aws/lambda/${aws_lambda_function.orchestrator.function_name}"
  retention_in_days = 14
}

# ── Worker Lambda ─────────────────────────────────────────────────────────────

resource "aws_lambda_function" "worker" {
  function_name    = "${local.prefix}-worker"
  role             = aws_iam_role.worker.arn
  handler          = "worker.handler.handler"
  runtime          = "python3.13"
  filename         = data.archive_file.worker.output_path
  source_code_hash = data.archive_file.worker.output_base64sha256
  timeout          = var.lambda_timeout_seconds
  memory_size      = var.lambda_memory_mb

  reserved_concurrent_executions = var.worker_concurrency

  environment {
    variables = {
      JOBS_TABLE     = aws_dynamodb_table.jobs.name
      BEDROCK_REGION = var.aws_region
      BEDROCK_MODEL  = var.bedrock_model_id
    }
  }
}

resource "aws_lambda_event_source_mapping" "worker_sqs" {
  event_source_arn = aws_sqs_queue.worker.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 1 # one company per invocation for isolation
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${aws_lambda_function.worker.function_name}"
  retention_in_days = 14
}

# ── Notifier Lambda ───────────────────────────────────────────────────────────

resource "aws_lambda_function" "notifier" {
  function_name    = "${local.prefix}-notifier"
  role             = aws_iam_role.notifier.arn
  handler          = "notifier.handler.handler"
  runtime          = "python3.13"
  filename         = data.archive_file.notifier.output_path
  source_code_hash = data.archive_file.notifier.output_base64sha256
  timeout          = var.lambda_timeout_seconds
  memory_size      = var.lambda_memory_mb

  environment {
    variables = {
      JOBS_TABLE       = aws_dynamodb_table.jobs.name
      SES_FROM_ADDRESS = var.ses_from_address
      SES_TO_ADDRESS   = var.ses_to_address
      LOOKBACK_MINUTES = tostring(var.lookback_minutes)
      SES_REGION       = var.aws_region
    }
  }
}

resource "aws_cloudwatch_log_group" "notifier" {
  name              = "/aws/lambda/${aws_lambda_function.notifier.function_name}"
  retention_in_days = 14
}
