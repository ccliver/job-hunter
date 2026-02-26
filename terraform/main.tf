terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }

  # TODO: configure remote state backend
  # backend "s3" {
  #   bucket = "your-terraform-state-bucket"
  #   key    = "job-hunter/terraform.tfstate"
  #   region = "us-east-1"
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "job-hunter"
      ManagedBy = "terraform"
    }
  }
}

locals {
  prefix = "job-hunter"
}
