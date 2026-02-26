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
