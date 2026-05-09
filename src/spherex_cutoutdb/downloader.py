"""Streaming cutout downloads with retries, checksums, and DB updates."""

from __future__ import annotations

from collections import OrderedDict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import heapq
import hashlib
from queue import Empty, Queue
import random
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn

from .config import Config
from .database import record_failure, record_validation, upsert_cutout_record, utcnow
from .models import DownloadResult, DownloadSummary, ValidationResult
from .validator import validate_cutout

DOWNLOAD_ACTIONS = {
    "download",
    "validate_existing",
    "redownload_invalid",
    "redownload_processing_update",
}
OVERWRITE_ACTIONS = DOWNLOAD_ACTIONS | {"skip_valid", "validate_existing"}
VALIDATION_OK = {"passed", "passed_with_warnings"}
PROGRESS_UPDATE_INTERVAL_SECONDS = 10.0
VERBOSE_FILE_DETAIL_LIMIT = 8


@dataclass(slots=True)
class CompletedDownload:
    plan_row: dict[str, Any]
    started_at: str | None
    result: DownloadResult
    validation: ValidationResult | None = None
    http_status: int | None = None
    retry_delay_seconds: float | None = None
    elapsed_seconds: float = 0.0
    bytes_per_second: float | None = None
    exception_class: str | None = None


@dataclass(slots=True)
class FileDownloadWork:
    sequence: int
    plan_row: dict[str, Any]
    attempt: int = 1
    started_at: str | None = None
    deadline_monotonic: float | None = None


@dataclass(slots=True)
class FileAttemptOutcome:
    work: FileDownloadWork
    completed: CompletedDownload
    retryable: bool = False
    retry_after_seconds: float | None = None


