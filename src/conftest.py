"""Root pytest configuration.

AWS credentials and region are set at module level so that the module-level
boto3 clients in each handler (created at import time) receive fake credentials
rather than resolving the local AWS profile.  AWS_CONFIG_FILE is pointed at
/dev/null to prevent botocore from discovering any SSO or named profiles.
"""

import os
from dataclasses import dataclass

import pytest

os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_CONFIG_FILE"] = "/dev/null"


@dataclass
class FakeLambdaContext:
    function_name: str = "test-function"
    function_version: str = "$LATEST"
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:test-function"
    memory_limit_in_mb: int = 512
    aws_request_id: str = "test-request-id"
    log_group_name: str = "/aws/lambda/test-function"
    log_stream_name: str = "test-stream"


@pytest.fixture()
def lambda_context() -> FakeLambdaContext:
    return FakeLambdaContext()
