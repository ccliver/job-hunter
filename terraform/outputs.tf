output "companies_table_name" {
  description = "DynamoDB companies table name"
  value       = aws_dynamodb_table.companies.name
}

output "jobs_table_name" {
  description = "DynamoDB jobs table name"
  value       = aws_dynamodb_table.jobs.name
}

output "worker_queue_url" {
  description = "SQS queue URL for the Worker Lambda"
  value       = aws_sqs_queue.worker.url
}

output "worker_dlq_url" {
  description = "SQS dead-letter queue URL for failed Worker messages"
  value       = aws_sqs_queue.worker_dlq.url
}

output "orchestrator_lambda_arn" {
  description = "ARN of the Orchestrator Lambda"
  value       = aws_lambda_function.orchestrator.arn
}

output "worker_lambda_arn" {
  description = "ARN of the Worker Lambda"
  value       = aws_lambda_function.worker.arn
}

output "notifier_lambda_arn" {
  description = "ARN of the Notifier Lambda"
  value       = aws_lambda_function.notifier.arn
}