class _SessionPool:
    """Thread-local request sessions with a shared connection-pool policy."""

    def __init__(self, config: Config, max_workers: int):
        self._config = config
        self._max_workers = max(1, int(max_workers))
        self._local = threading.local()
        self._lock = threading.Lock()
        self._sessions: list[requests.Session] = []

    def get(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = make_session(self._config, self._max_workers)
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()


@dataclass(slots=True)
class TargetDownloadJob:
    source_id: str
    source_name: str | None
    ra_deg: float | None
    dec_deg: float | None
    plan_rows: list[dict[str, Any]]
    config_data: dict[str, Any]
    overwrite: bool = False
    skip_existing: bool = True
    progress_queue: Any | None = None
    progress_interval_seconds: float = PROGRESS_UPDATE_INTERVAL_SECONDS


@dataclass(slots=True)
class TargetDownloadResult:
    source_id: str
    source_name: str | None
    ra_deg: float | None
    dec_deg: float | None
    planned_files: int
    skipped_files: int = 0
    downloaded_files: int = 0
    failed_files: int = 0
    bytes_downloaded: int = 0
    elapsed_seconds: float = 0.0
    plan_rows: list[dict[str, Any]] = field(default_factory=list)
    completed: list[CompletedDownload] = field(default_factory=list)
    worker_error: str | None = None


def resolve_project_path(config: Config, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else config.project.root / path


def make_session(config: Config, max_workers: int | None = None) -> requests.Session:
    workers = max(1, int(max_workers or config.download.max_workers or config.download.concurrency))
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=workers,
        pool_maxsize=workers,
        pool_block=True,
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": config.download.user_agent})
    return session


def download_one(
    plan_row: dict[str, Any],
    config: Config,
    session: requests.Session | None = None,
    *,
    progress_callback: Any | None = None,
    progress_interval_seconds: float = PROGRESS_UPDATE_INTERVAL_SECONDS,
) -> DownloadResult:
    cutout_key = plan_row["cutout_key"]
    final_path = resolve_project_path(config, plan_row["local_path"])
    partial_dir = config.project.data_root / "partial"
    partial_dir.mkdir(parents=True, exist_ok=True)
    partial_path = partial_dir / f"{cutout_key}.fits{config.download.partial_suffix}"
    final_path.parent.mkdir(parents=True, exist_ok=True)

    url = plan_row.get("cutout_url")
    if not url:
        return DownloadResult(
            plan_id=plan_row.get("plan_id"),
            cutout_key=cutout_key,
            local_path=final_path,
            success=False,
            status="failed",
            attempts=0,
            reason="missing cutout URL",
        )

    client = session or make_session(config, 1)
    owns_client = session is None
    try:
        headers = {"User-Agent": config.download.user_agent}
        retry = config.download.retry
        last_reason = None
        retry_statuses = set(retry.retry_http_status)

        for attempt in range(1, retry.attempts + 1):
            sha = hashlib.sha256()
            bytes_written = 0
            last_progress_emit = time.monotonic()
            try:
                with client.get(
                    url,
                    stream=True,
                    headers=headers,
                    timeout=(config.download.connect_timeout_sec, config.download.read_timeout_sec),
                ) as response:
                    if response.status_code in retry_statuses:
                        last_reason = f"HTTP {response.status_code}"
                        if attempt < retry.attempts:
                            _sleep_for_retry(response, retry, attempt)
                            continue
                        return DownloadResult(
                            plan_id=plan_row.get("plan_id"),
                            cutout_key=cutout_key,
                            local_path=final_path,
                            success=False,
                            status="failed",
                            file_size_bytes=0,
                            attempts=attempt,
                            reason=last_reason,
                        )
                    response.raise_for_status()
                    with partial_path.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=config.download.chunk_size_bytes):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            sha.update(chunk)
                            bytes_written += len(chunk)
                            if progress_callback is not None:
                                now = time.monotonic()
                                if now - last_progress_emit >= progress_interval_seconds:
                                    progress_callback(bytes_written, attempt)
                                    last_progress_emit = now
                if bytes_written <= 0:
                    raise IOError("empty response body")
                partial_path.replace(final_path)
                return DownloadResult(
                    plan_id=plan_row.get("plan_id"),
                    cutout_key=cutout_key,
                    local_path=final_path,
                    success=True,
                    status="downloaded",
                    file_size_bytes=bytes_written,
                    sha256=sha.hexdigest(),
                    attempts=attempt,
                )
            except Exception as exc:  # noqa: BLE001 - retry boundary
                last_reason = str(exc)
                if partial_path.exists():
                    partial_path.unlink()
                if attempt >= retry.attempts:
                    return DownloadResult(
                        plan_id=plan_row.get("plan_id"),
                        cutout_key=cutout_key,
                        local_path=final_path,
                        success=False,
                        status="failed",
                        file_size_bytes=bytes_written,
                        attempts=attempt,
                        reason=last_reason,
                    )
                delay = retry.backoff_seconds[min(attempt - 1, len(retry.backoff_seconds) - 1)]
                time.sleep(delay)

        return DownloadResult(
            plan_id=plan_row.get("plan_id"),
            cutout_key=cutout_key,
            local_path=final_path,
            success=False,
            status="failed",
            attempts=retry.attempts,
            reason=last_reason or "download failed",
        )
    finally:
        if owns_client:
            client.close()


def _sleep_for_retry(response: requests.Response, retry, attempt: int) -> None:
    delay = None
    if retry.honor_retry_after:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
    if delay is None:
        delay = retry.backoff_seconds[min(attempt - 1, len(retry.backoff_seconds) - 1)]
    time.sleep(delay)


def download_plan(
    conn,
    run_id: str | None,
    config: Config,
    max_downloads: int | None = None,
    session: requests.Session | None = None,
    *,
    console: Console | None = None,
    progress: bool = True,
    verbose: bool = False,
    dry_run: bool = False,
) -> DownloadSummary:
    rows = _planned_download_rows(
        conn,
        run_id,
        max_downloads,
        overwrite=bool(config.download.overwrite_existing),
    )
    if not rows and run_id is not None:
        rows = _planned_download_rows(
            conn,
            None,
            max_downloads,
            overwrite=bool(config.download.overwrite_existing),
        )
    summary = DownloadSummary()
    plan_rows = [dict(row) for row in rows]
    if not plan_rows:
        if verbose and console is not None:
            console.print("No planned downloads found.")
        return summary

    groups = group_plan_rows_by_target(plan_rows)
    summary.total_targets = len(groups)
    summary.attempted = len(plan_rows)
    if dry_run:
        summary.skipped = len(plan_rows)
        summary.successful_targets = len(groups)
        return summary

    workers = 1 if session is not None else max(1, int(config.download.max_workers or config.download.concurrency))
    if verbose and console is not None:
        mode = "thread-file" if session is None and workers > 1 else "serial-file"
        console.print(
            "Download plan: "
            f"targets={len(groups)} files={len(plan_rows)} max_workers={workers} mode={mode} "
            f"skip_existing={config.download.skip_existing} overwrite={config.download.overwrite_existing} "
            f"retry_attempts={config.download.retry.attempts} read_timeout={config.download.read_timeout_sec}s "
            f"total_timeout={config.download.total_timeout_sec}s "
            f"per_host_rate={config.download.per_host_rate_limit_per_second}/s "
            f"min_rate={_format_rate(config.download.min_download_rate_bytes_per_second)}"
        )

    progress_queue = Queue() if progress and console is not None else None
    target_results = _target_results_from_groups(groups)
    completed_iter = _file_download_iter(
        plan_rows,
        config,
        session=session,
        max_workers=workers,
        progress_queue=progress_queue,
    )
    if progress and console is not None:
        _run_file_downloads_with_progress(
            completed_iter,
            conn,
            run_id,
            config,
            summary,
            target_results,
            console,
            verbose,
            len(groups),
            len(plan_rows),
            workers,
        )
    else:
        for item in completed_iter:
            if isinstance(item, dict):
                if verbose and console is not None and item.get("event") == "file_retry":
                    console.print(_format_retry_event_line(item))
                continue
            _record_file_download_result(conn, run_id, config, item, target_results, summary, console, verbose)
    _finalize_target_summary(summary, target_results, console, verbose)
    return summary


def iter_download_plan_results(
    conn,
    run_id: str | None,
    config: Config,
    *,
    max_downloads: int | None = None,
    plan_rows: list[dict[str, Any]] | None = None,
    session: requests.Session | None = None,
    console: Console | None = None,
    progress: bool = True,
    verbose: bool = False,
    dry_run: bool = False,
):
    """Yield recorded per-file download results from the existing scheduler.

    This is the public handoff used by downstream photometry code. It preserves
    the downloader as the authority for URL construction, retry/requeue,
    validation, and cutout DB recording. SQLite writes stay in the caller's
    thread before a terminal ``CompletedDownload`` is yielded.
    """

    selected_rows = [dict(row) for row in (plan_rows or [])]
    if not selected_rows:
        rows = _planned_download_rows(
            conn,
            run_id,
            max_downloads,
            overwrite=bool(config.download.overwrite_existing),
        )
        if not rows and run_id is not None:
            rows = _planned_download_rows(
                conn,
                None,
                max_downloads,
                overwrite=bool(config.download.overwrite_existing),
            )
        selected_rows = [dict(row) for row in rows]
    elif max_downloads is not None:
        selected_rows = selected_rows[:max_downloads]

    if not selected_rows:
        if verbose and console is not None:
            console.print("No planned downloads found.")
        return

    groups = group_plan_rows_by_target(selected_rows)
    summary = DownloadSummary(attempted=len(selected_rows), total_targets=len(groups))
    if dry_run:
        summary.skipped = len(selected_rows)
        summary.successful_targets = len(groups)
        for row in selected_rows:
            yield {
                "event": "dry_run",
                "source_id": row.get("source_id"),
                "cutout_key": row.get("cutout_key"),
                "status": "skipped",
            }
        return

    workers = 1 if session is not None else max(1, int(config.download.max_workers or config.download.concurrency))
    progress_queue = Queue() if progress and console is not None else None
    target_results = _target_results_from_groups(groups)
    completed_iter = _file_download_iter(
        selected_rows,
        config,
        session=session,
        max_workers=workers,
        progress_queue=progress_queue,
    )
    for item in completed_iter:
        if isinstance(item, dict):
            if verbose and console is not None and item.get("event") == "file_retry":
                console.print(_format_retry_event_line(item))
            yield item
            continue
        _record_file_download_result(conn, run_id, config, item, target_results, summary, console, verbose)
        yield item
    _finalize_target_summary(summary, target_results, console, verbose)


def count_planned_downloads(
    conn,
    run_id: str | None,
    max_downloads: int | None = None,
    *,
    overwrite: bool = False,
) -> int:
    rows = _planned_download_rows(conn, run_id, max_downloads, overwrite=overwrite)
    if not rows and run_id is not None:
        rows = _planned_download_rows(conn, None, max_downloads, overwrite=overwrite)
    return len(rows)


def group_plan_rows_by_target(plan_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for row in sorted(plan_rows, key=_plan_row_sort_key):
        source_id = str(row["source_id"])
        group = grouped.get(source_id)
        if group is None:
            group = {
                "source_id": source_id,
                "source_name": row.get("source_name"),
                "ra_deg": _first_float(row.get("source_ra_deg"), row.get("cutout_ra_deg")),
                "dec_deg": _first_float(row.get("source_dec_deg"), row.get("cutout_dec_deg")),
                "rows": [],
            }
            grouped[source_id] = group
        group["rows"].append(row)
    return list(grouped.values())


def _target_results_from_groups(groups: list[dict[str, Any]]) -> dict[str, TargetDownloadResult]:
    return {
        str(group["source_id"]): TargetDownloadResult(
            source_id=str(group["source_id"]),
            source_name=group.get("source_name"),
            ra_deg=group.get("ra_deg"),
            dec_deg=group.get("dec_deg"),
            planned_files=len(group["rows"]),
            plan_rows=[dict(row) for row in group["rows"]],
        )
        for group in groups
    }


def _file_download_iter(
    plan_rows: list[dict[str, Any]],
    config: Config,
    *,
    session: requests.Session | None,
    max_workers: int,
    progress_queue=None,
):
    retry = config.download.retry
    workers = 1 if session is not None else max(1, int(max_workers))
    ready = deque(
        FileDownloadWork(
            sequence=index,
            plan_row=dict(row),
            deadline_monotonic=time.monotonic() + float(config.download.total_timeout_sec),
        )
        for index, row in enumerate(sorted(plan_rows, key=_plan_row_sort_key))
    )
    delayed: list[tuple[float, int, FileDownloadWork]] = []
    pending: dict[Any, FileDownloadWork] = {}
    active_by_host: dict[str, int] = {}
    host_next_start: dict[str, float] = {}
    sequence = len(ready)
    session_pool = None if session is not None else _SessionPool(config, workers)
    rate_limit = float(config.download.per_host_rate_limit_per_second or 0.0)
    request_interval = 1.0 / rate_limit if rate_limit > 0 else 0.0
    host_active_limit = _host_active_limit(config, workers)

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            while ready or delayed or pending:
                now = time.monotonic()
                while delayed and delayed[0][0] <= now:
                    _, _, work = heapq.heappop(delayed)
                    ready.append(work)

                submitted = False
                while ready and len(pending) < workers:
                    work = _take_ready_work(
                        ready,
                        now,
                        host_next_start,
                        active_by_host,
                        host_active_limit,
                    )
                    if work is None:
                        break
                    host = _request_host(work.plan_row)
                    active_by_host[host] = active_by_host.get(host, 0) + 1
                    if host:
                        host_next_start[host] = now + request_interval
                    future = executor.submit(
                        _run_file_download_attempt,
                        work,
                        config,
                        session,
                        session_pool,
                        progress_queue,
                    )
                    pending[future] = work
                    _emit_progress_event(
                        progress_queue,
                        _scheduler_state_event(ready, delayed, pending, active_by_host, "submitted"),
                    )
                    submitted = True
                    now = time.monotonic()
                if submitted:
                    for event in _drain_progress_queue(progress_queue):
                        yield event
                    continue

                for event in _drain_progress_queue(progress_queue):
                    yield event
                _emit_progress_event(
                    progress_queue,
                    _scheduler_state_event(ready, delayed, pending, active_by_host, "waiting"),
                )

                timeout = _scheduler_wait_timeout(ready, delayed, pending, host_next_start, active_by_host, host_active_limit)
                done = set()
                if pending:
                    done, _ = wait(set(pending), timeout=timeout, return_when=FIRST_COMPLETED)
                elif timeout > 0:
                    time.sleep(timeout)

                for event in _drain_progress_queue(progress_queue):
                    yield event

                for future in done:
                    work = pending.pop(future)
                    host = _request_host(work.plan_row)
                    if active_by_host.get(host, 0) <= 1:
                        active_by_host.pop(host, None)
                    else:
                        active_by_host[host] -= 1
                    try:
                        outcome = future.result()
                    except Exception as exc:  # noqa: BLE001 - worker boundary
                        outcome = _worker_exception_outcome(work, exc)

                    delay = _retry_delay_for_outcome(outcome, config)
                    if _should_requeue(outcome, config, delay):
                        ready_at = time.monotonic() + float(delay or 0.0)
                        sequence += 1
                        retry_work = FileDownloadWork(
                            sequence=sequence,
                            plan_row=outcome.work.plan_row,
                            attempt=outcome.work.attempt + 1,
                            started_at=outcome.completed.started_at,
                            deadline_monotonic=outcome.work.deadline_monotonic,
                        )
                        outcome.completed.retry_delay_seconds = float(delay or 0.0)
                        heapq.heappush(delayed, (ready_at, sequence, retry_work))
                        yield _file_retry_event(outcome, delay or 0.0, len(ready), len(delayed), len(pending))
                    else:
                        yield _finalized_outcome(outcome, config)
                    yield _scheduler_state_event(ready, delayed, pending, active_by_host, "updated")
    finally:
        if session_pool is not None:
            session_pool.close()


def _run_file_downloads_with_progress(
    completed_iter,
    conn,
    run_id: str | None,
    config: Config,
    summary: DownloadSummary,
    target_results: dict[str, TargetDownloadResult],
    console: Console,
    verbose: bool,
    target_total: int,
    files_total: int,
    workers: int,
) -> None:
    progress_state = _new_progress_state(target_total, files_total, workers)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn(
            "targets={task.fields[targets]} active={task.fields[active]} "
            "files={task.fields[files]} dl={task.fields[downloaded]} "
            "skip={task.fields[skipped]} fail={task.fields[failed]} "
            "retry={task.fields[retry]} queue={task.fields[queue]} "
            "bytes={task.fields[bytes]} last={task.fields[last]}"
        ),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as bar:
        task_id = bar.add_task(
            "Downloading files",
            total=files_total,
            targets=f"0/{target_total}",
            active=f"0/{workers}",
            files=f"0/{files_total}",
            downloaded=0,
            skipped=0,
            failed=0,
            retry=0,
            queue=files_total,
            bytes="0 B",
            last="-",
        )
        for item in completed_iter:
            if isinstance(item, dict):
                _apply_progress_event(progress_state, item)
                _update_download_progress_bar(bar, task_id, progress_state)
                _print_live_progress_event(console, item, verbose)
                continue
            _record_file_download_result(conn, run_id, config, item, target_results, summary, console, verbose)
            _apply_progress_event(progress_state, _file_done_event_from_completed(item))
            _sync_progress_state_from_summary(progress_state, summary)
            progress_state["completed_targets"] = _completed_target_count(target_results)
            _update_download_progress_bar(bar, task_id, progress_state)
            bar.update(task_id, advance=1)


def download_target_worker(job: TargetDownloadJob) -> TargetDownloadResult:
    return _run_target_download_job(job)


def _target_jobs_from_groups(
    groups: list[dict[str, Any]],
    config: Config,
    progress_queue: Any | None = None,
) -> list[TargetDownloadJob]:
    config_data = config.model_dump(mode="json")
    return [
        TargetDownloadJob(
            source_id=group["source_id"],
            source_name=group.get("source_name"),
            ra_deg=group.get("ra_deg"),
            dec_deg=group.get("dec_deg"),
            plan_rows=[dict(row) for row in group["rows"]],
            config_data=config_data,
            overwrite=bool(config.download.overwrite_existing),
            skip_existing=bool(config.download.skip_existing),
            progress_queue=progress_queue,
            progress_interval_seconds=PROGRESS_UPDATE_INTERVAL_SECONDS,
        )
        for group in groups
    ]


def _target_download_iter(
    jobs: list[TargetDownloadJob],
    *,
    session: requests.Session | None,
    max_workers: int,
    console: Console | None = None,
    verbose: bool = False,
    progress_queue=None,
):
    if max_workers <= 1 or len(jobs) <= 1 or session is not None:
        for job in jobs:
            result = _run_target_download_job(job, session=session)
            for event in _drain_progress_queue(progress_queue):
                yield event
            yield result
        return

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_target_worker, job): job for job in jobs}
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            for event in _drain_progress_queue(progress_queue):
                yield event
            for future in done:
                job = futures[future]
                try:
                    yield future.result()
                except Exception as exc:  # noqa: BLE001 - worker boundary
                    yield TargetDownloadResult(
                        source_id=job.source_id,
                        source_name=job.source_name,
                        ra_deg=job.ra_deg,
                        dec_deg=job.dec_deg,
                        planned_files=len(job.plan_rows),
                        plan_rows=job.plan_rows,
                        failed_files=len(job.plan_rows),
                        worker_error=f"{exc.__class__.__name__}: {exc}",
                    )
        for event in _drain_progress_queue(progress_queue):
            yield event


