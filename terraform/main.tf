locals {
  prefix = "job-hunter"
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "orchestrator" {
  name               = "${local.prefix}-orchestrator"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy" "orchestrator" {
  name = "${local.prefix}-orchestrator-policy"
  role = aws_iam_role.orchestrator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoDBScanCompanies"
        Effect   = "Allow"
        Action   = ["dynamodb:Scan"]
        Resource = aws_dynamodb_table.companies.arn
      },
      {
        Sid      = "SQSSendMessage"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.worker.arn
      },
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

resource "aws_iam_role" "worker" {
  name               = "${local.prefix}-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy" "worker" {
  name = "${local.prefix}-worker-policy"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoDBWriteJobs"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem"]
        Resource = aws_dynamodb_table.jobs.arn
      },
      {
        Sid    = "SQSReceive"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.worker.arn
      },
      {
        Sid    = "BedrockInvoke"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        # TODO: scope to specific model ARN once account/region are known
        Resource = "*"
      },
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

resource "aws_iam_role" "notifier" {
  name               = "${local.prefix}-notifier"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy" "notifier" {
  name = "${local.prefix}-notifier-policy"
  role = aws_iam_role.notifier.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBScanJobs"
        Effect = "Allow"
        Action = ["dynamodb:Scan", "dynamodb:Query"]
        Resource = [
          aws_dynamodb_table.jobs.arn,
          "${aws_dynamodb_table.jobs.arn}/index/*"
        ]
      },
      {
        Sid      = "SESSendEmail"
        Effect   = "Allow"
        Action   = ["ses:SendEmail", "ses:SendRawEmail"]
        Resource = "*"
      },
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}


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

resource "aws_lambda_function" "worker" {
  function_name    = "${local.prefix}-worker"
  role             = aws_iam_role.worker.arn
  handler          = "worker.handler.handler"
  runtime          = "python3.13"
  filename         = data.archive_file.worker.output_path
  source_code_hash = data.archive_file.worker.output_base64sha256
  timeout          = var.lambda_timeout_seconds
  memory_size      = var.lambda_memory_mb

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


resource "aws_sqs_queue" "worker_dlq" {
  name                      = "${local.prefix}-worker-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "worker" {
  name                       = "${local.prefix}-worker"
  visibility_timeout_seconds = var.lambda_timeout_seconds + 30
  message_retention_seconds  = 86400 # 1 day
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.worker_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_policy" "worker" {
  queue_url = aws_sqs_queue.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowOrchestratorSend"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.orchestrator.arn }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.worker.arn
      }
    ]
  })
}


resource "aws_dynamodb_table" "companies" {
  name         = "${local.prefix}-companies"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "company_name"

  attribute {
    name = "company_name"
    type = "S"
  }

  tags = {
    Name = "${local.prefix}-companies"
  }
}

resource "aws_dynamodb_table" "jobs" {
  name         = "${local.prefix}-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  # TODO: add a GSI on discovered_at so the Notifier can do efficient
  # time-range queries instead of a full table scan.
  # attribute {
  #   name = "discovered_at"
  #   type = "S"
  # }
  # global_secondary_index {
  #   name               = "discovered_at-index"
  #   hash_key           = "discovered_at"
  #   projection_type    = "ALL"
  # }

  tags = {
    Name = "${local.prefix}-jobs"
  }
}


resource "aws_cloudwatch_event_rule" "orchestrator" {
  name                = "${local.prefix}-orchestrator-schedule"
  description         = "Triggers the Orchestrator Lambda on a daily cron"
  schedule_expression = var.orchestrator_schedule
}

resource "aws_cloudwatch_event_target" "orchestrator" {
  rule      = aws_cloudwatch_event_rule.orchestrator.name
  target_id = "orchestrator-lambda"
  arn       = aws_lambda_function.orchestrator.arn
}

resource "aws_lambda_permission" "orchestrator_eventbridge" {
  statement_id  = "AllowEventBridgeInvokeOrchestrator"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.orchestrator.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.orchestrator.arn
}

resource "aws_cloudwatch_event_rule" "notifier" {
  name                = "${local.prefix}-notifier-schedule"
  description         = "Triggers the Notifier Lambda 30 minutes after the Orchestrator"
  schedule_expression = var.notifier_schedule
}

resource "aws_cloudwatch_event_target" "notifier" {
  rule      = aws_cloudwatch_event_rule.notifier.name
  target_id = "notifier-lambda"
  arn       = aws_lambda_function.notifier.arn
}

resource "aws_lambda_permission" "notifier_eventbridge" {
  statement_id  = "AllowEventBridgeInvokeNotifier"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.notifier.arn
}
