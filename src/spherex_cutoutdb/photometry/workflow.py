"""Low-storage photometry orchestration."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from dataclasses import asdict, dataclass, field
import json
import os
from queue import Empty, Queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pandas as pd
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn

from spherex_cutoutdb.config import Config
from spherex_cutoutdb.database import connect
from spherex_cutoutdb.downloader import resolve_project_path

from .measure import MeasurementResult, measure_cutout
from .measurement_plan import build_photometry_plan
from .outputs import (
    full_qa_measurement_path,
    validate_full_qa_measurement,
    validate_full_qa_outputs,
    validate_source_outputs,
    write_full_measurement_qa_batch,
    write_full_measurement_qa_outputs,
    write_full_qa_manifest,
    write_source_outputs,
)
from .result_store import (
    active_sources,
    finish_photometry_run,
    latest_cutout_for_key,
    mark_work_item_state,
    record_measurement,
    record_output_product,
    record_photometry_failure,
    source_by_id,
    source_by_name,
    start_photometry_run,
    upsert_source_summary,
)


ProgressCallback = Callable[[dict[str, Any]], None]

_SQLITE_WRITE_LOCK = threading.RLock()
_AUTO_SOURCE_WORKER_CAP = 8
_FULL_QA_AUTO_SOURCE_WORKER_CAP = 2


@dataclass(slots=True)
class PhotometrySummary:
    planned: int = 0
    skipped: int = 0
    measured: int = 0
    failed: int = 0
    science_recommended: int = 0
    downloaded: int = 0
    deleted_cutouts: int = 0
    plan_seconds: float = 0.0
    measure_seconds: float = 0.0
    output_seconds: float = 0.0
    qa_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    qa_plots_planned: int = 0
    qa_plots_written: int = 0
    qa_plots_failed: int = 0
    states: dict[str, int] = field(default_factory=dict)
    output_paths: dict[str, str] = field(default_factory=dict)
    worker_backend: str = ""
    worker_pid_count: int = 0


@dataclass(slots=True)
class _ProcessSourceState:
    source: dict[str, Any]
    summary: PhotometrySummary = field(default_factory=PhotometrySummary)
    measurements: list[MeasurementResult] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    output_paths: dict[str, Path] = field(default_factory=dict)
    terminal: int = 0
    finalized: bool = False
    started_at: float = field(default_factory=time.perf_counter)


def plan_photometry(
    conn,
    config: Config,
    *,
    source_ids: list[str] | None = None,
    photometry_run_id: str | None = None,
    force_remeasure: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    return build_photometry_plan(
        conn,
        config,
        photometry_run_id=photometry_run_id,
        source_ids=source_ids,
        force_remeasure=force_remeasure,
    )


def run_source_photometry(
    conn,
    config: Config,
    *,
    source_id: str | None = None,
    source_name: str | None = None,
    run_id: str | None = None,
    photometry_run_id: str | None = None,
    qa_level: str | None = None,
    progress: bool = True,
    verbose: bool = False,
    console=None,
    progress_callback: ProgressCallback | None = None,
    force_remeasure: bool = False,
) -> PhotometrySummary:
    source = _resolve_source(conn, source_id=source_id, source_name=source_name)
    if source is None:
        raise ValueError("source not found")
    own_run = photometry_run_id is None
    photometry_run_id = photometry_run_id or start_photometry_run(conn, config, run_id)
    summary = PhotometrySummary()
    source_started = time.perf_counter()
    try:
        plan_started = time.perf_counter()
        with _SQLITE_WRITE_LOCK:
            items, counts = build_photometry_plan(
                conn,
                config,
                photometry_run_id=photometry_run_id,
                source_ids=[source["source_id"]],
                force_remeasure=force_remeasure,
                commit=False,
            )
        plan_seconds = time.perf_counter() - plan_started
        summary.plan_seconds = plan_seconds
        summary.planned = len(items)
        summary.states = counts
        measurements: list[MeasurementResult] = []
        failures: list[dict[str, Any]] = []
        _verbose(
            console,
            verbose,
            f"Photometry source {_source_label(source)}: planned={summary.planned} "
            f"states={_format_counts(counts)} qa_level={qa_level or config.photometry.qa_level} "
            f"cleanup_successful_cutouts={config.photometry.cleanup.delete_successful_cutouts} "
            f"plan_sec={plan_seconds:.2f}",
        )
        def emit_progress(
            item: dict[str, Any],
            *,
            action: str,
            state: str,
            index: int,
            downloaded_delta: int = 0,
        ) -> None:
            event = _progress_event(source, item, summary, action=action, state=state, index=index, downloaded_delta=downloaded_delta)
            if progress_callback is not None:
                progress_callback(event)

        def process_item(item: dict[str, Any], index: int) -> None:
            state = item["state"]
            cutout_label = _cutout_label(item)
            before_downloaded = summary.downloaded
            _verbose(console, verbose, f"  item {index}/{summary.planned} state={state} {cutout_label}")
            if state == "photometry_valid":
                summary.skipped += 1
                _verbose(console, verbose, f"  skipped {cutout_label}: matching photometry already exists")
                emit_progress(item, action="skipped", state=state, index=index)
                return
            if state == "calibration_missing":
                summary.failed += 1
                reason = item["reason"] or "missing calibration"
                _record_item_failure(conn, photometry_run_id, item, "calibration", reason)
                failures.append({"work_item_id": item["work_item_id"], "failure_type": "calibration", "reason": item["reason"]})
                _verbose(console, verbose, f"  blocked {cutout_label}: calibration_missing reason={reason}")
                emit_progress(item, action="failed", state=state, index=index)
                return
            if state == "cutout_missing_or_invalid":
                summary.failed += 1
                reason = item["reason"] or "cutout is missing or invalid"
                _record_item_failure(conn, photometry_run_id, item, "input", reason)
                failures.append({"work_item_id": item["work_item_id"], "failure_type": "input", "reason": reason})
                _verbose(
                    console,
                    verbose,
                    f"  blocked {cutout_label}: cutout_missing_or_invalid reason={reason}; "
                    "run spxcutdb plan/download/validate before photometry",
                )
                emit_progress(item, action="failed", state=state, index=index)
                return
            if state != "cutout_valid_measurement_missing":
                summary.failed += 1
                reason = item["reason"] or f"blocked state: {state}"
                _record_item_failure(conn, photometry_run_id, item, "validation", reason)
                failures.append({"work_item_id": item["work_item_id"], "failure_type": "validation", "reason": item["reason"]})
                _verbose(console, verbose, f"  blocked {cutout_label}: state={state} reason={reason}")
                emit_progress(
                    item,
                    action="failed",
                    state=state,
                    index=index,
                    downloaded_delta=summary.downloaded - before_downloaded,
                )
                return
            try:
                _verbose(console, verbose, f"  measuring {cutout_label}")
                measurement = _measure_item(conn, config, item, photometry_run_id)
            except Exception as exc:  # noqa: BLE001 - per-measurement failure record
                summary.failed += 1
                with _SQLITE_WRITE_LOCK:
                    mark_work_item_state(conn, item["work_item_id"], "failed_fit", str(exc))
                _record_item_failure(conn, photometry_run_id, item, "fit", str(exc), exc.__class__.__name__)
                failures.append({"work_item_id": item["work_item_id"], "failure_type": "fit", "reason": str(exc)})
                _verbose(console, verbose, f"  failed_fit {cutout_label}: {exc.__class__.__name__}: {exc}")
                emit_progress(
                    item,
                    action="failed",
                    state="failed_fit",
                    index=index,
                    downloaded_delta=summary.downloaded - before_downloaded,
                )
                return
            measurements.append(measurement)
            summary.measured += 1
            if measurement.row.get("science_recommended"):
                summary.science_recommended += 1
            _verbose(console, verbose, _measurement_line(measurement))
            emit_progress(
                item,
                action="measured",
                state="measured",
                index=index,
                downloaded_delta=summary.downloaded - before_downloaded,
            )

        show_item_progress = bool(progress and console is not None and progress_callback is None and items)
        if items:
            _verbose(console, verbose, f"Photometry cutouts: total={len(items)}")
        measure_started = time.perf_counter()
        if show_item_progress:
            with _source_item_progress(console) as bar:
                task_id = bar.add_task(
                    "Photometry cutouts",
                    total=len(items),
                    measured=0,
                    skipped=0,
                    failed=0,
                    sci=0,
                    downloaded=0,
                    last="-",
                )

                def progress_and_process(item: dict[str, Any], index: int) -> None:
                    process_item(item, index)
                    _update_source_item_progress(bar, task_id, summary, _item_last(item, index=index))

                for idx, item in enumerate(items, start=1):
                    progress_and_process(item, idx)
                    bar.update(task_id, advance=1)
                    bar.refresh()
        else:
            for idx, item in enumerate(items, start=1):
                process_item(item, idx)
        summary.measure_seconds = time.perf_counter() - measure_started
        if measurements:
            _verbose(console, verbose, f"Writing photometry outputs for {_source_label(source)}: measurements={len(measurements)} failures={len(failures)}")
            output_started = time.perf_counter()
            output_progress_last = {"written": 0}
            effective_qa_level = qa_level or config.photometry.qa_level
            compact_qa_level = "standard" if effective_qa_level == "full" else effective_qa_level

            def output_progress(event: dict[str, Any]) -> None:
                written = int(event.get("qa_written") or 0)
                total = int(event.get("qa_total") or 0)
                if progress_callback is not None:
                    progress_callback(
                        {
                            "event": "photometry_output",
                            "source_id": source.get("source_id"),
                            "source_name": source.get("source_name"),
                            "phase": event.get("phase"),
                            "qa_written": written,
                            "qa_total": total,
                        }
                    )
                if not verbose or console is None or total <= 0:
                    return
                if event.get("phase") == "qa_start":
                    _verbose(console, verbose, f"  full QA plots: total={total}")
                    return
                if event.get("phase") == "qa_parallel_unavailable":
                    _verbose(console, verbose, "  full QA plot process pool unavailable; writing serially")
                    return
                if written == total or written - output_progress_last["written"] >= 25:
                    output_progress_last["written"] = written
                    _verbose(console, verbose, f"  full QA plots: {written}/{total}")

            if progress_callback is not None:
                progress_callback(
                    {
                        "event": "photometry_output",
                        "source_id": source.get("source_id"),
                        "source_name": source.get("source_name"),
                        "phase": "compact_start",
                        "qa_written": 0,
                        "qa_total": 0,
                    }
                )
            paths = write_source_outputs(
                config=config,
                source=source,
                measurements=measurements,
                failures=failures,
                qa_level=compact_qa_level,
                progress_callback=output_progress,
                write_full_qa=False,
            )
            if not validate_source_outputs(paths, config=config, source=source, measurements=measurements):
                raise RuntimeError("photometry outputs did not validate")
            if progress_callback is not None:
                progress_callback(
                    {
                        "event": "photometry_output",
                        "source_id": source.get("source_id"),
                        "source_name": source.get("source_name"),
                        "phase": "compact_done",
                        "qa_written": 0,
                        "qa_total": 0,
                    }
                )
            summary.output_paths = {key: str(path) for key, path in paths.items() if key != "qa_dir"}
            summary.output_seconds = time.perf_counter() - output_started
            with _SQLITE_WRITE_LOCK:
                for product_type, key in [
                    ("spectrum_csv", "csv"),
                    ("sed_plot", "sed"),
                    ("qa_summary", "qa"),
                    ("provenance_json", "provenance"),
                    ("measurement_index", "index"),
                    ("output_manifest", "manifest"),
                ]:
                    record_output_product(
                        conn,
                        photometry_run_id=photometry_run_id,
                        source_id=source["source_id"],
                        product_type=product_type,
                        path=paths[key],
                        config=config,
                        commit=False,
                    )
                upsert_source_summary(
                    conn,
                    photometry_run_id=photometry_run_id,
                    source_id=source["source_id"],
                    status="source_partial_success" if summary.failed else "source_complete",
                    n_planned=summary.planned,
                    n_measured=summary.measured,
                    n_failed=summary.failed,
                    n_science_recommended=summary.science_recommended,
                    paths=paths,
                    config=config,
                    summary=asdict(summary),
                )
                conn.commit()
            if effective_qa_level == "full":
                _verbose(console, verbose, f"Writing full measurement QA for {_source_label(source)}: measurements={len(measurements)}")
                qa_started = time.perf_counter()
                write_full_measurement_qa_outputs(
                    config=config,
                    source=source,
                    measurements=measurements,
                    progress_callback=output_progress,
                )
                summary.qa_seconds += time.perf_counter() - qa_started
                summary.qa_plots_planned += len(measurements)
                summary.qa_plots_written += _record_full_qa_products(conn, photometry_run_id, config, source, measurements)
            summary.output_seconds = time.perf_counter() - output_started
            if effective_qa_level == "full":
                with _SQLITE_WRITE_LOCK:
                    upsert_source_summary(
                        conn,
                        photometry_run_id=photometry_run_id,
                        source_id=source["source_id"],
                        status="source_partial_success" if summary.failed else "source_complete",
                        n_planned=summary.planned,
                        n_measured=summary.measured,
                        n_failed=summary.failed,
                        n_science_recommended=summary.science_recommended,
                        paths=paths,
                        config=config,
                        summary=asdict(summary),
                    )
            _verbose(
                console,
                verbose,
                f"Outputs for {_source_label(source)}: {_output_paths_line(paths, config)} "
                f"output_sec={summary.output_seconds:.2f}",
            )
            if config.photometry.cleanup.delete_successful_cutouts:
                with _SQLITE_WRITE_LOCK:
                    summary.deleted_cutouts += _cleanup_successful_cutouts(conn, config, measurements)
                _verbose(console, verbose, f"Cleanup for {_source_label(source)}: deleted_cutouts={summary.deleted_cutouts}")
        else:
            _verbose(console, verbose, f"No durable outputs written for {_source_label(source)}: measurements=0 failures={len(failures)}")
        summary.elapsed_seconds = time.perf_counter() - source_started
        if own_run:
            finish_photometry_run(conn, photometry_run_id, "partial_success" if summary.failed else "success", asdict(summary))
    except Exception:
        if own_run:
            finish_photometry_run(conn, photometry_run_id, "failed", asdict(summary))
        raise
    return summary


def run_photometry(
    conn,
    config: Config,
    *,
    source_ids: list[str] | None = None,
    limit_sources: int | None = None,
    run_id: str | None = None,
    qa_level: str | None = None,
    progress: bool = True,
    verbose: bool = False,
    console=None,
    max_source_workers: int | None = None,
    force_remeasure: bool = False,
    progress_callback: ProgressCallback | None = None,
    worker_backend: str = "process",
) -> PhotometrySummary:
    photometry_run_id = start_photometry_run(conn, config, run_id)
    total = PhotometrySummary()
    try:
        sources = active_sources(conn, source_ids=source_ids, limit=limit_sources)
        _verbose(
            console,
            verbose,
            f"Photometry run: sources={len(sources)} "
            f"qa_level={qa_level or config.photometry.qa_level} "
            f"cleanup_successful_cutouts={config.photometry.cleanup.delete_successful_cutouts}",
        )
        worker_backend = (worker_backend or "process").lower()
        if worker_backend not in {"process", "thread"}:
            raise ValueError("worker_backend must be one of process or thread")
        workers, worker_reason = _resolve_source_workers(
            config,
            requested_workers=max_source_workers,
            qa_level=qa_level,
            source_count=len(sources),
            cap_by_source_count=worker_backend != "process",
        )
        inflight_limit = _process_inflight_limit(config, workers)
        process_unavailable_reason = None
        if workers > 1 and worker_backend == "process":
            process_unavailable_reason = _process_pool_unavailable_reason()
            if process_unavailable_reason:
                worker_backend = "thread"
        display_backend = "serial" if workers <= 1 else worker_backend
        worker_line = (
            f"Photometry run workers: effective={workers} configured={config.runtime.max_source_workers} "
            f"requested={max_source_workers if max_source_workers is not None else '-'} "
            f"backend={display_backend} "
            f"process_workers={workers if display_backend == 'process' else 0} "
            f"inflight_limit={inflight_limit if display_backend == 'process' else '-'} "
            f"qa_workers={config.photometry.qa.full_plot_workers}"
        )
        if worker_reason:
            worker_line += f" {worker_reason}"
        if process_unavailable_reason:
            worker_line += f" process_backend_unavailable={process_unavailable_reason}; falling_back_to=thread"
        _verbose(console, verbose, worker_line)
        if workers > 1 and worker_backend == "process":
            _run_photometry_process_backend(
                conn,
                config,
                photometry_run_id=photometry_run_id,
                run_id=run_id,
                sources=sources,
                total=total,
                workers=workers,
                inflight_limit=inflight_limit,
                qa_level=qa_level,
                progress=progress,
                verbose=verbose,
                console=console,
                force_remeasure=force_remeasure,
                progress_callback=progress_callback,
            )
            finish_photometry_run(conn, photometry_run_id, "partial_success" if total.failed else "success", asdict(total))
            return total
        show_progress = bool(progress and console is not None and sources)
        event_queue: Queue | None = Queue() if show_progress and workers > 1 and len(sources) > 1 else None
        source_progress: dict[str, PhotometrySummary] = {}
        completed_source_ids: set[str] = set()
        completed_sources = 0
        total.worker_backend = "thread" if workers > 1 else "serial"
        if sources:
            _verbose(console, verbose, f"Photometry sources: total={len(sources)} workers={workers}")

        def emit_run_event(event: dict[str, Any]) -> None:
            if progress_callback is not None:
                progress_callback(event)
            if event_queue is not None:
                event_queue.put(event)

        def run_one_source(source: dict[str, Any]) -> PhotometrySummary:
            return run_source_photometry(
                conn,
                config,
                source_id=source["source_id"],
                run_id=run_id,
                photometry_run_id=photometry_run_id,
                qa_level=qa_level,
                progress=False,
                verbose=verbose,
                console=console,
                progress_callback=emit_run_event if event_queue is not None or progress_callback is not None else None,
                force_remeasure=force_remeasure,
            )

        def run_one_source_parallel(source: dict[str, Any]) -> PhotometrySummary:
            worker_conn = connect(config.project.database_path)
            try:
                return run_source_photometry(
                    worker_conn,
                    config,
                    source_id=source["source_id"],
                    run_id=run_id,
                    photometry_run_id=photometry_run_id,
                    qa_level=qa_level,
                    progress=False,
                    verbose=False,
                    console=None,
                    progress_callback=emit_run_event if event_queue is not None or progress_callback is not None else None,
                    force_remeasure=force_remeasure,
                )
            finally:
                worker_conn.close()

        if workers > 1 and len(sources) > 1:
            if show_progress:
                with _run_progress(console) as bar:
                    task_id = bar.add_task(
                        "Photometry sources",
                        total=len(sources),
                        sources=f"0/{len(sources)}",
                        planned=0,
                        measured=0,
                        skipped=0,
                        failed=0,
                        sci=0,
                        downloaded=0,
                        deleted=0,
                        items="0/?",
                        phase="-",
                        qa="-",
                        last="-",
                    )
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        futures = {executor.submit(run_one_source_parallel, source): source for source in sources}
                        pending = set(futures)
                        while pending:
                            done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                            if event_queue is not None:
                                _drain_run_event_queue(
                                    event_queue,
                                    bar,
                                    task_id,
                                    total,
                                    source_progress,
                                    completed_source_ids,
                                    completed_sources,
                                    len(sources),
                                )
                            for future in done:
                                source = futures[future]
                                source_summary = future.result()
                                source_id = str(source["source_id"])
                                completed_source_ids.add(source_id)
                                source_progress.pop(source_id, None)
                                _add_summary(total, source_summary)
                                _verbose(console, verbose, _source_timing_line(source, source_summary))
                                if progress_callback is not None:
                                    progress_callback(
                                        {
                                            "event": "photometry_source_done",
                                            "source_id": source.get("source_id"),
                                            "source_name": source.get("source_name"),
                                            "summary": asdict(source_summary),
                                        }
                                    )
                                completed_sources += 1
                                _update_run_progress(bar, task_id, total, completed_sources, len(sources), _source_label(source))
                                bar.update(task_id, advance=1)
                                bar.refresh()
                        if event_queue is not None:
                            _drain_run_event_queue(
                                event_queue,
                                bar,
                                task_id,
                                total,
                                source_progress,
                                completed_source_ids,
                                completed_sources,
                                len(sources),
                            )
            else:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {executor.submit(run_one_source_parallel, source): source for source in sources}
                    for future in as_completed(futures):
                        source = futures[future]
                        source_summary = future.result()
                        _add_summary(total, source_summary)
                        _verbose(console, verbose, _source_timing_line(source, source_summary))
                        if progress_callback is not None:
                            progress_callback(
                                {
                                    "event": "photometry_source_done",
                                    "source_id": source.get("source_id"),
                                    "source_name": source.get("source_name"),
                                    "summary": asdict(source_summary),
                                }
                            )
        elif show_progress:
            with _run_progress(console) as bar:
                task_id = bar.add_task(
                    "Photometry sources",
                    total=len(sources),
                    sources=f"0/{len(sources)}",
                    planned=0,
                    measured=0,
                    skipped=0,
                    failed=0,
                    sci=0,
                    downloaded=0,
                    deleted=0,
                    items="0/?",
                    phase="-",
                    qa="-",
                    last="-",
                )
                for source in sources:
                    source_summary = run_one_source(source)
                    _add_summary(total, source_summary)
                    if progress_callback is not None:
                        progress_callback(
                            {
                                "event": "photometry_source_done",
                                "source_id": source.get("source_id"),
                                "source_name": source.get("source_name"),
                                "summary": asdict(source_summary),
                            }
                        )
                    completed_sources += 1
                    _update_run_progress(bar, task_id, total, completed_sources, len(sources), _source_label(source))
                    bar.update(task_id, advance=1)
                    bar.refresh()
        else:
            for source in sources:
                source_summary = run_one_source(source)
                _add_summary(total, source_summary)
                if progress_callback is not None:
                    progress_callback(
                        {
                            "event": "photometry_source_done",
                            "source_id": source.get("source_id"),
                            "source_name": source.get("source_name"),
                            "summary": asdict(source_summary),
                        }
                    )
        finish_photometry_run(conn, photometry_run_id, "partial_success" if total.failed else "success", asdict(total))
    except Exception:
        finish_photometry_run(conn, photometry_run_id, "failed", asdict(total))
        raise
    return total


def _run_photometry_process_backend(
    conn,
    config: Config,
    *,
    photometry_run_id: str,
    run_id: str | None,
    sources: list[dict[str, Any]],
    total: PhotometrySummary,
    workers: int,
    inflight_limit: int,
    qa_level: str | None,
    progress: bool,
    verbose: bool,
    console,
    force_remeasure: bool,
    progress_callback: ProgressCallback | None,
) -> None:
    del run_id
    started = time.perf_counter()
    effective_qa_level = qa_level or config.photometry.qa_level
    source_states = {str(source["source_id"]): _ProcessSourceState(source=source) for source in sources}
    completed_sources = 0
    worker_pids: set[int] = set()
    full_qa_pending: list[_ProcessSourceState] = []
    show_progress = bool(progress and console is not None and sources)
    bar: Progress | None = None
    task_id: int | None = None

    def emit_event(event: dict[str, Any]) -> None:
        if progress_callback is not None:
            progress_callback(event)
        if bar is None or task_id is None:
            return
        if event.get("event") == "photometry_output":
            total_qa = int(event.get("qa_total") or 0)
            written = int(event.get("qa_written") or 0)
            qa = f"{written}/{total_qa}" if total_qa else "-"
            phase = str(event.get("phase") or "output")
            source_label = event.get("source_name") or event.get("source_id") or "-"
            last = f"{source_label}:QA"
        else:
            qa = "-"
            phase = str(event.get("phase") or event.get("action") or event.get("state") or "measure")
            source_label = event.get("source_name") or event.get("source_id") or "-"
            last_value = event.get("last") or event.get("cutout_key") or "-"
            last = f"{source_label}:{last_value}"
        bar.update(
            task_id,
            sources=f"{completed_sources}/{len(sources)}",
            planned=total.planned,
            measured=total.measured,
            skipped=total.skipped,
            failed=total.failed,
            sci=total.science_recommended,
            downloaded=total.downloaded,
            deleted=total.deleted_cutouts,
            items=_item_progress_text(total),
            phase=phase,
            qa=qa,
            last=last,
        )
        bar.refresh()

    def increment(summary: PhotometrySummary, field_name: str, amount: int | float = 1) -> None:
        setattr(summary, field_name, getattr(summary, field_name) + amount)
        setattr(total, field_name, getattr(total, field_name) + amount)

    def item_done(
        item: dict[str, Any],
        *,
        action: str,
        state: str,
        index: int,
        worker_pid: int | None = None,
    ) -> None:
        source_id = str(item["source"]["source_id"])
        state_obj = source_states[source_id]
        state_obj.terminal += 1
        event = _progress_event(
            state_obj.source,
            item,
            state_obj.summary,
            action=action,
            state=state,
            index=index,
            downloaded_delta=0,
        )
        event["phase"] = action
        if worker_pid is not None:
            event["worker_pid"] = worker_pid
        emit_event(event)
        maybe_finalize_source(state_obj)

    def maybe_cleanup_source(state_obj: _ProcessSourceState) -> None:
        if not config.photometry.cleanup.delete_successful_cutouts or not state_obj.measurements:
            return
        with _SQLITE_WRITE_LOCK:
            removed = _cleanup_successful_cutouts(conn, config, state_obj.measurements)
        state_obj.summary.deleted_cutouts += removed
        total.deleted_cutouts += removed
        _verbose(console, verbose, f"Cleanup for {_source_label(state_obj.source)}: deleted_cutouts={removed}")

    def output_progress(source: dict[str, Any], event: dict[str, Any]) -> None:
        emit_event(
            {
                "event": "photometry_output",
                "source_id": source.get("source_id"),
                "source_name": source.get("source_name"),
                "phase": event.get("phase"),
                "qa_written": int(event.get("qa_written") or 0),
                "qa_total": int(event.get("qa_total") or 0),
            }
        )

    def record_source_summary(state_obj: _ProcessSourceState, status: str) -> None:
        if not state_obj.output_paths:
            return
        with _SQLITE_WRITE_LOCK:
            upsert_source_summary(
                conn,
                photometry_run_id=photometry_run_id,
                source_id=state_obj.source["source_id"],
                status=status,
                n_planned=state_obj.summary.planned,
                n_measured=state_obj.summary.measured,
                n_failed=state_obj.summary.failed,
                n_science_recommended=state_obj.summary.science_recommended,
                paths=state_obj.output_paths,
                config=config,
                summary=asdict(state_obj.summary),
            )

    def maybe_finalize_source(state_obj: _ProcessSourceState) -> None:
        nonlocal completed_sources
        if state_obj.finalized or state_obj.terminal < state_obj.summary.planned:
            return
        state_obj.finalized = True
        output_started = time.perf_counter()
        output_measurements = _load_measurements_for_source(conn, state_obj.source["source_id"])
        if output_measurements:
            compact_qa_level = "standard" if effective_qa_level == "full" else effective_qa_level
            _verbose(
                console,
                verbose,
                f"Writing photometry outputs for {_source_label(state_obj.source)}: "
                f"measurements={len(output_measurements)} failures={len(state_obj.failures)}",
            )
            emit_event(
                {
                    "event": "photometry_output",
                    "source_id": state_obj.source.get("source_id"),
                    "source_name": state_obj.source.get("source_name"),
                    "phase": "compact_start",
                    "qa_written": 0,
                    "qa_total": 0,
                }
            )
            paths = write_source_outputs(
                config=config,
                source=state_obj.source,
                measurements=output_measurements,
                failures=state_obj.failures,
                qa_level=compact_qa_level,
                progress_callback=lambda event: output_progress(state_obj.source, event),
                write_full_qa=False,
            )
            if not validate_source_outputs(paths, config=config, source=state_obj.source, measurements=output_measurements):
                raise RuntimeError("photometry outputs did not validate")
            state_obj.output_paths = paths
            state_obj.summary.output_paths = {key: str(path) for key, path in paths.items() if key != "qa_dir"}
            with _SQLITE_WRITE_LOCK:
                for product_type, key in [
                    ("spectrum_csv", "csv"),
                    ("sed_plot", "sed"),
                    ("qa_summary", "qa"),
                    ("provenance_json", "provenance"),
                    ("measurement_index", "index"),
                    ("output_manifest", "manifest"),
                ]:
                    record_output_product(
                        conn,
                        photometry_run_id=photometry_run_id,
                        source_id=state_obj.source["source_id"],
                        product_type=product_type,
                        path=paths[key],
                        config=config,
                        commit=False,
                    )
                conn.commit()
            emit_event(
                {
                    "event": "photometry_output",
                    "source_id": state_obj.source.get("source_id"),
                    "source_name": state_obj.source.get("source_name"),
                    "phase": "compact_done",
                    "qa_written": 0,
                    "qa_total": 0,
                }
            )
            if effective_qa_level == "full" and state_obj.measurements:
                full_qa_pending.append(state_obj)
            else:
                maybe_cleanup_source(state_obj)
            state_obj.summary.output_seconds += time.perf_counter() - output_started
            total.output_seconds += state_obj.summary.output_seconds
            status = "source_partial_success" if state_obj.summary.failed else "source_complete"
            record_source_summary(state_obj, status)
            _verbose(
                console,
                verbose,
                f"Outputs for {_source_label(state_obj.source)}: {_output_paths_line(paths, config)} "
                f"output_sec={state_obj.summary.output_seconds:.2f}",
            )
        else:
            _verbose(console, verbose, f"No durable outputs written for {_source_label(state_obj.source)}: measurements=0 failures={len(state_obj.failures)}")
        state_obj.summary.elapsed_seconds = time.perf_counter() - state_obj.started_at
        completed_sources += 1
        if progress_callback is not None:
            progress_callback(
                {
                    "event": "photometry_source_done",
                    "source_id": state_obj.source.get("source_id"),
                    "source_name": state_obj.source.get("source_name"),
                    "summary": asdict(state_obj.summary),
                }
            )
        if bar is not None and task_id is not None:
            _update_run_progress(bar, task_id, total, completed_sources, len(sources), _source_label(state_obj.source))
            bar.update(task_id, advance=1)
            bar.refresh()
        _verbose(console, verbose, _source_timing_line(state_obj.source, state_obj.summary))

    def process_non_measure_item(item: dict[str, Any], index: int) -> None:
        source_id = str(item["source"]["source_id"])
        state_obj = source_states[source_id]
        state = item["state"]
        if state == "photometry_valid":
            increment(state_obj.summary, "skipped")
            item_done(item, action="skipped", state=state, index=index)
            return
        reason = item["reason"] or f"blocked state: {state}"
        failure_type = "calibration" if state == "calibration_missing" else "input" if state == "cutout_missing_or_invalid" else "validation"
        _record_item_failure(conn, photometry_run_id, item, failure_type, reason)
        state_obj.failures.append({"work_item_id": item["work_item_id"], "failure_type": failure_type, "reason": reason})
        increment(state_obj.summary, "failed")
        item_done(item, action="failed", state=state, index=index)

    def submit_measurement(pool: ProcessPoolExecutor, item: dict[str, Any], index: int):
        cutout = latest_cutout_for_key(conn, item["plan_row"]["cutout_key"])
        if cutout is None:
            reason = "cutout record is missing"
            mark_work_item_state(conn, item["work_item_id"], "failed_validation", reason)
            _record_item_failure(conn, photometry_run_id, item, "validation", reason)
            state_obj = source_states[str(item["source"]["source_id"])]
            state_obj.failures.append({"work_item_id": item["work_item_id"], "failure_type": "validation", "reason": reason})
            increment(state_obj.summary, "failed")
            item_done(item, action="failed", state="failed_validation", index=index)
            return None
        item["cutout"] = cutout
        mark_work_item_state(conn, item["work_item_id"], "measuring", None)
        payload = _process_measurement_payload(config, item, cutout)
        return pool.submit(_measure_item_process_worker, payload)

    def handle_measurement_result(item: dict[str, Any], index: int, submitted_at: float, future) -> None:
        source_id = str(item["source"]["source_id"])
        state_obj = source_states[source_id]
        try:
            payload = future.result()
            measurement = payload["measurement"]
            worker_pid = int(payload.get("worker_pid") or 0)
            measure_seconds = float(payload.get("measure_seconds") or 0.0)
        except Exception as exc:  # noqa: BLE001 - per-measurement failure record
            reason = str(exc)
            mark_work_item_state(conn, item["work_item_id"], "failed_fit", reason)
            _record_item_failure(conn, photometry_run_id, item, "fit", reason, exc.__class__.__name__)
            state_obj.failures.append({"work_item_id": item["work_item_id"], "failure_type": "fit", "reason": reason})
            elapsed = time.perf_counter() - submitted_at
            state_obj.summary.measure_seconds += elapsed
            total.measure_seconds += elapsed
            increment(state_obj.summary, "failed")
            _verbose(console, verbose, f"  failed_fit {_cutout_label(item)}: {exc.__class__.__name__}: {exc}")
            item_done(item, action="failed", state="failed_fit", index=index)
            return
        record_measurement(
            conn,
            photometry_run_id=photometry_run_id,
            work_item_id=item["work_item_id"],
            cutout_id=(item.get("cutout") or {}).get("cutout_id"),
            result=measurement,
            config=config,
        )
        state_obj.measurements.append(measurement)
        state_obj.summary.measure_seconds += measure_seconds
        total.measure_seconds += measure_seconds
        increment(state_obj.summary, "measured")
        if measurement.row.get("science_recommended"):
            increment(state_obj.summary, "science_recommended")
        if worker_pid:
            worker_pids.add(worker_pid)
            total.worker_pid_count = len(worker_pids)
        _verbose(console, verbose, f"{_measurement_line(measurement)} worker_pid={worker_pid or '-'}")
        item_done(item, action="measured", state="measured", index=index, worker_pid=worker_pid or None)

    def run_core() -> None:
        plan_started = time.perf_counter()
        source_ids = [str(source["source_id"]) for source in sources]
        with _SQLITE_WRITE_LOCK:
            items, counts = build_photometry_plan(
                conn,
                config,
                photometry_run_id=photometry_run_id,
                source_ids=source_ids,
                force_remeasure=force_remeasure,
                commit=False,
            )
        total.plan_seconds = time.perf_counter() - plan_started
        total.planned = len(items)
        total.states = counts
        total.worker_backend = "process"
        _verbose(
            console,
            verbose,
            f"Photometry process plan: planned={len(items)} states={_format_counts(counts)} "
            f"plan_sec={total.plan_seconds:.2f} process_workers={workers} inflight_limit={inflight_limit}",
        )
        for item in items:
            source_states[str(item["source"]["source_id"])].summary.planned += 1
        indexed_items = list(enumerate(items, start=1))
        measure_items: list[tuple[int, dict[str, Any]]] = []
        measure_indexes: set[int] = set()
        for index, item in indexed_items:
            if item["state"] == "cutout_valid_measurement_missing":
                measure_items.append((index, item))
                measure_indexes.add(index)
                continue
            if (
                effective_qa_level == "full"
                and item["state"] == "photometry_valid"
                and not validate_full_qa_measurement(
                    config=config,
                    source=item["source"],
                    measurement_id=item["measurement_id"],
                )
            ):
                if _cutout_file_exists_for_item(config, item):
                    _verbose(
                        console,
                        verbose,
                        f"  remeasuring for missing/stale full QA {_cutout_label(item)}",
                    )
                    measure_items.append((index, item))
                    measure_indexes.add(index)
                    continue
                _verbose(
                    console,
                    verbose,
                    f"  full QA missing but cutout file is unavailable; keeping valid measurement without redownload {_cutout_label(item)}",
                )
            if index not in measure_indexes:
                process_non_measure_item(item, index)
        for state_obj in source_states.values():
            maybe_finalize_source(state_obj)
        if measure_items:
            _set_parallel_worker_env_defaults()
            pending: dict[Any, tuple[dict[str, Any], int, float]] = {}
            cursor = 0
            with ProcessPoolExecutor(max_workers=workers) as pool:
                while cursor < len(measure_items) or pending:
                    while cursor < len(measure_items) and len(pending) < inflight_limit:
                        index, item = measure_items[cursor]
                        cursor += 1
                        future = submit_measurement(pool, item, index)
                        if future is not None:
                            pending[future] = (item, index, time.perf_counter())
                    if not pending:
                        continue
                    done, _ = wait(set(pending), timeout=0.25, return_when=FIRST_COMPLETED)
                    if not done:
                        continue
                    for future in done:
                        item, index, submitted_at = pending.pop(future)
                        handle_measurement_result(item, index, submitted_at, future)
        for state_obj in source_states.values():
            maybe_finalize_source(state_obj)
        if full_qa_pending:
            qa_started = time.perf_counter()
            qa_total = sum(len(state_obj.measurements) for state_obj in full_qa_pending)
            total.qa_plots_planned += qa_total
            _verbose(console, verbose, f"Writing full measurement QA: sources={len(full_qa_pending)} measurements={qa_total}")
            write_full_measurement_qa_batch(
                config=config,
                source_measurements=[(state_obj.source, state_obj.measurements) for state_obj in full_qa_pending],
                progress_callback=lambda event: emit_event(
                    {
                        "event": "photometry_output",
                        "phase": event.get("phase"),
                        "qa_written": int(event.get("qa_written") or 0),
                        "qa_total": int(event.get("qa_total") or 0),
                    }
                ),
            )
            qa_seconds = time.perf_counter() - qa_started
            total.qa_seconds += qa_seconds
            total.output_seconds += qa_seconds
            for state_obj in full_qa_pending:
                output_measurements = _load_measurements_for_source(conn, state_obj.source["source_id"])
                if output_measurements:
                    write_full_qa_manifest(config=config, source=state_obj.source, measurements=output_measurements)
                written = _record_full_qa_products(conn, photometry_run_id, config, state_obj.source, output_measurements)
                state_obj.summary.qa_plots_planned += len(state_obj.measurements)
                state_obj.summary.qa_plots_written += written
                state_obj.summary.qa_seconds += qa_seconds
                total.qa_plots_written += written
                if output_measurements and not validate_full_qa_outputs(config=config, source=state_obj.source, measurements=output_measurements):
                    missing = max(0, len(output_measurements) - written)
                    state_obj.summary.qa_plots_failed += missing
                    total.qa_plots_failed += missing
                maybe_cleanup_source(state_obj)
                status = "source_partial_success" if state_obj.summary.failed else "source_complete"
                record_source_summary(state_obj, status)
        total.elapsed_seconds = time.perf_counter() - started
        total.worker_pid_count = len(worker_pids)

    if show_progress:
        with _run_progress(console) as progress_bar:
            bar = progress_bar
            task_id = bar.add_task(
                "Photometry sources",
                total=len(sources),
                sources=f"0/{len(sources)}",
                planned=0,
                measured=0,
                skipped=0,
                failed=0,
                sci=0,
                downloaded=0,
                deleted=0,
                items="0/?",
                phase="-",
                qa="-",
                last="-",
            )
            run_core()
    else:
        run_core()


def _process_measurement_payload(config: Config, item: dict[str, Any], cutout: dict[str, Any]) -> dict[str, Any]:
    return {
        "config": config.model_dump(mode="json"),
        "source": dict(item["source"]),
        "cutout": dict(cutout),
        "calibration": item["calibration"],
        "measurement_id": item["measurement_id"],
        "work_item_id": item["work_item_id"],
    }


def _measure_item_process_worker(payload: dict[str, Any]) -> dict[str, Any]:
    _set_parallel_worker_env_defaults()
    started = time.perf_counter()
    config = Config.model_validate(payload["config"])
    cutout = payload["cutout"]
    measurement = measure_cutout(
        cutout_path=resolve_project_path(config, cutout["local_path"]),
        source=payload["source"],
        cutout_row=cutout,
        calibration_resolution=payload["calibration"],
        config=config,
        measurement_id=payload["measurement_id"],
        work_item_id=payload["work_item_id"],
    )
    return {
        "measurement": measurement,
        "worker_pid": os.getpid(),
        "measure_seconds": time.perf_counter() - started,
    }


def _set_parallel_worker_env_defaults() -> None:
    for name in ["OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"]:
        os.environ.setdefault(name, "1")


def _process_pool_ping() -> int:
    return os.getpid()


def _process_pool_unavailable_reason() -> str | None:
    try:
        _set_parallel_worker_env_defaults()
        with ProcessPoolExecutor(max_workers=1) as pool:
            worker_pid = int(pool.submit(_process_pool_ping).result(timeout=10))
        if worker_pid == os.getpid():
            return "worker_pid_matches_parent"
    except Exception as exc:  # noqa: BLE001 - environment capability probe
        return f"{exc.__class__.__name__}: {exc}"
    return None


def _process_inflight_limit(config: Config, workers: int) -> int:
    configured = max(1, int(config.runtime.max_inflight_cutouts))
    return max(1, min(configured, max(1, int(workers)) * 2))


def _add_summary(total: PhotometrySummary, item: PhotometrySummary) -> None:
    total.planned += item.planned
    total.skipped += item.skipped
    total.measured += item.measured
    total.failed += item.failed
    total.science_recommended += item.science_recommended
    total.downloaded += item.downloaded
    total.deleted_cutouts += item.deleted_cutouts
    total.plan_seconds += item.plan_seconds
    total.measure_seconds += item.measure_seconds
    total.output_seconds += item.output_seconds
    total.qa_seconds += item.qa_seconds
    total.qa_plots_planned += item.qa_plots_planned
    total.qa_plots_written += item.qa_plots_written
    total.qa_plots_failed += item.qa_plots_failed
    total.elapsed_seconds += item.elapsed_seconds
    total.states = dict(Counter(total.states) + Counter(item.states))
    if item.worker_backend:
        total.worker_backend = item.worker_backend
    total.worker_pid_count = max(total.worker_pid_count, item.worker_pid_count)


def _resolve_source_workers(
    config: Config,
    *,
    requested_workers: int | None,
    qa_level: str | None,
    source_count: int,
    cap_by_source_count: bool = True,
) -> tuple[int, str | None]:
    configured = max(1, int(requested_workers if requested_workers is not None else config.runtime.max_source_workers))
    source_cap = max(1, int(source_count or 1))
    effective_qa = qa_level or config.photometry.qa_level
    caps = [max(1, int(config.runtime.max_open_fits_files)), max(1, int(config.runtime.global_max_open_fits_files))]
    if cap_by_source_count:
        caps.append(source_cap)
    reason: str | None = None
    if requested_workers is None:
        auto_cap = _FULL_QA_AUTO_SOURCE_WORKER_CAP if effective_qa == "full" else _AUTO_SOURCE_WORKER_CAP
        caps.extend([auto_cap, max(1, int(config.runtime.max_fit_workers))])
        cap = min(caps)
        if configured > cap:
            reason = (
                f"auto_capped_from={configured} auto_cap={cap} "
                f"qa_level={effective_qa}; pass --max-source-workers to override auto source cap"
            )
    else:
        cap = min(caps)
        if configured > cap:
            reason = f"capped_from={configured} cap={cap} reason=open_fits_or_source_limit"
    return max(1, min(configured, cap)), reason


def _copy_summary(summary: PhotometrySummary) -> PhotometrySummary:
    return _summary_from_mapping(asdict(summary))


def _summary_from_mapping(data: dict[str, Any]) -> PhotometrySummary:
    return PhotometrySummary(
        planned=int(data.get("planned") or 0),
        skipped=int(data.get("skipped") or 0),
        measured=int(data.get("measured") or 0),
        failed=int(data.get("failed") or 0),
        science_recommended=int(data.get("science_recommended") or 0),
        downloaded=int(data.get("downloaded") or 0),
        deleted_cutouts=int(data.get("deleted_cutouts") or 0),
        plan_seconds=float(data.get("plan_seconds") or 0.0),
        measure_seconds=float(data.get("measure_seconds") or 0.0),
        output_seconds=float(data.get("output_seconds") or 0.0),
        elapsed_seconds=float(data.get("elapsed_seconds") or 0.0),
        states=dict(data.get("states") or {}),
        output_paths=dict(data.get("output_paths") or {}),
    )


def _display_summary(completed: PhotometrySummary, source_progress: dict[str, PhotometrySummary]) -> PhotometrySummary:
    out = _copy_summary(completed)
    for progress in source_progress.values():
        _add_summary(out, progress)
    return out


def _source_item_progress(console) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn(
            "measured={task.fields[measured]} skip={task.fields[skipped]} "
            "fail={task.fields[failed]} sci={task.fields[sci]} "
            "last={task.fields[last]}"
        ),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )


def _run_progress(console) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn(
            "sources={task.fields[sources]} planned={task.fields[planned]} "
            "items={task.fields[items]} "
            "measured={task.fields[measured]} skip={task.fields[skipped]} "
            "fail={task.fields[failed]} sci={task.fields[sci]} "
            "del={task.fields[deleted]} phase={task.fields[phase]} qa={task.fields[qa]} "
            "last={task.fields[last]}"
        ),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )


def _update_source_item_progress(bar: Progress, task_id: int, summary: PhotometrySummary, last: str) -> None:
    bar.update(
        task_id,
        measured=summary.measured,
        skipped=summary.skipped,
        failed=summary.failed,
        sci=summary.science_recommended,
        downloaded=summary.downloaded,
        last=last,
    )


def _update_run_progress(
    bar: Progress,
    task_id: int,
    summary: PhotometrySummary,
    completed_sources: int,
    total_sources: int,
    last: str,
) -> None:
    bar.update(
        task_id,
        sources=f"{completed_sources}/{total_sources}",
        planned=summary.planned,
        measured=summary.measured,
        skipped=summary.skipped,
        failed=summary.failed,
        sci=summary.science_recommended,
        downloaded=summary.downloaded,
        deleted=summary.deleted_cutouts,
        items=_item_progress_text(summary),
        phase="done",
        qa="-",
        last=last,
    )


def _item_progress_text(summary: PhotometrySummary) -> str:
    done = summary.measured + summary.skipped + summary.failed
    planned = summary.planned
    return f"{done}/{planned}" if planned else f"{done}/?"


def _drain_run_event_queue(
    event_queue: Queue,
    bar: Progress,
    task_id: int,
    completed_summary: PhotometrySummary,
    source_progress: dict[str, PhotometrySummary],
    completed_source_ids: set[str],
    completed_sources: int,
    total_sources: int,
) -> None:
    latest: dict[str, Any] | None = None
    while True:
        try:
            event = event_queue.get_nowait()
        except Empty:
            break
        latest = event
        if event.get("event") == "photometry_item" and isinstance(event.get("summary"), dict):
            source_id = str(event.get("source_id") or "")
            if source_id and source_id not in completed_source_ids:
                source_progress[source_id] = _summary_from_mapping(event["summary"])
    if latest is None:
        return
    source_label = latest.get("source_name") or latest.get("source_id") or "-"
    event = latest.get("event")
    if event == "photometry_output":
        total = int(latest.get("qa_total") or 0)
        written = int(latest.get("qa_written") or 0)
        qa = f"{written}/{total}" if total else "-"
        phase = str(latest.get("phase") or "output")
        last = f"{source_label}:QA"
    else:
        phase = str(latest.get("action") or latest.get("state") or "measure")
        qa = "-"
        last_value = latest.get("last") or latest.get("cutout_key") or "-"
        last = f"{source_label}:{last_value}"
    display = _display_summary(completed_summary, source_progress)
    bar.update(
        task_id,
        sources=f"{completed_sources}/{total_sources}",
        planned=display.planned,
        measured=display.measured,
        skipped=display.skipped,
        failed=display.failed,
        sci=display.science_recommended,
        downloaded=display.downloaded,
        deleted=display.deleted_cutouts,
        items=_item_progress_text(display),
        phase=phase,
        qa=qa,
        last=last,
    )
    bar.refresh()


def _verbose(console, verbose: bool, message: str) -> None:
    if verbose and console is not None:
        console.print(message)


def _source_label(source: dict[str, Any]) -> str:
    name = source.get("source_name")
    source_id = source.get("source_id")
    if name and name != source_id:
        return f"{source_id} ({name})"
    return str(source_id)


def _cutout_label(item: dict[str, Any]) -> str:
    plan_row = item.get("plan_row", {})
    parts = [
        f"cutout={plan_row.get('cutout_key')}",
        f"detector={plan_row.get('detector_id')}",
    ]
    collection = plan_row.get("collection")
    if collection:
        parts.append(f"collection={collection}")
    product_id = plan_row.get("product_id")
    if product_id:
        parts.append(f"product={product_id}")
    return " ".join(parts)


def _item_last(item: dict[str, Any], *, index: int) -> str:
    key = item.get("plan_row", {}).get("cutout_key")
    if key:
        text = str(key)
        if len(text) > 32:
            text = text[:29] + "..."
        return f"{index}:{text}"
    return str(index)


def _progress_event(
    source: dict[str, Any],
    item: dict[str, Any],
    summary: PhotometrySummary,
    *,
    action: str,
    state: str,
    index: int,
    downloaded_delta: int,
) -> dict[str, Any]:
    return {
        "event": "photometry_item",
        "source_id": source.get("source_id"),
        "source_name": source.get("source_name"),
        "cutout_key": item.get("plan_row", {}).get("cutout_key"),
        "state": state,
        "action": action,
        "index": index,
        "downloaded_delta": downloaded_delta,
        "last": _item_last(item, index=index),
        "summary": asdict(summary),
    }


def _measurement_line(measurement: MeasurementResult) -> str:
    row = measurement.row
    flags = row.get("photometry_flags") or "-"
    return (
        f"  measured cutout={row.get('cutout_key')} detector={row.get('detector_id')} "
        f"wave_um={_fmt(row.get('wavelength_um'))} "
        f"flux={_fmt(row.get('selected_flux_uJy'))}+/-{_fmt(row.get('selected_flux_err_uJy'))}uJy "
        f"snr={_fmt(row.get('selected_snr'))} detection={row.get('detection_status')} "
        f"science={bool(row.get('science_recommended'))} mode={row.get('science_mode')} "
        f"flags={flags}"
    )


def _source_timing_line(source: dict[str, Any], summary: PhotometrySummary) -> str:
    return (
        f"Finished {_source_label(source)}: planned={summary.planned} measured={summary.measured} "
        f"skipped={summary.skipped} failed={summary.failed} "
        f"plan_sec={summary.plan_seconds:.2f} measure_sec={summary.measure_seconds:.2f} "
        f"output_sec={summary.output_seconds:.2f} elapsed_sec={summary.elapsed_seconds:.2f}"
    )


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _fmt(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "nan"
    if pd.isna(numeric):
        return "nan"
    return f"{numeric:.4g}"


def _output_paths_line(paths: dict[str, Path], config: Config) -> str:
    wanted = ["csv", "sed", "qa", "provenance", "index"]
    return " ".join(f"{key}={_rel_or_str(paths[key], config)}" for key in wanted if key in paths)


def _rel_or_str(path: Path, config: Config) -> str:
    try:
        return str(Path(path).resolve().relative_to(config.project.root))
    except ValueError:
        return str(path)


def summarize_photometry(conn, config: Config) -> Path:
    rows = conn.execute(
        """
        SELECT *
        FROM photometry_source_summaries
        ORDER BY source_id
        """
    ).fetchall()
    path = config.photometry.output_root / "summaries" / "photometry_catalog_summary.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([{key: row[key] for key in row.keys()} for row in rows])
    df.to_csv(path, index=False)
    return path


def _measure_item(conn, config: Config, item: dict[str, Any], photometry_run_id: str) -> MeasurementResult:
    cutout = latest_cutout_for_key(conn, item["plan_row"]["cutout_key"])
    if cutout is None:
        raise ValueError("cutout record is missing")
    path = resolve_project_path(config, cutout["local_path"])
    with _SQLITE_WRITE_LOCK:
        mark_work_item_state(conn, item["work_item_id"], "measuring", None, commit=True)
    result = measure_cutout(
        cutout_path=path,
        source=item["source"],
        cutout_row=cutout,
        calibration_resolution=item["calibration"],
        config=config,
        measurement_id=item["measurement_id"],
        work_item_id=item["work_item_id"],
    )
    with _SQLITE_WRITE_LOCK:
        record_measurement(
            conn,
            photometry_run_id=photometry_run_id,
            work_item_id=item["work_item_id"],
            cutout_id=cutout.get("cutout_id"),
            result=result,
            config=config,
        )
    return result


def _cleanup_successful_cutouts(conn, config: Config, measurements: list[MeasurementResult]) -> int:
    removed = 0
    for measurement in measurements:
        status = str(measurement.row.get("measurement_status") or "")
        if status.startswith("failed_") or status == "invalid_fit":
            continue
        cutout_key = measurement.row["cutout_key"]
        cutout = latest_cutout_for_key(conn, cutout_key)
        if cutout is None:
            continue
        path = resolve_project_path(config, cutout["local_path"])
        if path.exists():
            path.unlink()
            removed += 1
        conn.execute("UPDATE cutouts SET file_exists = 0 WHERE cutout_key = ?", (cutout_key,))
    conn.commit()
    return removed


def _record_item_failure(conn, photometry_run_id: str, item: dict[str, Any], failure_type: str, reason: str, exception_class: str | None = None) -> None:
    with _SQLITE_WRITE_LOCK:
        record_photometry_failure(
            conn,
            photometry_run_id=photometry_run_id,
            work_item_id=item.get("work_item_id"),
            source_id=item.get("source", {}).get("source_id"),
            product_id=item.get("plan_row", {}).get("product_id"),
            cutout_id=(item.get("cutout") or {}).get("cutout_id"),
            failure_type=failure_type,
            reason=reason,
            exception_class=exception_class,
        )


def _load_measurements_for_source(conn, source_id: str) -> list[Any]:
    rows = conn.execute(
        """
        SELECT row_json, provenance_json
        FROM photometry_measurements
        WHERE source_id = ?
        ORDER BY wavelength_um, measurement_id
        """,
        (source_id,),
    ).fetchall()
    measurements: list[Any] = []
    for row in rows:
        measurement_row = json.loads(row["row_json"]) if row["row_json"] else {}
        provenance = json.loads(row["provenance_json"]) if row["provenance_json"] else {}
        measurements.append(SimpleNamespace(row=measurement_row, provenance=provenance, qa_arrays={}))
    return measurements


def _record_full_qa_products(conn, photometry_run_id: str, config: Config, source: dict[str, Any], measurements: list) -> int:
    written = 0
    with _SQLITE_WRITE_LOCK:
        for measurement in measurements:
            measurement_id = str(getattr(measurement, "row", {}).get("measurement_id") or "")
            if not measurement_id:
                continue
            path = full_qa_measurement_path(config, source, measurement_id)
            if not path.exists() or path.stat().st_size <= 0:
                continue
            record_output_product(
                conn,
                photometry_run_id=photometry_run_id,
                source_id=source["source_id"],
                measurement_id=measurement_id,
                product_type="measurement_qa_png",
                path=path,
                config=config,
                commit=False,
            )
            written += 1
        conn.commit()
    return written


def _cutout_file_exists_for_item(config: Config, item: dict[str, Any]) -> bool:
    cutout = item.get("cutout")
    if not cutout:
        return False
    local_path = cutout.get("local_path")
    if not local_path:
        return False
    return resolve_project_path(config, local_path).exists()


def _resolve_source(conn, *, source_id: str | None, source_name: str | None) -> dict[str, Any] | None:
    if source_id:
        return source_by_id(conn, source_id)
    if source_name:
        return source_by_name(conn, source_name)
    return None
