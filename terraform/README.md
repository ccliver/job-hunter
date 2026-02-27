<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.9 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | ~> 5.0 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_aws"></a> [aws](#provider\_aws) | ~> 5.0 |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [aws_cloudwatch_event_rule.notifier](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_rule.orchestrator](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_target.notifier](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.orchestrator](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_log_group.notifier](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_log_group) | resource |
| [aws_cloudwatch_log_group.orchestrator](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_log_group) | resource |
| [aws_cloudwatch_log_group.worker](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_log_group) | resource |
| [aws_dynamodb_table.companies](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/dynamodb_table) | resource |
| [aws_dynamodb_table.jobs](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/dynamodb_table) | resource |
| [aws_iam_role.notifier](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role.orchestrator](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role.worker](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role_policy.notifier](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.orchestrator](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.worker](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_lambda_event_source_mapping.worker_sqs](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_event_source_mapping) | resource |
| [aws_lambda_function.notifier](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_function) | resource |
| [aws_lambda_function.orchestrator](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_function) | resource |
| [aws_lambda_function.worker](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_function) | resource |
| [aws_lambda_permission.notifier_eventbridge](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_permission) | resource |
| [aws_lambda_permission.orchestrator_eventbridge](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_permission) | resource |
| [aws_sqs_queue.worker](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sqs_queue) | resource |
| [aws_sqs_queue.worker_dlq](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sqs_queue) | resource |
| [aws_sqs_queue_policy.worker](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sqs_queue_policy) | resource |
| [aws_iam_policy_document.lambda_assume_role](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_aws_region"></a> [aws\_region](#input\_aws\_region) | AWS region to deploy resources into | `string` | `"us-east-1"` | no |
| <a name="input_bedrock_model_id"></a> [bedrock\_model\_id](#input\_bedrock\_model\_id) | Bedrock model ID used by the Worker agent | `string` | `"us.anthropic.claude-haiku-4-5-20251001-v1:0"` | no |
| <a name="input_lambda_memory_mb"></a> [lambda\_memory\_mb](#input\_lambda\_memory\_mb) | Lambda function memory in MB | `number` | `512` | no |
| <a name="input_lambda_timeout_seconds"></a> [lambda\_timeout\_seconds](#input\_lambda\_timeout\_seconds) | Lambda function timeout in seconds | `number` | `300` | no |
| <a name="input_lookback_minutes"></a> [lookback\_minutes](#input\_lookback\_minutes) | Minutes the Notifier looks back when querying for new jobs | `number` | `60` | no |
| <a name="input_notifier_schedule"></a> [notifier\_schedule](#input\_notifier\_schedule) | EventBridge cron expression for the Notifier Lambda (30 min after orchestrator) | `string` | `"cron(30 9 * * ? *)"` | no |
| <a name="input_orchestrator_schedule"></a> [orchestrator\_schedule](#input\_orchestrator\_schedule) | EventBridge cron expression for the Orchestrator Lambda | `string` | `"cron(0 9 * * ? *)"` | no |
| <a name="input_ses_from_address"></a> [ses\_from\_address](#input\_ses\_from\_address) | Verified SES sender email address | `string` | n/a | yes |
| <a name="input_ses_to_address"></a> [ses\_to\_address](#input\_ses\_to\_address) | Recipient email address for job digests | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_companies_table_name"></a> [companies\_table\_name](#output\_companies\_table\_name) | DynamoDB companies table name |
| <a name="output_jobs_table_name"></a> [jobs\_table\_name](#output\_jobs\_table\_name) | DynamoDB jobs table name |
| <a name="output_notifier_lambda_arn"></a> [notifier\_lambda\_arn](#output\_notifier\_lambda\_arn) | ARN of the Notifier Lambda |
| <a name="output_orchestrator_lambda_arn"></a> [orchestrator\_lambda\_arn](#output\_orchestrator\_lambda\_arn) | ARN of the Orchestrator Lambda |
| <a name="output_worker_dlq_url"></a> [worker\_dlq\_url](#output\_worker\_dlq\_url) | SQS dead-letter queue URL for failed Worker messages |
| <a name="output_worker_lambda_arn"></a> [worker\_lambda\_arn](#output\_worker\_lambda\_arn) | ARN of the Worker Lambda |
| <a name="output_worker_queue_url"></a> [worker\_queue\_url](#output\_worker\_queue\_url) | SQS queue URL for the Worker Lambda |
<!-- END_TF_DOCS -->
