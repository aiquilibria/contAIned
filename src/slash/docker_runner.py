"""
slash Docker runtime — executes slash commands inside an isolated container.

When ``runtime.mode`` in ``.slash/manifest.yaml`` is ``docker``, ``slash repl``
delegates here instead of running the agent in-process.  The container receives
only the workspace bind-mount; the rest of the host filesystem is invisible.

This module is an implementation detail of the ``repl`` command and is not
part of the public API.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
            line = line[len("export "):].strip()
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


class DockerRunner:
    """
    Wraps ``docker run`` to execute ``slash repl`` inside a container
    configured from the ``runtime.docker`` block of the manifest.

    Parameters
    ----------
    docker_config:
        The ``runtime.docker`` dict from ``.slash/manifest.yaml``.
    workspace:
        Absolute path to the workspace root (bound to ``/workspace`` inside
        the container).
    """

    def __init__(self, docker_config: dict, workspace: Path) -> None:
        self.config = docker_config
        self.workspace = workspace.resolve()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _base_args(self) -> list[str]:
        """
        Return the ``docker run`` arguments common to all sub-commands,
        excluding the ``-it`` TTY flag and the sub-command itself.
        """
        docker_bin = _find_docker()
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        name = f"slash-{self.workspace.name}-{os.getpid()}"
        image = self.config.get("image", "slash:latest")
        config_volume = self.config.get("agent_config_volume", "slash-agent-config")
        network = self.config.get("network", "slash-net")
        memory = self.config.get("memory", "2g")
        cpus = str(self.config.get("cpus", 2))
        # Mount the host ~/.claude directory AND ~/.claude.json so Claude Code
        # credentials and configuration persist across container runs.
        # Claude Code splits its state across two separate paths:
        #   ~/.claude/       — sessions, credentials, backups
        #   ~/.claude.json   — main config (model prefs, auth tokens, etc.)
        # Both are created on the host if absent so that a first-time
        # in-container login writes back to the host automatically.
        host_claude_dir  = Path.home() / ".claude"
        host_claude_json = Path.home() / ".claude.json"
        host_claude_dir.mkdir(exist_ok=True)
        if not host_claude_json.exists():
            host_claude_json.touch()  # Docker needs a file, not a directory

        args = [
            docker_bin, "run", "--rm",
            "--name", name,
            "--volume", f"{self.workspace}:/workspace",
            "--volume", f"{host_claude_dir}:/home/agent/.claude",
            "--volume", f"{host_claude_json}:/home/agent/.claude.json",
            "--volume", f"{config_volume}:/home/agent/.config/agent",
            "--env", f"ANTHROPIC_API_KEY={api_key}",
            # Prevent the in-container slash process from re-entering docker
            # mode when it reads the workspace manifest.  Without this flag
            # slash repl inside the container would read the host manifest
            # (mode: docker), call _find_docker(), fail to find docker inside
            # the container, and crash with "Docker executable not found".
            "--env", "SLASH_FORCE_LOCAL=1",
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
        args += [
            "--network", network,
            "--memory", memory,
            "--cpus", cpus,
            image,
        ]
        return args

    # ── public interface ──────────────────────────────────────────────────────

    def run_repl(self, verbosity: str | None = None) -> None:
        """
        Execute ``slash repl`` inside a Docker container with an interactive
        TTY and block until the session ends.  Exits with the container's exit
        code.
        """
        args = self._base_args()
        # Insert -it (interactive TTY) before the image name
        image = self.config.get("image", "slash:latest")
        idx = args.index(image)
        args.insert(idx, "-it")

        args += ["repl"]
        if verbosity:
            args += ["--verbosity", verbosity]

        result = subprocess.run(args)
        sys.exit(result.returncode)
