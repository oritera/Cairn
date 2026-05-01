from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.tasks.common import run_healthcheck
from cairn.dispatcher.workers.registry import get_driver

LOG = logging.getLogger("runtime.startup")
STARTUP_HEALTHCHECK_PREVIEW_LIMIT = 50


@dataclass(slots=True)
class StartupHealthcheckResult:
    worker_name: str
    ok: bool
    returncode: int
    duration_ms: int
    http_status: str | None
    response_preview: str
    stderr_preview: str
    command: str


def run_startup_healthchecks(
    config: DispatchConfig,
    container_manager: ContainerManager,
    *,
    show_commands: bool = False,
) -> list[StartupHealthcheckResult]:
    container_name = container_manager.create_startup_container()
    workers = list(config.workers)
    parallelism = max(1, min(len(workers), config.runtime.max_workers, 8))
    LOG.info(
        "[*] Startup healthcheck: workers=%s parallelism=%s",
        len(workers),
        parallelism,
    )
    try:
        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            future_map = {
                executor.submit(
                    _run_worker_healthcheck,
                    container_manager,
                    container_name,
                    worker,
                    config.runtime.healthcheck_timeout,
                ): worker.name
                for worker in workers
            }
            results: list[StartupHealthcheckResult] = []
            for future in as_completed(future_map):
                worker_name = future_map[future]
                try:
                    result = future.result()
                except Exception:
                    LOG.exception("startup healthcheck crashed worker=%s", worker_name)
                    result = StartupHealthcheckResult(
                        worker_name=worker_name,
                        ok=False,
                        returncode=1,
                        duration_ms=0,
                        http_status=None,
                        response_preview="",
                        stderr_preview="startup healthcheck crashed",
                        command="-",
                    )
                results.append(result)
    finally:
        LOG.debug("removing startup healthcheck container container=%s", container_name)
        container_manager.remove_container(container_name, force=True)

    results.sort(key=lambda result: result.worker_name)
    _log_report(results, show_commands=show_commands)
    return results


def format_failure_summary(results: list[StartupHealthcheckResult]) -> str:
    failed = [result for result in results if not result.ok]
    if not failed:
        return "startup healthchecks failed for all workers"
    details = []
    for result in failed:
        preview = result.response_preview or result.stderr_preview or "-"
        details.append(
            f"{result.worker_name}(http={result.http_status or '-'}, code={result.returncode}, preview={preview})"
        )
    return f"startup healthchecks failed for all workers: {', '.join(details)}"


def _run_worker_healthcheck(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    timeout_seconds: int,
) -> StartupHealthcheckResult:
    driver = get_driver(worker.type)
    healthcheck = run_healthcheck(
        container_manager,
        container_name,
        worker,
        driver.build_startup_healthcheck(worker),
        timeout_seconds=timeout_seconds,
    )
    result = healthcheck.result
    http_status, response_preview = _parse_stdout(result.stdout)
    return StartupHealthcheckResult(
        worker_name=worker.name,
        ok=result.returncode == 0,
        returncode=result.returncode,
        duration_ms=healthcheck.duration_ms,
        http_status=http_status,
        response_preview=response_preview,
        stderr_preview=_preview(result.stderr),
        command=driver.describe_startup_healthcheck(worker),
    )


def _log_report(results: list[StartupHealthcheckResult], *, show_commands: bool) -> None:
    if not results:
        LOG.warning("[!] Startup healthcheck: no workers configured")
        return
    worker_width = max(len("WORKER"), *(len(result.worker_name) for result in results))
    lines = ["[=] Startup healthcheck results"]
    header = f"{'CHK':<5} {'WORKER':<{worker_width}} {'HTTP':<6} {'CODE':<6} {'TIME_S':>8}  PREVIEW"
    lines.append(header)
    lines.append(f"{'-' * 5} {'-' * worker_width} {'-' * 6} {'-' * 6} {'-' * 8}  {'-' * 50}")
    healthy_count = 0
    for result in results:
        if result.ok:
            healthy_count += 1
        marker = "[+]" if result.ok else "[-]"
        preview = result.response_preview or result.stderr_preview or "-"
        duration_seconds = f"{result.duration_ms / 1000:.2f}"
        lines.append(
            f"{marker:<5} "
            f"{result.worker_name:<{worker_width}} "
            f"{(result.http_status or '-'): <6} "
            f"{result.returncode:<6} "
            f"{duration_seconds:>8}  "
            f"{preview}"
        )
    lines.append(
        f"[=] Summary: total={len(results)} healthy={healthy_count} unhealthy={len(results) - healthy_count}"
    )
    if show_commands:
        lines.append("")
        lines.append("[=] Startup healthcheck commands")
        for result in results:
            lines.append(f"- {result.worker_name}")
            lines.append(f"  {result.command}")
        lines.append("")
    LOG.info("\n%s\n", "\n".join(lines))


def _parse_stdout(stdout: str) -> tuple[str | None, str]:
    lines = stdout.splitlines()
    http_status: str | None = None
    body_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if http_status is None and stripped.startswith("http_status="):
            http_status = stripped.partition("=")[2] or None
            continue
        body_lines.append(line)
    return http_status, _preview("\n".join(body_lines))


def _preview(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= STARTUP_HEALTHCHECK_PREVIEW_LIMIT:
        return compact
    return compact[:STARTUP_HEALTHCHECK_PREVIEW_LIMIT] + "..."
