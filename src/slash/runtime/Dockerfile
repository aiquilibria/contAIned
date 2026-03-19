FROM python:3.13-slim

# Install runtimes and tools the agent may legitimately use
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm git curl bash ca-certificates \
    bubblewrap socat \
    && rm -rf /var/lib/apt/lists/*

# Install sandbox-runtime for seccomp filter support
RUN npm install -g @anthropic-ai/sandbox-runtime

# Install the Claude Code CLI using the official native installer.
# The script installs to /root/.local/bin (mode 700 dir — inaccessible to
# non-root users via symlink).  Copy the binary to /usr/local/bin instead
# so the non-root agent user can execute it without PATH gymnastics.
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && cp /root/.local/bin/claude /usr/local/bin/claude \
    && chmod 755 /usr/local/bin/claude

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

# Give the agent user a ~/.local/bin that contains the claude binary so the
# Claude Code CLI's native-install self-check passes without printing warnings.
RUN mkdir -p /home/agent/.local/bin \
    && ln -s /usr/local/bin/claude /home/agent/.local/bin/claude \
    && chown -R agent /home/agent/.local

ENV PATH="/home/agent/.local/bin:$PATH"

USER agent

# Trust /workspace regardless of host UID/GID ownership
RUN git config --global --add safe.directory /workspace

WORKDIR /workspace

ENTRYPOINT ["slash"]
