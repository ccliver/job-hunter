"""Root pytest configuration.

AWS credentials and region are set at module level so that the module-level
boto3 clients in each handler (created at import time) receive fake credentials
rather than resolving the local AWS profile.  AWS_CONFIG_FILE is pointed at
/dev/null to prevent botocore from discovering any SSO or named profiles.
"""

import os

os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_CONFIG_FILE"] = "/dev/null"
