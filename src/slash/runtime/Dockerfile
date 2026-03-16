FROM python:3.13-slim

# Install runtimes and tools the agent may legitimately use
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm git curl bash ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast Python package management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install slash from the distributed package
COPY . /opt/slash
RUN uv pip install --system --python python3.13 --no-cache --break-system-packages /opt/slash

# Create a non-root user whose UID/GID matches the host operator.
# The GID may already exist in the base image (e.g., macOS staff group is 20,
# which conflicts with Debian's dialout group), so we reuse it if present.
ARG HOST_UID=1000
ARG HOST_GID=1000
RUN if getent group $HOST_GID >/dev/null 2>&1; then \
        existing_group=$(getent group $HOST_GID | cut -d: -f1); \
        useradd -m -u $HOST_UID -g $existing_group -s /bin/bash agent; \
    else \
        groupadd -g $HOST_GID agent \
        && useradd -m -u $HOST_UID -g $HOST_GID -s /bin/bash agent; \
    fi

USER agent
WORKDIR /workspace

ENTRYPOINT ["slash"]
