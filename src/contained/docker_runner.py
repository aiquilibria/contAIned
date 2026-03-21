"""
contAIned Docker runtime — executes contAIned commands inside an isolated container.

When ``contAIned`` is invoked, it delegates here to run the agent inside an
isolated Docker container.  The container receives only the workspace
bind-mount; the rest of the host filesystem is invisible.

This module is an implementation detail of the REPL entry point and is not
part of the public API.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _parse_env_file(env_file: Path) -> dict[str, str]:
    """
    Parse a ``.env`` file into a ``{key: value}`` dict.

    Handles the conventions that shell ``.env`` files use but that Docker's
    ``--env-file`` flag does **not** understand:

    * Lines starting with ``#`` (comments) and blank lines are ignored.
    * An ``export `` prefix is stripped (e.g. ``export KEY=value``).
    * Values wrapped in matching single or double quotes have those quotes
      stripped (e.g. ``KEY="value"`` → ``value``).

    Docker's built-in ``--env-file`` passes values verbatim, so a value
    written as ``KEY="sk-ant-..."`` would inject the literal string
    ``"sk-ant-..."`` (quotes included), breaking any downstream consumer that
    expects a bare key.  Parsing the file here and passing each variable via
    ``--env KEY=VALUE`` avoids that problem entirely.
    """
    result: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        # Skip blank lines and comments
        if not line or line.startswith("#"):
            continue
        # Strip optional 'export ' prefix
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        # Must contain '=' to be a valid assignment
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        # Strip matching surrounding quotes from the value
        if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        result[key] = value
    return result


# Common Docker executable locations when not in PATH
_DOCKER_SEARCH_PATHS = [
    "/usr/local/bin/docker",
    "/usr/bin/docker",
    "/opt/homebrew/bin/docker",
]


def _find_docker() -> str:
    """
    Locate the Docker executable.

    First checks PATH via ``shutil.which``, then falls back to common
    installation locations.  Raises ``FileNotFoundError`` with a helpful
    message if Docker cannot be found.
    """
    # Try PATH first
    docker = shutil.which("docker")
    if docker:
        return docker

    # Check common locations
    for path in _DOCKER_SEARCH_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    raise FileNotFoundError(
        "Docker executable not found. Please ensure Docker is installed and either:\n"
        "  1. Add it to your PATH, or\n"
        "  2. Install Docker Desktop from https://www.docker.com/products/docker-desktop/\n"
        f"Searched PATH and: {', '.join(_DOCKER_SEARCH_PATHS)}"
    )


# Common cosign executable locations when not in PATH
_COSIGN_SEARCH_PATHS = [
    "/usr/local/bin/cosign",
    "/usr/bin/cosign",
]


def _find_cosign() -> str:
    """
    Locate the cosign executable (v2.x required).

    Only called when ``sigstore.enabled`` is true.  Raises ``FileNotFoundError``
    with install instructions if cosign cannot be found.
    """
    cosign = shutil.which("cosign")
    if cosign:
        return cosign

    for path in _COSIGN_SEARCH_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    raise FileNotFoundError(
        "cosign executable not found. cosign v2.x is required when Sigstore is enabled.\n"
        "Install cosign: https://docs.sigstore.dev/cosign/system_config/installation/\n"
        f"Searched PATH and: {', '.join(_COSIGN_SEARCH_PATHS)}"
    )


class DockerRunner:
    """
    Wraps ``docker run`` to execute ``contAIned`` inside a container
    configured from the ``runtime.docker`` block of the manifest.

    Parameters
    ----------
    docker_config:
        The ``runtime.docker`` dict from ``.contAIned/manifest.yaml``.
    workspace:
        Absolute path to the workspace root (bound to ``/workspace`` inside
        the container).
    policy:
        The ``policy`` dict from ``.contAIned/manifest.yaml``.  Used to
        configure the egress filtering proxy when ``policy.egress.enabled``
        is true.
    """

    def __init__(
        self,
        docker_config: dict,
        workspace: Path,
        policy: dict | None = None,
    ) -> None:
        self.config = docker_config
        self.workspace = workspace.resolve()
        self.policy = policy or {}

    # ── internal helpers ──────────────────────────────────────────────────────

    def _base_args(self) -> list[str]:
        """
        Return the ``docker run`` arguments common to all sub-commands,
        excluding the ``-it`` TTY flag and the sub-command itself.
        """
        docker_bin = _find_docker()
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        name = f"contAIned-{self.workspace.name}-{os.getpid()}"
        image = self.config.get("image", "contained:latest")
        config_volume = self.config.get("agent_config_volume", "contAIned-agent-config")
        network = self.config.get("network", "contAIned-net")
        memory = self.config.get("memory", "2g")
        cpus = str(self.config.get("cpus", 2))
        # Mount the host ~/.claude directory AND ~/.claude.json so Claude Code
        # credentials and configuration persist across container runs.
        # Claude Code splits its state across two separate paths:
        #   ~/.claude/       — sessions, credentials, backups
        #   ~/.claude.json   — main config (model prefs, auth tokens, etc.)
        # Both are created on the host if absent so that a first-time
        # in-container login writes back to the host automatically.
        host_claude_dir = Path.home() / ".claude"
        host_claude_json = Path.home() / ".claude.json"
        host_claude_dir.mkdir(exist_ok=True)
        if not host_claude_json.exists():
            host_claude_json.touch()  # Docker needs a file, not a directory

        args = [
            docker_bin,
            "run",
            "--rm",
            "--name",
            name,
            "--volume",
            f"{self.workspace}:/workspace",
            "--volume",
            f"{host_claude_dir}:/home/agent/.claude",
            "--volume",
            f"{host_claude_json}:/home/agent/.claude.json",
            "--volume",
            f"{config_volume}:/home/agent/.config/agent",
            "--env",
            f"ANTHROPIC_API_KEY={api_key}",
            # Prevent the in-container contAIned process from re-entering docker
            # mode when it reads the workspace manifest.  Without this flag
            # contAIned repl inside the container would read the host manifest
            # (mode: docker), call _find_docker(), fail to find docker inside
            # the container, and crash with "Docker executable not found".
            "--env",
            "contAIned_FORCE_LOCAL=1",
        ]
        # Inject workspace .env file if present so project secrets are
        # available inside the container without being baked into the image.
        #
        # We parse the file ourselves rather than using Docker's --env-file
        # flag because Docker passes values verbatim: a line like
        #   ANTHROPIC_API_KEY="sk-ant-..."
        # would inject the key with literal surrounding quotes, making it
        # invalid.  Our parser strips quotes and 'export' prefixes so the
        # values reach the container in the form the agent expects.
        env_file = self.workspace / ".env"
        if env_file.is_file():
            for key, value in _parse_env_file(env_file).items():
                args += ["--env", f"{key}={value}"]
        # If egress filtering is enabled, point the agent at the proxy sidecar.
        egress = self.policy.get("egress", {})
        if egress.get("enabled", False):
            proxy_url = f"http://{self._proxy_name()}:3128"
            args += [
                "--env",
                f"HTTP_PROXY={proxy_url}",
                "--env",
                f"HTTPS_PROXY={proxy_url}",
                "--env",
                "NO_PROXY=localhost,127.0.0.1",
            ]

        args += [
            "--network",
            network,
            "--memory",
            memory,
            "--cpus",
            cpus,
            image,
        ]
        return args

    # ── proxy sidecar ─────────────────────────────────────────────────────────

    def _proxy_name(self) -> str:
        """Deterministic name for the proxy sidecar container."""
        return f"contAIned-proxy-{self.workspace.name}"

    def _start_proxy(self) -> str | None:
        """
        Start the egress filtering proxy sidecar if ``policy.egress.enabled``
        is true.

        The proxy is a second container running ``python3 -m contained.proxy``
        inside the same ``contained:latest`` image, with the allowed domain
        list passed as CLI arguments.  It listens on port 3128 of the Docker
        network, and the agent container reaches it by container name.

        Returns the container name on success, or ``None`` if egress filtering
        is disabled or the proxy fails to start (a warning is printed but the
        session continues).
        """
        egress = self.policy.get("egress", {})
        if not egress.get("enabled", False):
            return None

        docker_bin = _find_docker()
        domains: list[str] = egress.get(
            "allowed_domains",
            ["api.anthropic.com", "code.claude.com", "docs.anthropic.com"],
        )
        image = self.config.get("image", "contained:latest")
        network = self.config.get("network", "contAIned-net")
        name = self._proxy_name()

        # Remove any stale proxy container left over from a crashed session.
        subprocess.run([docker_bin, "rm", "-f", name], capture_output=True)

        cmd = [
            docker_bin,
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--network",
            network,
            "--entrypoint",
            "python3",
            image,
            "-m",
            "contained.proxy",
        ] + domains

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            import sys as _sys

            print(
                f"[contAIned] Warning: egress proxy failed to start — "
                f"{result.stderr.strip() or result.stdout.strip()}",
                file=_sys.stderr,
            )
            return None

        # Poll until the container is confirmed running (up to 5 s).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            check = subprocess.run(
                [docker_bin, "inspect", "--format", "{{.State.Running}}", name],
                capture_output=True,
                text=True,
            )
            if check.stdout.strip() == "true":
                # Give the Python process inside a moment to bind the port.
                time.sleep(0.3)
                return name
            time.sleep(0.1)

        import sys as _sys

        print(
            "[contAIned] Warning: egress proxy did not become ready in time.",
            file=_sys.stderr,
        )
        return None

    def _stop_proxy(self, name: str) -> None:
        """Stop and remove the proxy sidecar container."""
        docker_bin = _find_docker()
        subprocess.run([docker_bin, "rm", "-f", name], capture_output=True)

    # ── public interface ──────────────────────────────────────────────────────

    def run_repl(self) -> None:
        """
        Execute ``contAIned`` inside a Docker container with an interactive TTY
        and block until the session ends.  Exits with the container's exit code.

        If egress filtering is enabled, a proxy sidecar container is started
        before the agent and stopped (regardless of how the session ends) after.
        """
        proxy_name = self._start_proxy()
        exit_code = 1
        try:
            args = self._base_args()
            # Insert -it (interactive TTY) before the image name
            image = self.config.get("image", "contained:latest")
            idx = args.index(image)
            args.insert(idx, "-it")
            result = subprocess.run(args)
            exit_code = result.returncode
        finally:
            if proxy_name:
                self._stop_proxy(proxy_name)
        sys.exit(exit_code)
