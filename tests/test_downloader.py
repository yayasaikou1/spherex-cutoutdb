from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from threading import Lock
from threading import Thread
import time

import pytest
from rich.console import Console

responses = pytest.importorskip("responses")

from conftest import make_synthetic_cutout
from spherex_cutoutdb.catalog import ingest_catalog
from spherex_cutoutdb.config import load_config, write_default_config
from spherex_cutoutdb.database import connect, initialize_schema, table_count, utcnow
from spherex_cutoutdb.downloader import CompletedDownload, download_one, download_plan, group_plan_rows_by_target, iter_download_plan_results


def _config(tmp_path, tiny_catalog_path):
    cfg_path = write_default_config(tmp_path, tiny_catalog_path)
    cfg = load_config(tmp_path, cfg_path)
    cfg.download.retry.attempts = 2
    cfg.download.retry.backoff_seconds = [0, 0]
    cfg.download.retry.jitter_seconds = 0
    return cfg


class _StaticHandler(BaseHTTPRequestHandler):
    routes: dict[str, list[tuple[int, bytes] | dict]] = {}
    request_log: list[dict] = []
    lock = Lock()
    active = 0
    max_active = 0

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        responses_for_path = self.routes.get(self.path)
        if not responses_for_path:
            self.send_response(404)
            self.end_headers()
            return
        raw = responses_for_path.pop(0) if len(responses_for_path) > 1 else responses_for_path[0]
        if isinstance(raw, dict):
            status = int(raw.get("status", 200))
            body = raw.get("body", b"")
            headers = raw.get("headers", {})
            delay = float(raw.get("delay", 0.0))
            chunk_size = raw.get("chunk_size")
            chunk_delay = float(raw.get("chunk_delay", 0.0))
        else:
            status, body = raw
            headers = {}
            delay = 0.0
            chunk_size = None
            chunk_delay = 0.0
        entry = {"path": self.path, "started": time.monotonic(), "ended": None, "status": status}
        with self.lock:
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
            entry["active_at_start"] = type(self).active
            type(self).request_log.append(entry)
        try:
            if delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key, str(value))
            self.end_headers()
            if body and chunk_size:
                for start in range(0, len(body), int(chunk_size)):
                    try:
                        self.wfile.write(body[start:start + int(chunk_size)])
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    if chunk_delay:
                        time.sleep(chunk_delay)
            elif body:
                self.wfile.write(body)
        finally:
            entry["ended"] = time.monotonic()
            with self.lock:
                type(self).active -= 1

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return


@pytest.fixture
def local_http_server():
    _StaticHandler.routes = {}
    _StaticHandler.request_log = []
    _StaticHandler.active = 0
    _StaticHandler.max_active = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StaticHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base_url, _StaticHandler.routes
    finally:
        server.shutdown()
        server.server_close()


def _download_project(tmp_path, tiny_catalog_path):
    cfg = _config(tmp_path, tiny_catalog_path)
    cfg.download.max_workers = 2
    cfg.download.concurrency = 2
    cfg.download.per_host_rate_limit_per_second = 1000
    cfg.download.retry.attempts = 2
    cfg.download.retry.backoff_seconds = [0, 0]
    cfg.download.retry.jitter_seconds = 0
    conn = connect(cfg.project.database_path)
    initialize_schema(conn)
    ingest_catalog(conn, cfg, "catalog_run")
    return cfg, conn


