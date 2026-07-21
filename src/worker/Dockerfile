FROM --platform=linux/amd64 public.ecr.aws/lambda/python:3.13

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Python dependencies
COPY pyproject.toml .
COPY worker/ ${LAMBDA_TASK_ROOT}/worker/
RUN uv pip install --system --no-cache .

# Lambda task root files are world-readable by default (standard COPY
# umask), so no chown is needed for a non-root user. useradd isn't available
# on this base image (no shadow-utils), so use a bare numeric UID instead of
# creating a named user — Lambda doesn't require a passwd entry for it.
USER 1000

CMD ["worker.handler.handler"]