def _run_target_download_job(
    job: TargetDownloadJob,
    session: requests.Session | None = None,
) -> TargetDownloadResult:
    start = time.monotonic()
    config = Config.model_validate(job.config_data)
    result = TargetDownloadResult(
        source_id=job.source_id,
        source_name=job.source_name,
        ra_deg=job.ra_deg,
        dec_deg=job.dec_deg,
        planned_files=len(job.plan_rows),
        plan_rows=job.plan_rows,
    )
    client = session or make_session(config, max(1, len(job.plan_rows)))
    owns_client = session is None
    try:
        for plan_row in job.plan_rows:
            completed = _download_and_validate(
                plan_row,
                config,
                session=client,
                overwrite=job.overwrite,
                skip_existing=job.skip_existing,
                progress_callback=_file_progress_callback(job, plan_row),
                progress_interval_seconds=job.progress_interval_seconds,
            )
            result.completed.append(completed)
            if completed.result.status == "skipped_existing":
                result.skipped_files += 1
            elif completed.result.success:
                result.downloaded_files += 1
                result.bytes_downloaded += completed.result.file_size_bytes
            if not completed.result.success or _validation_failed(completed.validation):
                result.failed_files += 1
            _emit_progress_event(job.progress_queue, _file_done_event(job, completed))
    finally:
        if owns_client:
            client.close()
        result.elapsed_seconds = time.monotonic() - start
    return result


