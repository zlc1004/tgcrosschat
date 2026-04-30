FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install Docker CLI + Docker Compose plugin (to manage sibling containers via
# the mounted Docker socket) and curl/ca-certificates needed to download them.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-27.0.3.tgz | tar -xz \
    && mv docker/docker /usr/local/bin/ \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -SL https://github.com/docker/compose/releases/download/v2.27.1/docker-compose-linux-x86_64 \
         -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
    && rm -rf docker \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python requirements
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    python -m pip install -r requirements.txt

# Install patchright's Chromium browser and its system dependencies.
# This is required for the Discord token-extraction feature.
RUN python -m patchright install --with-deps chromium

# Use config.py.example as the default config so the image works out of the
# box with MANAGER_BOT_TOKEN / MANAGER_USERNAME environment variables.
# Users who prefer a config file can mount their own config.py over it.
COPY config.py.example config.py

COPY . .

CMD ["python", "manager.py"]