def _insert_plan(
    conn,
    *,
    source_id: str,
    cutout_key: str,
    url: str,
    local_path: str,
    run_id: str = "plan_run",
    priority: int = 1,
    action: str = "download",
):
    source = conn.execute("SELECT ra_deg, dec_deg FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    conn.execute(
        """
        INSERT INTO download_plan(
          run_id, source_id, cutout_key, cutout_ra_deg, cutout_dec_deg,
          cutout_size_arcsec, cutout_size_deg, cutout_url, local_path,
          access_method, action, reason, priority, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            source_id,
            cutout_key,
            float(source["ra_deg"]),
            float(source["dec_deg"]),
            60.0,
            60.0 / 3600.0,
            url,
            local_path,
            "onprem_cutout",
            action,
            "test",
            priority,
            utcnow(),
        ),
    )
    conn.commit()


@responses.activate
def test_download_one_success(tmp_path, tiny_catalog_path):
    cfg = _config(tmp_path, tiny_catalog_path)
    fits_path = make_synthetic_cutout(tmp_path / "source.fits")
    body = fits_path.read_bytes()
    url = "https://example.test/cutout.fits"
    responses.add(responses.GET, url, body=body, status=200)
    result = download_one(
        {
            "plan_id": 1,
            "cutout_key": "abc123",
            "local_path": "data/cutouts/test.fits",
            "cutout_url": url,
        },
        cfg,
    )
    assert result.success
    assert (tmp_path / "data" / "cutouts" / "test.fits").exists()
    assert result.sha256


@responses.activate
def test_download_one_retries_503(tmp_path, tiny_catalog_path):
    cfg = _config(tmp_path, tiny_catalog_path)
    fits_path = make_synthetic_cutout(tmp_path / "source.fits")
    url = "https://example.test/retry.fits"
    responses.add(responses.GET, url, status=503)
    responses.add(responses.GET, url, body=fits_path.read_bytes(), status=200)
    result = download_one(
        {
            "plan_id": 1,
            "cutout_key": "retry123",
            "local_path": "data/cutouts/retry.fits",
            "cutout_url": url,
        },
        cfg,
    )
    assert result.success
    assert result.attempts == 2


@responses.activate
def test_download_one_failure(tmp_path, tiny_catalog_path):
    cfg = _config(tmp_path, tiny_catalog_path)
    url = "https://example.test/fail.fits"
    responses.add(responses.GET, url, status=500)
    responses.add(responses.GET, url, status=500)
    result = download_one(
        {
            "plan_id": 1,
            "cutout_key": "fail123",
            "local_path": "data/cutouts/fail.fits",
            "cutout_url": url,
        },
        cfg,
    )
    assert not result.success
    assert "HTTP 500" in result.reason


def test_group_plan_rows_by_target_deterministic_order():
    groups = group_plan_rows_by_target(
        [
            {"source_id": "B", "plan_id": 3, "priority": 2, "cutout_ra_deg": 3, "cutout_dec_deg": 4},
            {"source_id": "A", "plan_id": 2, "priority": 1, "cutout_ra_deg": 1, "cutout_dec_deg": 2},
            {"source_id": "A", "plan_id": 1, "priority": 1, "cutout_ra_deg": 1, "cutout_dec_deg": 2},
        ]
    )
    assert [group["source_id"] for group in groups] == ["A", "B"]
    assert [row["plan_id"] for row in groups[0]["rows"]] == [1, 2]


def test_download_plan_parallel_by_target_records_parent_db(tmp_path, tiny_catalog_path, local_http_server):
    cfg, conn = _download_project(tmp_path, tiny_catalog_path)
    base_url, routes = local_http_server
    body = make_synthetic_cutout(tmp_path / "served.fits").read_bytes()
    routes["/m101.fits"] = [(200, body)]
    routes["/demo.fits"] = [(200, body)]
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="m101-key",
        url=f"{base_url}/m101.fits",
        local_path="data/cutouts/M101.fits",
        priority=1,
    )
    _insert_plan(
        conn,
        source_id="SPHERExDemo",
        cutout_key="demo-key",
        url=f"{base_url}/demo.fits",
        local_path="data/cutouts/demo.fits",
        priority=2,
    )
    summary = download_plan(conn, "download_run", cfg, console=None, progress=False, verbose=True)
    assert summary.total_targets == 2
    assert summary.successful_targets == 2
    assert summary.downloaded == 2
    assert summary.failed == 0
    assert table_count(conn, "cutouts") == 2
    assert table_count(conn, "validation_results") == 2
    assert table_count(conn, "failures") == 0
    conn.close()


def test_download_plan_skips_existing_valid_file(tmp_path, tiny_catalog_path, local_http_server):
    cfg, conn = _download_project(tmp_path, tiny_catalog_path)
    cfg.download.max_workers = 2
    base_url, routes = local_http_server
    final_path = tmp_path / "data" / "cutouts" / "M101_existing.fits"
    make_synthetic_cutout(final_path)
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="existing-key",
        url=f"{base_url}/should-not-be-requested.fits",
        local_path="data/cutouts/M101_existing.fits",
    )
    summary = download_plan(conn, "download_run", cfg, console=None, progress=False, verbose=False)
    assert summary.skipped == 1
    assert summary.downloaded == 0
    assert summary.failed == 0
    assert "/should-not-be-requested.fits" not in routes
    assert conn.execute("SELECT validation_status FROM cutouts WHERE cutout_key = 'existing-key'").fetchone()[0] in {
        "passed",
        "passed_with_warnings",
    }
    conn.close()


def test_download_plan_processes_validate_existing_rows(tmp_path, tiny_catalog_path, local_http_server):
    cfg, conn = _download_project(tmp_path, tiny_catalog_path)
    base_url, routes = local_http_server
    final_path = tmp_path / "data" / "cutouts" / "M101_validate_existing.fits"
    make_synthetic_cutout(final_path)
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="validate-existing-key",
        url=f"{base_url}/should-not-be-requested.fits",
        local_path="data/cutouts/M101_validate_existing.fits",
        action="validate_existing",
    )
    summary = download_plan(conn, "download_run", cfg, console=None, progress=False, verbose=False)
    assert summary.skipped == 1
    assert summary.downloaded == 0
    assert summary.failed == 0
    assert "/should-not-be-requested.fits" not in routes
    row = conn.execute(
        "SELECT validation_status FROM cutouts WHERE cutout_key = 'validate-existing-key'"
    ).fetchone()
    assert row is not None
    assert row[0] in {"passed", "passed_with_warnings"}
    conn.close()


def test_download_plan_overwrite_existing_valid_file(tmp_path, tiny_catalog_path, local_http_server):
    cfg, conn = _download_project(tmp_path, tiny_catalog_path)
    cfg.download.overwrite_existing = True
    base_url, routes = local_http_server
    final_path = tmp_path / "data" / "cutouts" / "M101_overwrite.fits"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(b"old")
    body = make_synthetic_cutout(tmp_path / "overwrite_served.fits").read_bytes()
    routes["/overwrite.fits"] = [(200, body)]
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="overwrite-key",
        url=f"{base_url}/overwrite.fits",
        local_path="data/cutouts/M101_overwrite.fits",
    )
    summary = download_plan(conn, "download_run", cfg, console=None, progress=False, verbose=False)
    assert summary.skipped == 0
    assert summary.downloaded == 1
    assert final_path.stat().st_size == len(body)
    conn.close()


def test_download_plan_progress_path_uses_file_scheduler(tmp_path, tiny_catalog_path, local_http_server):
    cfg, conn = _download_project(tmp_path, tiny_catalog_path)
    cfg.download.max_workers = 2
    cfg.download.concurrency = 2
    base_url, routes = local_http_server
    body = make_synthetic_cutout(tmp_path / "progress_served.fits").read_bytes()
    routes["/progress-a.fits"] = [(200, body)]
    routes["/progress-b.fits"] = [(200, body)]
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="progress-a-key",
        url=f"{base_url}/progress-a.fits",
        local_path="data/cutouts/progress_a.fits",
        priority=1,
    )
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="progress-b-key",
        url=f"{base_url}/progress-b.fits",
        local_path="data/cutouts/progress_b.fits",
        priority=2,
    )
    console = Console(file=StringIO(), force_terminal=False)

    summary = download_plan(conn, "download_run", cfg, console=console, progress=True, verbose=True)

    assert summary.downloaded == 2
    assert summary.failed == 0
    assert table_count(conn, "cutouts") == 2
    conn.close()


def test_file_scheduler_parallelizes_many_files_for_one_target(tmp_path, tiny_catalog_path, local_http_server):
    cfg, conn = _download_project(tmp_path, tiny_catalog_path)
    cfg.download.max_workers = 3
    cfg.download.concurrency = 3
    cfg.download.retry.attempts = 1
    base_url, routes = local_http_server
    body = make_synthetic_cutout(tmp_path / "parallel_served.fits").read_bytes()
    routes["/slow-one-target.fits"] = [{"status": 200, "body": body, "delay": 0.5}]
    routes["/fast-one-target-a.fits"] = [(200, body)]
    routes["/fast-one-target-b.fits"] = [(200, body)]
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="same-target-slow",
        url=f"{base_url}/slow-one-target.fits",
        local_path="data/cutouts/same_target_slow.fits",
        priority=1,
    )
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="same-target-fast-a",
        url=f"{base_url}/fast-one-target-a.fits",
        local_path="data/cutouts/same_target_fast_a.fits",
        priority=2,
    )
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="same-target-fast-b",
        url=f"{base_url}/fast-one-target-b.fits",
        local_path="data/cutouts/same_target_fast_b.fits",
        priority=3,
    )

    summary = download_plan(conn, "download_run", cfg, console=None, progress=False, verbose=False)

    assert summary.total_targets == 1
    assert summary.successful_targets == 1
    assert summary.downloaded == 3
    assert summary.failed == 0
    slow = next(item for item in _StaticHandler.request_log if item["path"] == "/slow-one-target.fits")
    fast = next(item for item in _StaticHandler.request_log if item["path"] == "/fast-one-target-a.fits")
    assert fast["started"] < slow["ended"]
    assert _StaticHandler.max_active >= 2
    conn.close()


def test_503_requeue_does_not_block_fast_file(tmp_path, tiny_catalog_path, local_http_server):
    cfg, conn = _download_project(tmp_path, tiny_catalog_path)
    cfg.download.max_workers = 1
    cfg.download.concurrency = 1
    cfg.download.retry.attempts = 2
    cfg.download.retry.backoff_seconds = [0.2, 0.2]
    base_url, routes = local_http_server
    body = make_synthetic_cutout(tmp_path / "retry503_served.fits").read_bytes()
    routes["/retry-503.fits"] = [
        {"status": 503, "body": b""},
        {"status": 200, "body": body},
    ]
    routes["/fast-after-503.fits"] = [(200, body)]
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="retry-503-key",
        url=f"{base_url}/retry-503.fits",
        local_path="data/cutouts/retry_503.fits",
        priority=1,
    )
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="fast-after-503-key",
        url=f"{base_url}/fast-after-503.fits",
        local_path="data/cutouts/fast_after_503.fits",
        priority=2,
    )

    summary = download_plan(conn, "download_run", cfg, console=None, progress=False, verbose=False)

    assert summary.downloaded == 2
    assert summary.failed == 0
    paths = [item["path"] for item in _StaticHandler.request_log]
    assert paths == ["/retry-503.fits", "/fast-after-503.fits", "/retry-503.fits"]
    conn.close()


def test_retry_after_delays_only_retrying_file(tmp_path, tiny_catalog_path, local_http_server):
    cfg, conn = _download_project(tmp_path, tiny_catalog_path)
    cfg.download.max_workers = 1
    cfg.download.concurrency = 1
    cfg.download.retry.attempts = 2
    cfg.download.retry.backoff_seconds = [0.5, 0.5]
    base_url, routes = local_http_server
    body = make_synthetic_cutout(tmp_path / "retry_after_served.fits").read_bytes()
    routes["/retry-after.fits"] = [
        {"status": 429, "body": b"", "headers": {"Retry-After": "0.25"}},
        {"status": 200, "body": body},
    ]
    routes["/fast-after-429.fits"] = [(200, body)]
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="retry-after-key",
        url=f"{base_url}/retry-after.fits",
        local_path="data/cutouts/retry_after.fits",
        priority=1,
    )
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="fast-after-429-key",
        url=f"{base_url}/fast-after-429.fits",
        local_path="data/cutouts/fast_after_429.fits",
        priority=2,
    )

    summary = download_plan(conn, "download_run", cfg, console=None, progress=False, verbose=False)

    assert summary.downloaded == 2
    assert summary.failed == 0
    paths = [item["path"] for item in _StaticHandler.request_log]
    assert paths == ["/retry-after.fits", "/fast-after-429.fits", "/retry-after.fits"]
    first_retry, _, second_retry = _StaticHandler.request_log
    assert second_retry["started"] - first_retry["ended"] >= 0.20
    conn.close()


def test_low_speed_file_is_aborted_so_fast_file_can_continue(tmp_path, tiny_catalog_path, local_http_server):
    cfg, conn = _download_project(tmp_path, tiny_catalog_path)
    cfg.download.max_workers = 1
    cfg.download.concurrency = 1
    cfg.download.retry.attempts = 1
    cfg.download.min_download_rate_bytes_per_second = 1024 * 1024
    cfg.download.low_speed_time_sec = 0.05
    base_url, routes = local_http_server
    body = make_synthetic_cutout(tmp_path / "fast_after_low_speed.fits").read_bytes()
    routes["/too-slow.fits"] = [
        {
            "status": 200,
            "body": b"x" * (256 * 1024),
            "chunk_size": 1024,
            "chunk_delay": 0.02,
        }
    ]
    routes["/fast-after-slow.fits"] = [(200, body)]
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="too-slow-key",
        url=f"{base_url}/too-slow.fits",
        local_path="data/cutouts/too_slow.fits",
        priority=1,
    )
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="fast-after-slow-key",
        url=f"{base_url}/fast-after-slow.fits",
        local_path="data/cutouts/fast_after_slow.fits",
        priority=2,
    )

    summary = download_plan(conn, "download_run", cfg, console=None, progress=False, verbose=False)

    assert summary.downloaded == 1
    assert summary.failed == 1
    assert [item["path"] for item in _StaticHandler.request_log] == ["/too-slow.fits", "/fast-after-slow.fits"]
    failure = conn.execute("SELECT reason FROM failures WHERE local_path LIKE '%too_slow%'").fetchone()
    assert "download rate" in failure["reason"]
    conn.close()


def test_completed_files_are_recorded_before_slow_file_finishes(tmp_path, tiny_catalog_path, local_http_server):
    cfg, conn = _download_project(tmp_path, tiny_catalog_path)
    cfg.download.max_workers = 2
    cfg.download.concurrency = 2
    base_url, routes = local_http_server
    body = make_synthetic_cutout(tmp_path / "immediate_db_served.fits").read_bytes()
    routes["/slow-db.fits"] = [{"status": 200, "body": body, "delay": 1.0}]
    routes["/fast-db.fits"] = [(200, body)]
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="slow-db-key",
        url=f"{base_url}/slow-db.fits",
        local_path="data/cutouts/slow_db.fits",
        priority=1,
    )
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="fast-db-key",
        url=f"{base_url}/fast-db.fits",
        local_path="data/cutouts/fast_db.fits",
        priority=2,
    )
    conn.close()

    result_holder = {}

    def run_download():
        worker_conn = connect(cfg.project.database_path)
        try:
            result_holder["summary"] = download_plan(
                worker_conn,
                "download_run",
                cfg,
                console=None,
                progress=False,
                verbose=False,
            )
        finally:
            worker_conn.close()

    thread = Thread(target=run_download)
    thread.start()
    poll_conn = connect(cfg.project.database_path)
    try:
        deadline = time.monotonic() + 3.0
        seen = False
        while time.monotonic() < deadline:
            count = poll_conn.execute("SELECT COUNT(*) FROM cutouts").fetchone()[0]
            if count >= 1:
                seen = True
                break
            time.sleep(0.02)
        assert seen
        assert thread.is_alive()
    finally:
        poll_conn.close()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result_holder["summary"].downloaded == 2


def test_iter_download_plan_results_records_before_yield(tmp_path, tiny_catalog_path, local_http_server):
    cfg, conn = _download_project(tmp_path, tiny_catalog_path)
    cfg.download.max_workers = 1
    base_url, routes = local_http_server
    body = make_synthetic_cutout(tmp_path / "adapter_served.fits").read_bytes()
    routes["/adapter.fits"] = [(200, body)]
    _insert_plan(
        conn,
        source_id="M101",
        cutout_key="adapter-key",
        url=f"{base_url}/adapter.fits",
        local_path="data/cutouts/adapter.fits",
    )

    terminal = [
        item
        for item in iter_download_plan_results(conn, "download_run", cfg, progress=False)
        if isinstance(item, CompletedDownload)
    ]

    assert len(terminal) == 1
    assert terminal[0].result.success
    row = conn.execute("SELECT validation_status FROM cutouts WHERE cutout_key = 'adapter-key'").fetchone()
    assert row["validation_status"] in {"passed", "passed_with_warnings"}
    conn.close()