def _download_and_validate(
    plan_row: dict[str, Any],
    config: Config,
    session: requests.Session | None = None,
    *,
    overwrite: bool = False,
    skip_existing: bool = True,
    progress_callback: Any | None = None,
    progress_interval_seconds: float = PROGRESS_UPDATE_INTERVAL_SECONDS,
) -> CompletedDownload:
    final_path = resolve_project_path(config, plan_row["local_path"])
    if skip_existing and not overwrite and final_path.exists():
        validation = validate_cutout(final_path, config)
        if validation.status in VALIDATION_OK:
            return CompletedDownload(
                plan_row=plan_row,
                started_at=None,
                result=DownloadResult(
                    plan_id=plan_row.get("plan_id"),
                    cutout_key=plan_row["cutout_key"],
                    local_path=final_path,
                    success=True,
                    status="skipped_existing",
                    file_size_bytes=validation.file_size_bytes,
                    sha256=validation.sha256,
                    attempts=0,
                    reason="existing file validates",
                ),
                validation=validation,
            )
    started_at = utcnow()
    result = download_one(
        plan_row,
        config,
        session=session,
        progress_callback=progress_callback,
        progress_interval_seconds=progress_interval_seconds,
    )
    validation = validate_cutout(result.local_path, config) if result.success else None
    return CompletedDownload(
        plan_row=plan_row,
        started_at=started_at,
        result=result,
        validation=validation,
    )


def _run_file_download_attempt(
    work: FileDownloadWork,
    config: Config,
    session: requests.Session | None,
    session_pool: _SessionPool | None,
    progress_queue=None,
) -> FileAttemptOutcome:
    plan_row = work.plan_row
    final_path = resolve_project_path(config, plan_row["local_path"])
    if work.attempt == 1 and config.download.skip_existing and not config.download.overwrite_existing and final_path.exists():
        validation = validate_cutout(final_path, config)
        if validation.status in VALIDATION_OK:
            result = DownloadResult(
                plan_id=plan_row.get("plan_id"),
                cutout_key=plan_row["cutout_key"],
                local_path=final_path,
                success=True,
                status="skipped_existing",
                file_size_bytes=validation.file_size_bytes,
                sha256=validation.sha256,
                attempts=0,
                reason="existing file validates",
            )
            completed = CompletedDownload(
                plan_row=plan_row,
                started_at=None,
                result=result,
                validation=validation,
            )
            return FileAttemptOutcome(work=work, completed=completed, retryable=False)

    client = session or (session_pool.get() if session_pool is not None else make_session(config, 1))
    owns_client = session is None and session_pool is None
    started_at = work.started_at or utcnow()
    started_monotonic = time.monotonic()
    try:
        attempt = _download_one_attempt(
            plan_row,
            config,
            client,
            attempt=work.attempt,
            deadline_monotonic=work.deadline_monotonic,
            progress_callback=_file_progress_callback_for_queue(progress_queue, plan_row, work.attempt),
            progress_interval_seconds=PROGRESS_UPDATE_INTERVAL_SECONDS,
        )
        result = attempt.completed.result
        validation = None
        if result.success:
            validation = validate_cutout(
                result.local_path,
                config,
                precomputed_sha256=result.sha256,
                precomputed_sha256_file_size=result.file_size_bytes,
            )
        elapsed = max(time.monotonic() - started_monotonic, 0.0)
        completed = CompletedDownload(
            plan_row=plan_row,
            started_at=started_at,
            result=result,
            validation=validation,
            http_status=attempt.completed.http_status,
            elapsed_seconds=elapsed,
            bytes_per_second=(result.file_size_bytes / elapsed) if result.file_size_bytes and elapsed > 0 else None,
            exception_class=attempt.completed.exception_class,
        )
        return FileAttemptOutcome(
            work=work,
            completed=completed,
            retryable=attempt.retryable,
            retry_after_seconds=attempt.retry_after_seconds,
        )
    finally:
        if owns_client:
            client.close()


def _download_one_attempt(
    plan_row: dict[str, Any],
    config: Config,
    session: requests.Session,
    *,
    attempt: int,
    deadline_monotonic: float | None,
    progress_callback: Any | None = None,
    progress_interval_seconds: float = PROGRESS_UPDATE_INTERVAL_SECONDS,
) -> FileAttemptOutcome:
    cutout_key = plan_row["cutout_key"]
    final_path = resolve_project_path(config, plan_row["local_path"])
    partial_dir = config.project.data_root / "partial"
    partial_dir.mkdir(parents=True, exist_ok=True)
    partial_path = partial_dir / f"{cutout_key}.fits{config.download.partial_suffix}"
    final_path.parent.mkdir(parents=True, exist_ok=True)

    url = plan_row.get("cutout_url")
    if not url:
        result = DownloadResult(
            plan_id=plan_row.get("plan_id"),
            cutout_key=cutout_key,
            local_path=final_path,
            success=False,
            status="failed",
            attempts=attempt,
            reason="missing cutout URL",
        )
        return FileAttemptOutcome(
            work=FileDownloadWork(0, plan_row, attempt, deadline_monotonic=deadline_monotonic),
            completed=CompletedDownload(plan_row=plan_row, started_at=None, result=result),
            retryable=False,
        )

    retry_statuses = set(config.download.retry.retry_http_status)
    sha = hashlib.sha256()
    bytes_written = 0
    last_progress_emit = time.monotonic()
    try:
        _raise_if_deadline_exceeded(deadline_monotonic)
        with session.get(
            url,
            stream=True,
            timeout=_request_timeout(config, deadline_monotonic),
        ) as response:
            http_status = int(response.status_code)
            if http_status in retry_statuses:
                result = DownloadResult(
                    plan_id=plan_row.get("plan_id"),
                    cutout_key=cutout_key,
                    local_path=final_path,
                    success=False,
                    status="failed",
                    file_size_bytes=0,
                    attempts=attempt,
                    reason=f"HTTP {http_status}",
                )
                return FileAttemptOutcome(
                    work=FileDownloadWork(0, plan_row, attempt, deadline_monotonic=deadline_monotonic),
                    completed=CompletedDownload(
                        plan_row=plan_row,
                        started_at=None,
                        result=result,
                        http_status=http_status,
                    ),
                    retryable=True,
                    retry_after_seconds=_parse_retry_after(response.headers.get("Retry-After")),
                )
            response.raise_for_status()
            rate_window_started = time.monotonic()
            rate_window_bytes = 0
            with partial_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=config.download.chunk_size_bytes):
                    _raise_if_deadline_exceeded(deadline_monotonic)
                    if not chunk:
                        continue
                    handle.write(chunk)
                    sha.update(chunk)
                    bytes_written += len(chunk)
                    _raise_if_low_speed(
                        config,
                        bytes_written=bytes_written,
                        rate_window_started=rate_window_started,
                        rate_window_bytes=rate_window_bytes,
                    )
                    now = time.monotonic()
                    if (
                        config.download.min_download_rate_bytes_per_second > 0
                        and now - rate_window_started >= config.download.low_speed_time_sec
                    ):
                        rate_window_started = now
                        rate_window_bytes = bytes_written
                    if progress_callback is not None:
                        if now - last_progress_emit >= progress_interval_seconds:
                            progress_callback(bytes_written, attempt)
                            last_progress_emit = now
        if bytes_written <= 0:
            raise IOError("empty response body")
        partial_path.replace(final_path)
        result = DownloadResult(
            plan_id=plan_row.get("plan_id"),
            cutout_key=cutout_key,
            local_path=final_path,
            success=True,
            status="downloaded",
            file_size_bytes=bytes_written,
            sha256=sha.hexdigest(),
            attempts=attempt,
        )
        return FileAttemptOutcome(
            work=FileDownloadWork(0, plan_row, attempt, deadline_monotonic=deadline_monotonic),
            completed=CompletedDownload(
                plan_row=plan_row,
                started_at=None,
                result=result,
                http_status=http_status,
            ),
            retryable=False,
        )
    except Exception as exc:  # noqa: BLE001 - retry boundary
        if partial_path.exists():
            partial_path.unlink()
        status = getattr(getattr(exc, "response", None), "status_code", None)
        result = DownloadResult(
            plan_id=plan_row.get("plan_id"),
            cutout_key=cutout_key,
            local_path=final_path,
            success=False,
            status="failed",
            file_size_bytes=bytes_written,
            attempts=attempt,
            reason=str(exc),
        )
        return FileAttemptOutcome(
            work=FileDownloadWork(0, plan_row, attempt, deadline_monotonic=deadline_monotonic),
            completed=CompletedDownload(
                plan_row=plan_row,
                started_at=None,
                result=result,
                http_status=int(status) if status is not None else None,
                exception_class=exc.__class__.__name__,
            ),
            retryable=_is_retryable_exception(exc),
        )


