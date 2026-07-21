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

variable "builtin_location" {
  description = "Location substring to additionally keep for the Built In (builtin.com) ATS backend; blank disables it (remote-only)"
  type        = string
  default     = ""
}

variable "builtin_work_type" {
  description = "Work-type keyword to keep for the Built In ATS backend (remote, hybrid, office, any, or any literal substring)"
  type        = string
  default     = "remote"
}

variable "location" {
  description = "Location substring to additionally keep for every ATS backend except builtin; blank disables it (remote-only). Independent of builtin_location"
  type        = string
  default     = ""
}

variable "work_type" {
  description = "Work-type keyword to keep for every ATS backend except builtin (remote, hybrid, office, any, or any literal substring). Independent of builtin_work_type"
  type        = string
  default     = "remote"
}

variable "lambda_timeout_seconds" {
  description = "Lambda function timeout in seconds"
  type        = number
  default     = 300
}

variable "lambda_memory_mb" {
  description = "Lambda function memory in MB (orchestrator and notifier)"
  type        = number
  default     = 512
}

variable "worker_memory_mb" {
  description = "Worker Lambda memory in MB"
  type        = number
  default     = 512
}
