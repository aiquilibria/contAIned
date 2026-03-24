# syntax=docker/dockerfile:1.4
FROM python:3.13-slim

# Install runtimes and tools the agent may legitimately use
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm git curl bash ca-certificates \
    bubblewrap socat \
    && rm -rf /var/lib/apt/lists/*

# Install sandbox-runtime for seccomp filter support
RUN npm install -g @anthropic-ai/sandbox-runtime

# Install the Claude Code CLI using the official native installer.
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && cp /root/.local/bin/claude /usr/local/bin/claude \
    && chmod 755 /usr/local/bin/claude

# Install uv for fast Python package management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install contAIned into an isolated venv so it never contaminates the
# workspace Python environment, regardless of what the agent installs.
#
# CONTAINED_PACKAGE controls where to install from:
#   - Default "contained[dev]": install from PyPI (release mode).
#   - "/opt/contAIned[dev]": install from local source (development mode).
ARG CONTAINED_PACKAGE=contained[dev]
COPY --chown=root:root . /opt/contAIned/
RUN uv venv --python python3.13 /opt/contained-venv \
    && uv pip install --python /opt/contained-venv/bin/python3 --no-cache "$CONTAINED_PACKAGE"

# Set up in-container runtime paths
RUN mkdir -p /etc/claude-code /etc/contained /run/contained

# Copy the Claude Code plugin and statusline to fixed paths so the entrypoint
# can reference them without knowing the Python package install location.
RUN /opt/contained-venv/bin/python3 -c "\
import os, shutil, contained; \
pkg = os.path.dirname(contained.__file__); \
shutil.copy(os.path.join(pkg, 'runtime', 'statusline.py'), '/etc/contained/statusline.py'); \
shutil.copytree(os.path.join(pkg, 'runtime', 'plugin'), '/etc/contained/plugin') \
"

# Bake managed-settings.json into the image.
# MANAGED_SETTINGS_CONTENT must be passed as a base64-encoded build arg by
# contained init — direct docker build invocations are not supported.
ARG MANAGED_SETTINGS_CONTENT
RUN test -n "$MANAGED_SETTINGS_CONTENT" \
    || { echo "ERROR: MANAGED_SETTINGS_CONTENT build arg is required. Use contained init to build this image."; exit 1; } \
    && echo "$MANAGED_SETTINGS_CONTENT" | base64 -d > /etc/claude-code/managed-settings.json

# Bake the operator manifest into the image so hooks read policy from
# /etc/contained/manifest.yaml rather than the workspace.
ARG MANIFEST_CONTENT=""
RUN if [ -n "$MANIFEST_CONTENT" ]; then \
        echo "$MANIFEST_CONTENT" | base64 -d > /etc/contained/manifest.yaml; \
    fi

# Write the in-container entrypoint script.
# Reads agent.model from the workspace manifest and execs claude directly.
RUN cat <<'ENTRYPOINT_EOF' > /etc/contained/entrypoint.sh
#!/bin/sh
MODEL=$(/opt/contained-venv/bin/python3 -c "
import yaml, sys
try:
    m = yaml.safe_load(open('/workspace/.contAIned/manifest.yaml'))
    print(m.get('agent', {}).get('model', ''))
except Exception:
    pass
" 2>/dev/null)
CMD="claude --plugin-dir /etc/contained/plugin"
[ -n "$MODEL" ] && CMD="$CMD --model $MODEL"
exec $CMD "$@"
ENTRYPOINT_EOF
RUN chmod +x /etc/contained/entrypoint.sh

# Create a non-root user whose UID/GID matches the host operator.
ARG HOST_UID=1000
ARG HOST_GID=1000
RUN if getent group $HOST_GID >/dev/null 2>&1; then \
        existing_group=$(getent group $HOST_GID | cut -d: -f1); \
        useradd -m -u $HOST_UID -g $existing_group -s /bin/bash agent; \
    else \
        groupadd -g $HOST_GID agent \
        && useradd -m -u $HOST_UID -g $HOST_GID -s /bin/bash agent; \
    fi

# Give the agent user a ~/.local/bin so the Claude Code self-check passes.
RUN mkdir -p /home/agent/.local/bin \
    && ln -s /usr/local/bin/claude /home/agent/.local/bin/claude \
    && chown -R agent /home/agent/.local

ENV PATH="/opt/contained-venv/bin:/home/agent/.local/bin:$PATH"

USER agent

# Trust /workspace regardless of host UID/GID ownership
RUN git config --global --add safe.directory /workspace

WORKDIR /workspace

ENTRYPOINT ["/etc/contained/entrypoint.sh"]