def _record_file_download_result(
    conn,
    run_id: str | None,
    config: Config,
    completed: CompletedDownload,
    target_results: dict[str, TargetDownloadResult],
    summary: DownloadSummary,
    console: Console | None,
    verbose: bool,
) -> None:
    _record_completed_download(conn, run_id, config, completed, summary)
    source_id = str(completed.plan_row.get("source_id"))
    target_result = target_results.get(source_id)
    if target_result is not None:
        target_result.completed.append(completed)
        if completed.result.status == "skipped_existing":
            target_result.skipped_files += 1
        elif completed.result.success:
            target_result.downloaded_files += 1
            target_result.bytes_downloaded += completed.result.file_size_bytes
        if not completed.result.success or _validation_failed(completed.validation):
            target_result.failed_files += 1
    if verbose and console is not None:
        console.print(_format_file_detail_line(completed))


def _completed_target_count(target_results: dict[str, TargetDownloadResult]) -> int:
    return sum(
        1
        for target_result in target_results.values()
        if len(target_result.completed) >= target_result.planned_files
    )


def _finalize_target_summary(
    summary: DownloadSummary,
    target_results: dict[str, TargetDownloadResult],
    console: Console | None,
    verbose: bool,
) -> None:
    for target_result in target_results.values():
        if target_result.failed_files == 0:
            summary.successful_targets += 1
        elif target_result.downloaded_files or target_result.skipped_files:
            summary.partially_failed_targets += 1
        else:
            summary.failed_targets += 1
        if verbose and console is not None:
            status = _target_status(target_result)
            console.print(
                f"target {target_result.source_id}"
                f"{f' ({target_result.source_name})' if target_result.source_name else ''} "
                f"status={status} planned={target_result.planned_files} "
                f"skipped={target_result.skipped_files} downloaded={target_result.downloaded_files} "
                f"failed={target_result.failed_files} bytes={_format_bytes(target_result.bytes_downloaded)}"
            )


def _record_target_download_result(
    conn,
    run_id: str | None,
    config: Config,
    target_result: TargetDownloadResult,
    summary: DownloadSummary,
    console: Console | None,
    verbose: bool,
) -> None:
    if target_result.failed_files == 0:
        summary.successful_targets += 1
    elif target_result.downloaded_files or target_result.skipped_files:
        summary.partially_failed_targets += 1
    else:
        summary.failed_targets += 1

    for completed in target_result.completed:
        _record_completed_download(conn, run_id, config, completed, summary)

    if target_result.worker_error:
        summary.failed += target_result.planned_files
        for plan_row in _uncompleted_rows(target_result):
            record_failure(
                conn,
                {
                    "run_id": run_id,
                    "source_id": target_result.source_id,
                    "product_id": plan_row.get("product_id"),
                    "plan_id": plan_row.get("plan_id"),
                    "phase": "download",
                    "status": "retryable",
                    "reason": target_result.worker_error,
                    "url": plan_row.get("cutout_url"),
                    "local_path": plan_row.get("local_path"),
                    "attempt": 0,
                    "max_attempts": config.download.retry.attempts,
                },
            )

    if verbose and console is not None:
        status = _target_status(target_result)
        console.print(
            f"target {target_result.source_id}"
            f"{f' ({target_result.source_name})' if target_result.source_name else ''} "
            f"status={status} "
            f"RA={target_result.ra_deg} Dec={target_result.dec_deg} "
            f"planned={target_result.planned_files} skipped={target_result.skipped_files} "
            f"downloaded={target_result.downloaded_files} failed={target_result.failed_files} "
            f"bytes={_format_bytes(target_result.bytes_downloaded)} elapsed={target_result.elapsed_seconds:.1f}s"
        )
        if target_result.worker_error:
            console.print(f"  worker error: {target_result.worker_error}")
        for line in _verbose_file_detail_lines(target_result, limit=VERBOSE_FILE_DETAIL_LIMIT):
            console.print(line)


def _record_completed_download(
    conn,
    run_id: str | None,
    config: Config,
    completed: CompletedDownload,
    summary: DownloadSummary,
) -> None:
    plan_row = completed.plan_row
    result = completed.result
    validation = completed.validation
    if result.status == "skipped_existing":
        summary.skipped += 1
    elif result.success:
        summary.downloaded += 1
        summary.bytes_downloaded += result.file_size_bytes
    if result.success:
        cutout_id = upsert_cutout_record(
            conn,
            _cutout_record_from_plan(plan_row, result, completed.started_at, run_id),
            commit=False,
        )
        if validation is None:
            validation = validate_cutout(result.local_path, config)
        record_validation(conn, _validation_record(run_id, cutout_id, result, validation, config), commit=False)
        if not validation.status.startswith("passed"):
            summary.failed += 1
            record_failure(
                conn,
                {
                    "run_id": run_id,
                    "source_id": plan_row.get("source_id"),
                    "product_id": plan_row.get("product_id"),
                    "plan_id": plan_row.get("plan_id"),
                    "cutout_id": cutout_id,
                    "phase": "validation",
                    "status": "open",
                    "reason": validation.reason,
                    "local_path": str(result.local_path),
                },
                commit=False,
            )
    else:
        summary.failed += 1
        record_failure(
            conn,
            {
                "run_id": run_id,
                "source_id": plan_row.get("source_id"),
                "product_id": plan_row.get("product_id"),
                "plan_id": plan_row.get("plan_id"),
                "phase": "download",
                "status": "retryable",
                "reason": result.reason or "download failed",
                "url": plan_row.get("cutout_url"),
                "local_path": str(result.local_path),
                "attempt": result.attempts,
                "max_attempts": config.download.retry.attempts,
            },
            commit=False,
        )
    conn.commit()


