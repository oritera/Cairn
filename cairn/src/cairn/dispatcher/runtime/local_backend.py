from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.runtime.local_process import LocalProcess
from cairn.dispatcher.runtime.platform_compat import merge_env

LOG = logging.getLogger(__name__)


class LocalBackend:
    """Runs workers directly on the dispatcher host instead of in per-project containers.

    Each project gets an isolated working directory under ``workspace_root`` (defaulting
    to the directory the dispatcher was started in). Worker processes inherit the host
    environment so the pre-configured ``claude`` / ``codex`` / ``pi`` CLIs and their
    credentials are used as-is; no API keys are injected. There are no containers to
    build or tear down, so the container-lifecycle methods are inert.
    """

    def __init__(self, config: LocalConfig):
        self._config = config
        root = config.workspace_root
        self._root = Path(root).expanduser() if root else Path.cwd()

    def close(self) -> None:
        return None

    def container_name(self, project_id: str) -> str:
        return str(self._project_dir(project_id))

    def ensure_running(self, project_id: str) -> str:
        project_dir = self._project_dir(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        LOG.debug("local project workdir ready project=%s dir=%s", project_id, project_dir)
        return str(project_dir)

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> LocalProcess:
        merged_env = merge_env(os.environ, env)
        return LocalProcess(
            command,
            cwd=container_name,
            env=merged_env,
            timeout_seconds=timeout_seconds,
            term_grace_seconds=kill_after_seconds,
        )

    def graph_snapshot_root(self, container_name: str) -> str:
        """Where graph snapshots for this project are written: a native host path inside
        the project workdir (so it is valid on Windows and is cleaned up with the project)."""
        return str(Path(container_name) / ".cairn-prompts")

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        target = Path(path)
        if not target.is_absolute():
            raise ValueError(f"local file path must be absolute: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def needs_completed_cleanup(self, project_id: str) -> bool:
        return self._config.completed_action == "remove" and self._project_dir(project_id).exists()

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return False

    def cleanup_completed(self, project_id: str) -> bool:
        if self._config.completed_action == "remove":
            project_dir = self._project_dir(project_id)
            LOG.info("removing completed project workdir project=%s dir=%s", project_id, project_dir)
            shutil.rmtree(project_dir, ignore_errors=True)
        return True

    def cleanup_stopped(self, project_id: str) -> bool:
        return True

    def managed_container_names(self) -> list[str]:
        return []

    def _project_dir(self, project_id: str) -> Path:
        # Replace path separators and characters that are illegal in Windows file names.
        safe = "".join("-" if ch in '/\\:*?"<>|' else ch for ch in project_id)
        return self._root / safe
