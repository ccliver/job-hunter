variable "aws_region" {
  description = "AWS region to deploy resources into"
  type        = string
  default     = "us-east-1"
}

variable "ses_from_address" {
  description = "Verified SES sender email address"
  type        = string
}

variable "ses_to_address" {
  description = "Recipient email address for job digests"
  type        = string
}

variable "orchestrator_schedule" {
  description = "EventBridge cron expression for the Orchestrator Lambda"
  type        = string
  default     = "cron(0 9 * * ? *)" # 09:00 UTC daily
}

variable "notifier_schedule" {
  description = "EventBridge cron expression for the Notifier Lambda (30 min after orchestrator)"
  type        = string
  default     = "cron(30 9 * * ? *)" # 09:30 UTC daily
}

variable "lookback_minutes" {
  description = "Minutes the Notifier looks back when querying for new jobs"
  type        = number
  default     = 60
}

variable "bedrock_model_id" {
  description = "Bedrock model ID used by the Worker agent"
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "lambda_timeout_seconds" {
  description = "Lambda function timeout in seconds"
  type        = number
  default     = 300
}

variable "lambda_memory_mb" {
  description = "Lambda function memory in MB"
  type        = number
  default     = 512
}