def _planned_download_rows(
    conn,
    run_id: str | None,
    limit: int | None,
    *,
    overwrite: bool = False,
):
    actions = OVERWRITE_ACTIONS if overwrite else DOWNLOAD_ACTIONS
    params: list[Any] = []
    clause = f"dp.action IN ({','.join('?' for _ in actions)})"
    params.extend(sorted(actions))
    if run_id:
        clause += " AND dp.run_id = ?"
        params.append(run_id)
    else:
        latest = conn.execute(
            f"""
            SELECT run_id
            FROM download_plan
            WHERE action IN ({','.join('?' for _ in actions)})
              AND run_id IS NOT NULL
            ORDER BY plan_id DESC
            LIMIT 1
            """,
            tuple(sorted(actions)),
        ).fetchone()
        if latest is not None:
            clause += " AND dp.run_id = ?"
            params.append(latest["run_id"])
    sql = f"""
    SELECT
      dp.*, s.source_name, s.ra_deg AS source_ra_deg, s.dec_deg AS source_dec_deg,
      p.access_url AS parent_access_url, p.cloud_access_json, p.parent_filename,
      p.collection, p.observation_id, p.detector_id, p.planning_period, p.processing_version,
      p.processing_date, p.bandpass, p.em_min, p.em_max
    FROM download_plan dp
    LEFT JOIN sources s ON s.source_id = dp.source_id
    LEFT JOIN discovery_products p ON p.product_id = dp.product_id
    WHERE {clause}
    ORDER BY COALESCE(dp.priority, 999999), dp.source_id, dp.plan_id
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, tuple(params)).fetchall()


def _cutout_record_from_plan(
    plan_row: dict[str, Any],
    result: DownloadResult,
    started_at: str | None,
    run_id: str | None,
) -> dict[str, Any]:
    downloaded = result.status == "downloaded"
    return {
        "cutout_key": plan_row["cutout_key"],
        "source_id": plan_row["source_id"],
        "product_id": plan_row.get("product_id"),
        "local_path": plan_row["local_path"],
        "file_exists": True,
        "file_size_bytes": result.file_size_bytes,
        "sha256": result.sha256,
        "access_method": plan_row.get("access_method", "onprem_cutout"),
        "parent_access_url": plan_row.get("parent_access_url"),
        "cloud_access_json": plan_row.get("cloud_access_json"),
        "cutout_url_used": plan_row.get("cutout_url"),
        "parent_filename": plan_row.get("parent_filename"),
        "collection": plan_row.get("collection"),
        "observation_id": plan_row.get("observation_id"),
        "detector_id": plan_row.get("detector_id"),
        "planning_period": plan_row.get("planning_period"),
        "processing_version": plan_row.get("processing_version"),
        "processing_date": plan_row.get("processing_date"),
        "bandpass": plan_row.get("bandpass"),
        "em_min": plan_row.get("em_min"),
        "em_max": plan_row.get("em_max"),
        "cutout_ra_deg": plan_row.get("cutout_ra_deg"),
        "cutout_dec_deg": plan_row.get("cutout_dec_deg"),
        "cutout_size_arcsec": plan_row.get("cutout_size_arcsec"),
        "download_started_at": started_at if downloaded else None,
        "download_completed_at": utcnow() if downloaded else None,
        "download_run_id": run_id if downloaded else None,
        "validation_status": None,
        "active": True,
    }


def _validation_record(
    run_id: str | None,
    cutout_id: int,
    result: DownloadResult,
    validation: ValidationResult,
    config: Config,
) -> dict[str, Any]:
    try:
        local_path = str(result.local_path.relative_to(config.project.root))
    except ValueError:
        local_path = str(result.local_path)
    return {
        "run_id": run_id,
        "cutout_id": cutout_id,
        "local_path": local_path,
        "status": validation.status,
        "reason": validation.reason,
        "warnings": validation.warnings,
        "errors": validation.errors,
        "file_size_bytes": validation.file_size_bytes,
        "sha256": validation.sha256,
        "required_hdus_present": validation.required_hdus_present,
        "image_shape": validation.image_shape,
        "flags_shape": validation.flags_shape,
        "variance_shape": validation.variance_shape,
        "zodi_shape": validation.zodi_shape,
        "psf_shape": validation.psf_shape,
        "wcwave_summary": validation.wcwave_summary,
        "spatial_wcs_valid": validation.wcs_summary.get("spatial_wcs_valid", False),
        "spectral_wcs_valid": validation.wcs_summary.get("spectral_wcs_valid", False),
        "hdu_summary": validation.hdu_summary,
        "wcs_summary": validation.wcs_summary,
        "psf_metadata": validation.psf_metadata,
        "header_metadata": validation.header_metadata,
    }


def _raise_if_deadline_exceeded(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise TimeoutError("total download timeout exceeded")


def _raise_if_low_speed(
    config: Config,
    *,
    bytes_written: int,
    rate_window_started: float,
    rate_window_bytes: int,
) -> None:
    min_rate = float(config.download.min_download_rate_bytes_per_second or 0.0)
    if min_rate <= 0:
        return
    elapsed = time.monotonic() - rate_window_started
    if elapsed < float(config.download.low_speed_time_sec):
        return
    window_bytes = max(bytes_written - rate_window_bytes, 0)
    rate = window_bytes / elapsed if elapsed > 0 else 0.0
    if rate < min_rate:
        raise TimeoutError(
            "download rate "
            f"{_format_bytes(rate)}/s below minimum {_format_bytes(min_rate)}/s "
            f"for {elapsed:.1f}s"
        )


def _request_timeout(config: Config, deadline_monotonic: float | None) -> tuple[float, float]:
    connect_timeout = float(config.download.connect_timeout_sec)
    read_timeout = float(config.download.read_timeout_sec)
    if deadline_monotonic is None:
        return connect_timeout, read_timeout
    remaining = max(deadline_monotonic - time.monotonic(), 0.001)
    return min(connect_timeout, remaining), min(read_timeout, remaining)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        return max(parsed.timestamp() - time.time(), 0.0)
    except (TypeError, ValueError, OSError):
        return None


def _is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    retryable = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
    )
    return isinstance(exc, retryable)


def _host_active_limit(config: Config, workers: int) -> int:
    configured = getattr(config.download, "per_host_max_concurrency", None)
    if configured is None:
        return max(1, workers)
    return max(1, min(workers, int(configured)))


def _take_ready_work(
    ready: deque[FileDownloadWork],
    now: float,
    host_next_start: dict[str, float],
    active_by_host: dict[str, int],
    host_active_limit: int,
) -> FileDownloadWork | None:
    for _ in range(len(ready)):
        work = ready.popleft()
        host = _request_host(work.plan_row)
        if active_by_host.get(host, 0) < host_active_limit and now >= host_next_start.get(host, 0.0):
            return work
        ready.append(work)
    return None


def _scheduler_wait_timeout(
    ready: deque[FileDownloadWork],
    delayed: list[tuple[float, int, FileDownloadWork]],
    pending: dict[Any, FileDownloadWork],
    host_next_start: dict[str, float],
    active_by_host: dict[str, int],
    host_active_limit: int,
) -> float:
    now = time.monotonic()
    candidates: list[float] = []
    if delayed:
        candidates.append(max(delayed[0][0] - now, 0.0))
    if ready:
        for work in ready:
            host = _request_host(work.plan_row)
            if active_by_host.get(host, 0) < host_active_limit:
                candidates.append(max(host_next_start.get(host, 0.0) - now, 0.0))
    if not candidates:
        return 0.1 if pending else 0.0
    return min(max(min(candidates), 0.0), 0.1 if pending else max(min(candidates), 0.0))


def _request_host(plan_row: dict[str, Any]) -> str:
    url = plan_row.get("cutout_url") or ""
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _scheduler_state_event(
    ready: deque[FileDownloadWork],
    delayed: list[tuple[float, int, FileDownloadWork]],
    pending: dict[Any, FileDownloadWork],
    active_by_host: dict[str, int],
    last: str,
) -> dict[str, Any]:
    return {
        "event": "scheduler_state",
        "ready": len(ready),
        "delayed": len(delayed),
        "active": len(pending),
        "active_by_host": dict(active_by_host),
        "last": last,
    }


def _worker_exception_outcome(work: FileDownloadWork, exc: Exception) -> FileAttemptOutcome:
    final_path = Path(work.plan_row.get("local_path") or "")
    result = DownloadResult(
        plan_id=work.plan_row.get("plan_id"),
        cutout_key=work.plan_row["cutout_key"],
        local_path=final_path,
        success=False,
        status="failed",
        attempts=work.attempt,
        reason=f"{exc.__class__.__name__}: {exc}",
    )
    return FileAttemptOutcome(
        work=work,
        completed=CompletedDownload(
            plan_row=work.plan_row,
            started_at=work.started_at,
            result=result,
            exception_class=exc.__class__.__name__,
        ),
        retryable=True,
    )


def _retry_delay_for_outcome(outcome: FileAttemptOutcome, config: Config) -> float | None:
    retry = config.download.retry
    if not outcome.retryable:
        return None
    base = retry.backoff_seconds[min(outcome.work.attempt - 1, len(retry.backoff_seconds) - 1)]
    delay = float(base)
    if retry.honor_retry_after and outcome.retry_after_seconds is not None:
        max_backoff = max(float(value) for value in retry.backoff_seconds) if retry.backoff_seconds else delay
        delay = min(float(outcome.retry_after_seconds), max_backoff)
    jitter = float(getattr(retry, "jitter_seconds", 0.0) or 0.0)
    if jitter > 0:
        delay += random.uniform(0.0, jitter)
    return max(delay, 0.0)


def _should_requeue(outcome: FileAttemptOutcome, config: Config, delay: float | None) -> bool:
    if not outcome.retryable:
        return False
    if outcome.work.attempt >= config.download.retry.attempts:
        return False
    deadline = outcome.work.deadline_monotonic
    if deadline is not None and time.monotonic() + float(delay or 0.0) >= deadline:
        return False
    return True


def _finalized_outcome(outcome: FileAttemptOutcome, config: Config) -> CompletedDownload:
    completed = outcome.completed
    if (
        outcome.retryable
        and not completed.result.success
        and outcome.work.attempt < config.download.retry.attempts
        and outcome.work.deadline_monotonic is not None
        and time.monotonic() >= outcome.work.deadline_monotonic
    ):
        completed.result.reason = f"{completed.result.reason}; total download timeout exceeded"
    return completed


def _file_retry_event(
    outcome: FileAttemptOutcome,
    delay: float,
    ready: int,
    delayed: int,
    active: int,
) -> dict[str, Any]:
    plan_row = outcome.work.plan_row
    return {
        "event": "file_retry",
        "source_id": plan_row.get("source_id"),
        "cutout_key": plan_row.get("cutout_key"),
        "parent_filename": plan_row.get("parent_filename"),
        "attempt": outcome.work.attempt,
        "next_attempt": outcome.work.attempt + 1,
        "retry_delay": float(delay),
        "http_status": outcome.completed.http_status,
        "reason": outcome.completed.result.reason,
        "ready": ready,
        "delayed": delayed,
        "active": active,
    }


def _file_done_event_from_completed(completed: CompletedDownload) -> dict[str, Any]:
    status = completed.result.status if completed.result.success else "failed"
    if completed.result.success and _validation_failed(completed.validation):
        status = "validation_failed"
    return {
        "event": "file_done",
        "source_id": completed.plan_row.get("source_id"),
        "cutout_key": completed.result.cutout_key,
        "parent_filename": completed.plan_row.get("parent_filename"),
        "status": status,
        "bytes": completed.result.file_size_bytes if completed.result.status == "downloaded" else 0,
        "attempt": completed.result.attempts,
        "reason": completed.result.reason,
        "http_status": completed.http_status,
    }


def _file_progress_callback_for_queue(progress_queue, plan_row: dict[str, Any], attempt: int):
    if progress_queue is None:
        return None

    def callback(bytes_written: int, _attempt: int) -> None:
        _emit_progress_event(
            progress_queue,
            {
                "event": "file_progress",
                "source_id": plan_row.get("source_id"),
                "cutout_key": plan_row.get("cutout_key"),
                "parent_filename": plan_row.get("parent_filename"),
                "bytes": int(bytes_written),
                "attempt": attempt,
            },
        )

    return callback


def _format_retry_event_line(event: dict[str, Any]) -> str:
    name = event.get("parent_filename") or event.get("cutout_key")
    status = f" HTTP={event.get('http_status')}" if event.get("http_status") is not None else ""
    return (
        f"  retry {name} attempt={event.get('attempt')} "
        f"next={event.get('next_attempt')} delay={float(event.get('retry_delay') or 0):.2f}s"
        f"{status} reason={event.get('reason')}"
    )



def _new_progress_state(target_total: int, files_total: int, workers: int) -> dict[str, Any]:
    return {
        "target_total": target_total,
        "files_total": files_total,
        "workers": workers,
        "completed_targets": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "bytes_complete": 0,
        "active_bytes": {},
        "completed_file_keys": set(),
        "active": 0,
        "ready": files_total,
        "delayed": 0,
        "last": "-",
    }


def _apply_progress_event(progress_state: dict[str, Any], event: dict[str, Any]) -> None:
    source_id = str(event.get("source_id") or "")
    cutout_key = str(event.get("cutout_key") or "")
    file_key = (source_id, cutout_key)
    if file_key in progress_state["completed_file_keys"]:
        return
    event_type = event.get("event")
    if event_type == "scheduler_state":
        progress_state["active"] = int(event.get("active") or 0)
        progress_state["ready"] = int(event.get("ready") or 0)
        progress_state["delayed"] = int(event.get("delayed") or 0)
        progress_state["last"] = str(event.get("last") or progress_state["last"])
        return
    if event_type == "file_progress":
        progress_state["active_bytes"][file_key] = int(event.get("bytes") or 0)
        progress_state["last"] = _short_event_label(event)
        return
    if event_type == "file_retry":
        progress_state["active_bytes"].pop(file_key, None)
        progress_state["ready"] = int(event.get("ready") or progress_state["ready"])
        progress_state["delayed"] = int(event.get("delayed") or progress_state["delayed"])
        progress_state["active"] = int(event.get("active") or progress_state["active"])
        progress_state["last"] = _short_event_label(event)
        return
    if event_type != "file_done":
        return
    progress_state["completed_file_keys"].add(file_key)
    progress_state["active_bytes"].pop(file_key, None)
    status = event.get("status")
    if status == "skipped_existing":
        progress_state["skipped"] += 1
    elif status == "downloaded":
        progress_state["downloaded"] += 1
        progress_state["bytes_complete"] += int(event.get("bytes") or 0)
    else:
        progress_state["failed"] += 1
    progress_state["last"] = _short_event_label(event)


def _sync_progress_state_from_summary(
    progress_state: dict[str, Any],
    summary: DownloadSummary,
    target_result: TargetDownloadResult | None = None,
) -> None:
    progress_state["completed_targets"] = (
        summary.successful_targets
        + summary.partially_failed_targets
        + summary.failed_targets
    )
    progress_state["downloaded"] = summary.downloaded
    progress_state["skipped"] = summary.skipped
    progress_state["failed"] = summary.failed
    progress_state["bytes_complete"] = summary.bytes_downloaded
    if target_result is not None:
        for completed in target_result.completed:
            key = (str(target_result.source_id), str(completed.result.cutout_key))
            progress_state["active_bytes"].pop(key, None)
            progress_state["completed_file_keys"].add(key)
        progress_state["last"] = _short_target_label(target_result)


def _update_download_progress_bar(bar: Progress, task_id: int, progress_state: dict[str, Any]) -> None:
    completed_files = (
        progress_state["downloaded"]
        + progress_state["skipped"]
        + progress_state["failed"]
    )
    active_bytes = sum(progress_state["active_bytes"].values())
    bar.update(
        task_id,
        targets=f"{progress_state['completed_targets']}/{progress_state['target_total']}",
        active=f"{progress_state['active']}/{progress_state['workers']}",
        files=f"{completed_files}/{progress_state['files_total']}",
        downloaded=progress_state["downloaded"],
        skipped=progress_state["skipped"],
        failed=progress_state["failed"],
        retry=progress_state["delayed"],
        queue=progress_state["ready"],
        bytes=_format_bytes(progress_state["bytes_complete"] + active_bytes),
        last=progress_state["last"],
    )


def _print_live_progress_event(console: Console, event: dict[str, Any], verbose: bool) -> None:
    if not verbose:
        return
    if event.get("event") == "file_progress":
        console.print(
            f"progress target={event.get('source_id')} file={_short_event_label(event)} "
            f"bytes={_format_bytes(event.get('bytes'))} attempt={event.get('attempt')}"
        )
    elif event.get("event") == "file_retry":
        console.print(_format_retry_event_line(event))


def _short_event_label(event: dict[str, Any]) -> str:
    label = str(event.get("parent_filename") or event.get("cutout_key") or event.get("source_id") or "-")
    if len(label) > 18:
        return label[:15] + "..."
    return label


def _drain_progress_queue(progress_queue) -> list[dict[str, Any]]:
    if progress_queue is None:
        return []
    events = []
    while True:
        try:
            events.append(progress_queue.get_nowait())
        except Empty:
            break
        except Exception:
            break
    return events


def _emit_progress_event(progress_queue, event: dict[str, Any]) -> None:
    if progress_queue is None:
        return
    try:
        progress_queue.put(event)
    except Exception:
        return


def _file_progress_callback(job: TargetDownloadJob, plan_row: dict[str, Any]):
    if job.progress_queue is None:
        return None

    def callback(bytes_written: int, attempt: int) -> None:
        _emit_progress_event(
            job.progress_queue,
            {
                "event": "file_progress",
                "source_id": job.source_id,
                "cutout_key": plan_row.get("cutout_key"),
                "parent_filename": plan_row.get("parent_filename"),
                "bytes": int(bytes_written),
                "attempt": attempt,
            },
        )

    return callback


def _file_done_event(job: TargetDownloadJob, completed: CompletedDownload) -> dict[str, Any]:
    result = completed.result
    status = result.status if result.success else "failed"
    if result.success and _validation_failed(completed.validation):
        status = "validation_failed"
    return {
        "event": "file_done",
        "source_id": job.source_id,
        "cutout_key": result.cutout_key,
        "parent_filename": completed.plan_row.get("parent_filename"),
        "status": status,
        "bytes": result.file_size_bytes if result.status == "downloaded" else 0,
        "attempt": result.attempts,
        "reason": result.reason,
    }


def _plan_row_sort_key(row: dict[str, Any]) -> tuple[int, str, int]:
    priority = row.get("priority")
    try:
        priority_int = int(priority) if priority is not None else 999999
    except (TypeError, ValueError):
        priority_int = 999999
    try:
        plan_id = int(row.get("plan_id") or 0)
    except (TypeError, ValueError):
        plan_id = 0
    return priority_int, str(row.get("source_id") or ""), plan_id


def _first_float(*values: Any) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _validation_failed(validation: ValidationResult | None) -> bool:
    return validation is not None and not validation.status.startswith("passed")


def _uncompleted_rows(target_result: TargetDownloadResult) -> list[dict[str, Any]]:
    completed_plan_ids = {
        completed.plan_row.get("plan_id")
        for completed in target_result.completed
    }
    return [
        row
        for row in target_result.plan_rows
        if row.get("plan_id") not in completed_plan_ids
    ]


def _target_status(target_result: TargetDownloadResult) -> str:
    if target_result.worker_error:
        return "worker_failed"
    if target_result.failed_files == 0:
        if target_result.downloaded_files == 0 and target_result.skipped_files:
            return "skipped_existing"
        return "success"
    if target_result.downloaded_files or target_result.skipped_files:
        return "partial_failed"
    return "failed"


def _short_target_label(target_result: TargetDownloadResult) -> str:
    label = str(target_result.source_id)
    if len(label) > 18:
        return label[:15] + "..."
    return label


def _format_bytes(size: int | float | None) -> str:
    value = float(size or 0)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    unit = units[0]
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            break
        value /= 1024.0
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def _verbose_file_detail_lines(target_result: TargetDownloadResult, limit: int) -> list[str]:
    lines: list[str] = []
    ordered = sorted(
        target_result.completed,
        key=lambda completed: _file_detail_priority(completed),
    )
    for completed in ordered[:limit]:
        lines.append(_format_file_detail_line(completed))
    omitted = max(len(ordered) - limit, 0)
    if omitted:
        lines.append(f"  ... {omitted} more file outcome(s) omitted")
    return lines


def _file_detail_priority(completed: CompletedDownload) -> tuple[int, str]:
    result = completed.result
    if not result.success or _validation_failed(completed.validation):
        priority = 0
    elif result.status == "skipped_existing":
        priority = 1
    else:
        priority = 2
    return priority, str(completed.plan_row.get("parent_filename") or result.cutout_key)


def _format_file_detail_line(completed: CompletedDownload) -> str:
    result = completed.result
    plan_row = completed.plan_row
    name = plan_row.get("parent_filename") or result.cutout_key[:12]
    http = f" http={completed.http_status}" if completed.http_status is not None else ""
    speed = f" rate={_format_rate(completed.bytes_per_second)}" if completed.bytes_per_second else ""
    elapsed = f" elapsed={completed.elapsed_seconds:.2f}s" if completed.elapsed_seconds else ""
    if not result.success:
        return f"  fail {name} attempts={result.attempts}{http}{elapsed} reason={result.reason}"
    validation = completed.validation
    validation_status = validation.status if validation else "not_validated"
    if result.status == "skipped_existing":
        return (
            f"  skip {name} existing valid "
            f"validation={validation_status} path={plan_row.get('local_path')}"
        )
    if _validation_failed(validation):
        reason = validation.reason if validation else "validation failed"
        return f"  fail {name} downloaded{http}{elapsed}{speed} validation={validation_status} reason={reason}"
    return (
        f"  done {name} bytes={_format_bytes(result.file_size_bytes)} "
        f"attempts={result.attempts}{http}{elapsed}{speed} validation={validation_status}"
    )


def _format_rate(bytes_per_second: float | None) -> str:
    if not bytes_per_second:
        return "0.00 MiB/s"
    return f"{bytes_per_second / (1024 * 1024):.2f} MiB/s"
