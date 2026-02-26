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
