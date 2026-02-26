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
