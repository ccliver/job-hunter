FROM --platform=linux/amd64 public.ecr.aws/lambda/python:3.13

# Install Chromium's system dependencies via dnf.
# We do this manually rather than using `playwright install --with-deps` because
# Playwright's OS detection does not recognise the Lambda base image and falls
# back to an Ubuntu/apt-get path, which fails on Amazon Linux 2023.
RUN dnf install -y \
    alsa-lib \
    atk \
    at-spi2-atk \
    at-spi2-core \
    cairo \
    cups-libs \
    dbus-glib \
    expat \
    fontconfig \
    glib2 \
    gtk3 \
    libdrm \
    libX11 \
    libXcomposite \
    libXdamage \
    libXext \
    libXfixes \
    libXrandr \
    libxkbcommon \
    mesa-libgbm \
    nspr \
    nss \
    pango \
    && dnf clean all

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Python dependencies
COPY pyproject.toml .
COPY worker/ ${LAMBDA_TASK_ROOT}/worker/
RUN uv pip install --system --no-cache .

# Bake Chromium into the image so there's no runtime download.
# PLAYWRIGHT_BROWSERS_PATH must be consistent between build and runtime.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium

# Drop to a non-root user. The browser binaries are chowned so Playwright
# can read/execute them; the Lambda task root files are world-readable by
# default (standard COPY umask) so no chown is needed there.
RUN useradd -m -u 1000 appuser \
    && chown -R appuser /ms-playwright
USER appuser

CMD ["worker.handler.handler"]
