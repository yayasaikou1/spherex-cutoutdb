"""Calibration cache synchronization."""

from __future__ import annotations

import shutil
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

from spherex_cutoutdb.config import Config

from .registry import (
    cached_calibration_files,
    infer_calibration_version,
    infer_detector_id,
    infer_product_type,
    register_calibration_file,
)

_THREAD_LOCAL = threading.local()


@dataclass(slots=True)
class CalibrationSyncSummary:
    imported: int = 0
    downloaded: int = 0
    skipped_existing: int = 0
    validated: int = 0
    valid: int = 0
    failed: int = 0
    missing: list[str] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class CalibrationDownloadTask:
    product: str
    detector: int
    url: str
    target: Path
    expected_size: int | None = None


def sync_calibrations(
    conn,
    config: Config,
    *,
    products: list[str] | None = None,
    detectors: list[int] | None = None,
    input_dir: Path | None = None,
    urls: dict[str, str] | None = None,
) -> CalibrationSyncSummary:
    selected_products = _expand_products(products or config.calibration.required_products, config)
    selected_detectors = detectors or _detectors_from_db(conn) or list(range(1, 7))
    summary = CalibrationSyncSummary()

    imported_paths: list[tuple[Path, str | None, int | None, str | None]] = []
    if input_dir is not None:
        for path in sorted(Path(input_dir).rglob("*.fits")):
            product = infer_product_type(path)
            if product in selected_products:
                imported_paths.append((path, product, infer_detector_id(path), None))

    url_templates = dict(config.calibration.product_urls)
    if urls:
        url_templates.update(urls)
    download_tasks: list[CalibrationDownloadTask] = []
    if input_dir is None or urls:
        for product in selected_products:
            template = url_templates.get(product)
            if template:
                for detector in selected_detectors:
                    url = _format_url_template(config, product, detector, template)
                    target = _cache_target_for_url(config, product, detector, url)
                    download_tasks.append(CalibrationDownloadTask(product, detector, url, target))
        templated_products = set(url_templates)
        official_products = [product for product in selected_products if product not in templated_products]
        if input_dir is None and config.calibration.use_official_ibe and official_products:
            download_tasks.extend(_official_download_tasks(config, official_products, selected_detectors))

    for path, product, detector, source_url, downloaded in _download_tasks(download_tasks, config):
        if downloaded:
            summary.downloaded += 1
        else:
            summary.skipped_existing += 1
        imported_paths.append((path, product, detector, source_url))

    for path, product, detector, source_url in imported_paths:
        target = _cache_target_for_file(config, product, detector, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.resolve() != target.resolve():
            shutil.copy2(path, target)
            summary.imported += 1
        record = register_calibration_file(
            conn,
            config,
            target,
            product_type=product,
            detector_id=detector,
            source_url=source_url,
        )
        summary.records.append(record)

    validate_summary = validate_cached_calibrations(conn, config, products=selected_products)
    summary.validated += validate_summary.validated
    summary.valid += validate_summary.valid
    summary.failed += validate_summary.failed
    summary.records.extend(validate_summary.records)
    summary.missing = _missing_required(conn, config, selected_products, selected_detectors)
    return summary


def validate_cached_calibrations(
    conn,
    config: Config,
    *,
    products: list[str] | None = None,
) -> CalibrationSyncSummary:
    selected = set(_expand_products(products or config.calibration.required_products, config))
    summary = CalibrationSyncSummary()
    seen: set[Path] = set()
    for path in cached_calibration_files(config):
        product = infer_product_type(path)
        if product not in selected:
            continue
        if path in seen:
            continue
        seen.add(path)
        record = register_calibration_file(conn, config, path, product_type=product)
        summary.validated += 1
        if record["validation"].status == "valid":
            summary.valid += 1
        else:
            summary.failed += 1
        summary.records.append(record)
    return summary


def _expand_products(products: list[str], config: Config) -> list[str]:
    expanded: list[str] = []
    for product in products:
        if product == "required":
            expanded.extend(config.calibration.required_products)
        else:
            expanded.append(product)
    return sorted(set(expanded))


def _detectors_from_db(conn) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT detector_id
        FROM cutouts
        WHERE detector_id IS NOT NULL
        UNION
        SELECT DISTINCT detector_id
        FROM discovery_products
        WHERE detector_id IS NOT NULL
        ORDER BY detector_id
        """
    ).fetchall()
    return [int(row[0]) for row in rows if row[0] is not None]


def _cache_target_for_file(config: Config, product: str | None, detector: int | None, path: Path) -> Path:
    product_name = product or infer_product_type(path) or "unknown"
    detector_id = detector if detector is not None else infer_detector_id(path)
    version = infer_calibration_version(path) or "unknown_version"
    detector_dir = f"D{detector_id}" if detector_id is not None else "Dunknown"
    return config.calibration.cache_root / config.calibration.release / product_name / version / detector_dir / path.name


def _cache_target_for_url(config: Config, product: str, detector: int, url: str) -> Path:
    name = Path(url.split("?", 1)[0]).name or f"{product}_D{detector}.fits"
    return _cache_target_for_file(config, product, detector, Path(name))


def _format_url_template(config: Config, product: str, detector: int, template: str) -> str:
    return template.format(
        release=config.calibration.release,
        release_lower=config.calibration.release.lower(),
        product_type=product,
        detector=detector,
        detector_id=detector,
    )


def _official_download_tasks(config: Config, products: list[str], detectors: list[int]) -> list[CalibrationDownloadTask]:
    mode = config.calibration.download_source
    if mode == "auto" and not config.calibration.prefer_cloud:
        mode = "ibe"
    if mode == "cloud":
        return _official_s3_download_tasks(config, products, detectors)
    if mode == "ibe":
        return _official_ibe_download_tasks(config, products, detectors)

    cloud_tasks = _safe_official_tasks(_official_s3_download_tasks, config, products, detectors)
    ibe_tasks = _safe_official_tasks(_official_ibe_download_tasks, config, products, detectors)
    if not cloud_tasks:
        return ibe_tasks
    if not ibe_tasks:
        return cloud_tasks
    cloud_score = _probe_download_score(cloud_tasks[0].url, config)
    ibe_score = _probe_download_score(ibe_tasks[0].url, config)
    if cloud_score >= ibe_score:
        return _complete_with_fallback(cloud_tasks, ibe_tasks, products, detectors)
    return _complete_with_fallback(ibe_tasks, cloud_tasks, products, detectors)


def _safe_official_tasks(factory, config: Config, products: list[str], detectors: list[int]) -> list[CalibrationDownloadTask]:
    try:
        return factory(config, products, detectors)
    except Exception:
        return []


def _complete_with_fallback(
    primary: list[CalibrationDownloadTask],
    fallback: list[CalibrationDownloadTask],
    products: list[str],
    detectors: list[int],
) -> list[CalibrationDownloadTask]:
    expected = {(product, detector) for product in products for detector in detectors}
    found = {(task.product, task.detector) for task in primary}
    missing = expected - found
    if not missing:
        return primary
    return primary + [task for task in fallback if (task.product, task.detector) in missing]


def _official_s3_download_tasks(config: Config, products: list[str], detectors: list[int]) -> list[CalibrationDownloadTask]:
    tasks: list[CalibrationDownloadTask] = []
    for product in products:
        version_prefix = _latest_s3_version_prefix(config, product)
        if version_prefix is None:
            continue
        entries = _read_s3_objects(config, version_prefix)
        for detector in detectors:
            file_entry = _select_detector_file(entries, product, detector)
            if file_entry is None:
                continue
            name = str(file_entry["name"])
            key = str(file_entry["key"])
            url = _s3_file_url(config, key)
            target = _cache_target_for_file(config, product, detector, Path(name))
            tasks.append(
                CalibrationDownloadTask(
                    product=product,
                    detector=detector,
                    url=url,
                    target=target,
                    expected_size=_parse_size(file_entry.get("size")),
                )
            )
    return tasks


def _latest_s3_version_prefix(config: Config, product: str) -> str | None:
    prefix = f"{_cloud_release_prefix(config)}/{product}/"
    prefixes = _read_s3_common_prefixes(config, prefix)
    if not prefixes:
        return None
    prefixes.sort(key=_version_sort_key)
    return prefixes[-1]


def _cloud_release_prefix(config: Config) -> str:
    return config.calibration.cloud_prefix.format(
        release=config.calibration.release,
        release_lower=config.calibration.release.lower(),
    ).strip("/")


def _s3_base_url(config: Config) -> str:
    return f"https://{config.calibration.cloud_bucket}.s3.{config.calibration.cloud_region}.amazonaws.com"


def _s3_file_url(config: Config, key: str) -> str:
    return f"{_s3_base_url(config)}/{'/'.join(quote(part) for part in key.strip('/').split('/'))}"


def _read_s3_common_prefixes(config: Config, prefix: str) -> list[str]:
    root = _read_s3_list(config, prefix=prefix, delimiter="/")
    namespace = _xml_namespace(root)
    return [
        node.findtext(f"{namespace}Prefix", default="")
        for node in root.findall(f"{namespace}CommonPrefixes")
        if node.findtext(f"{namespace}Prefix", default="")
    ]


def _read_s3_objects(config: Config, prefix: str) -> list[dict[str, Any]]:
    root = _read_s3_list(config, prefix=prefix)
    namespace = _xml_namespace(root)
    rows: list[dict[str, Any]] = []
    for node in root.findall(f"{namespace}Contents"):
        key = node.findtext(f"{namespace}Key", default="")
        if not key or key.endswith("/"):
            continue
        rows.append(
            {
                "key": key,
                "name": Path(key).name,
                "size": node.findtext(f"{namespace}Size", default=""),
                "last_modified": node.findtext(f"{namespace}LastModified", default=""),
            }
        )
    return rows


def _read_s3_list(config: Config, *, prefix: str, delimiter: str | None = None) -> ET.Element:
    params = {"list-type": "2", "prefix": prefix}
    if delimiter is not None:
        params["delimiter"] = delimiter
    response = _session(config).get(
        _s3_base_url(config),
        params=params,
        headers={"User-Agent": config.download.user_agent},
        timeout=(config.download.connect_timeout_sec, config.calibration.download_timeout_sec),
    )
    response.raise_for_status()
    return ET.fromstring(response.content)


def _xml_namespace(root: ET.Element) -> str:
    if root.tag.startswith("{"):
        return root.tag.split("}", 1)[0] + "}"
    return ""


def _official_ibe_download_tasks(config: Config, products: list[str], detectors: list[int]) -> list[CalibrationDownloadTask]:
    tasks: list[CalibrationDownloadTask] = []
    for product in products:
        version_dir = _latest_official_version_dir(config, product)
        if version_dir is None:
            continue
        for detector in detectors:
            listing_path = f"{product}/{version_dir}/{detector}"
            entries = _read_ibe_listing(config, listing_path)
            file_entry = _select_detector_file(entries, product, detector)
            if file_entry is None:
                continue
            name = str(file_entry["name"])
            url = _official_ibe_file_url(config, product, version_dir, detector, name)
            target = _cache_target_for_file(config, product, detector, Path(name))
            tasks.append(
                CalibrationDownloadTask(
                    product=product,
                    detector=detector,
                    url=url,
                    target=target,
                    expected_size=_parse_size(file_entry.get("size")),
                )
            )
    return tasks


def _latest_official_version_dir(config: Config, product: str) -> str | None:
    entries = [entry for entry in _read_ibe_listing(config, product) if entry.get("size") == "-"]
    if not entries:
        return None
    entries.sort(key=lambda entry: _version_sort_key(str(entry.get("name", ""))))
    return str(entries[-1]["name"])


def _read_ibe_listing(config: Config, path: str) -> list[dict[str, Any]]:
    base = config.calibration.official_ibe_listing_url.format(
        release=config.calibration.release,
        release_lower=config.calibration.release.lower(),
    ).rstrip("/")
    encoded = "/".join(quote(part) for part in path.strip("/").split("/") if part)
    url = f"{base}/{encoded}"
    response = _session(config).get(
        url,
        headers={
            "Accept": "application/x-ndjson",
            "User-Agent": config.download.user_agent,
        },
        timeout=(config.download.connect_timeout_sec, config.calibration.download_timeout_sec),
    )
    response.raise_for_status()
    rows: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            import json

            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _official_ibe_file_url(config: Config, product: str, version_dir: str, detector: int, filename: str) -> str:
    base = config.calibration.official_ibe_base_url.format(
        release=config.calibration.release,
        release_lower=config.calibration.release.lower(),
    ).rstrip("/")
    encoded = "/".join(quote(part) for part in [product, version_dir, str(detector), filename])
    return f"{base}/{encoded}"


def _select_detector_file(entries: list[dict[str, Any]], product: str, detector: int) -> dict[str, Any] | None:
    prefix = f"{product}_D{detector}_"
    candidates = [
        entry
        for entry in entries
        if str(entry.get("name", "")).startswith(prefix)
        and str(entry.get("name", "")).lower().endswith(".fits")
        and entry.get("size") != "-"
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda entry: _version_sort_key(str(entry.get("name", ""))))
    return candidates[-1]


def _version_sort_key(name: str) -> tuple[str, int, str]:
    version = 0
    date = ""
    import re

    version_match = re.search(r"-v(\d+)", name)
    if version_match:
        version = int(version_match.group(1))
    date_match = re.search(r"(\d{4}-\d{3})", name)
    if date_match:
        date = date_match.group(1)
    return date, version, name


def _parse_size(value: Any) -> int | None:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def _download_tasks(
    tasks: list[CalibrationDownloadTask],
    config: Config,
) -> list[tuple[Path, str, int, str, bool]]:
    if not tasks:
        return []
    workers = max(1, min(config.calibration.download_max_workers, len(tasks)))
    results: list[tuple[Path, str, int, str, bool]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_download_task, task, config): task for task in tasks}
        for future in as_completed(futures):
            results.append(future.result())
    return results


def _download_task(task: CalibrationDownloadTask, config: Config) -> tuple[Path, str, int, str, bool]:
    if _target_matches(task.target, task.expected_size):
        return task.target, task.product, task.detector, task.url, False
    _download_or_copy_url(task.url, task.target, config)
    return task.target, task.product, task.detector, task.url, True


def _target_matches(target: Path, expected_size: int | None) -> bool:
    if not target.exists() or not target.is_file():
        return False
    if target.stat().st_size <= 0:
        return False
    return expected_size is None or target.stat().st_size == expected_size


def _download_or_copy_url(url: str, target: Path, config: Config) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    if url.startswith("file://"):
        shutil.copy2(Path(url[7:]), tmp)
        tmp.replace(target)
        return
    source = Path(url)
    if source.exists():
        shutil.copy2(source, tmp)
        tmp.replace(target)
        return
    with _session(config).get(
        url,
        stream=True,
        headers={"User-Agent": config.download.user_agent},
        timeout=(config.download.connect_timeout_sec, config.calibration.download_timeout_sec),
    ) as response:
        response.raise_for_status()
        with tmp.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=config.download.chunk_size_bytes):
                if chunk:
                    handle.write(chunk)
    tmp.replace(target)


def _probe_download_score(url: str, config: Config) -> float:
    headers = {
        "User-Agent": config.download.user_agent,
        "Range": "bytes=0-1048575",
    }
    start = time.monotonic()
    total = 0
    try:
        with _session(config).get(
            url,
            stream=True,
            headers=headers,
            timeout=(config.download.connect_timeout_sec, min(config.calibration.download_timeout_sec, 30)),
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=262144):
                if not chunk:
                    continue
                total += len(chunk)
                if total >= 1048576:
                    break
    except Exception:
        return 0.0
    elapsed = max(time.monotonic() - start, 1.0e-6)
    return total / elapsed


def _session(config: Config) -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        pool_size = max(4, int(config.calibration.download_max_workers))
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _THREAD_LOCAL.session = session
    return session


def _missing_required(conn, config: Config, products: list[str], detectors: list[int]) -> list[str]:
    missing: list[str] = []
    for product in products:
        for detector in detectors:
            row = conn.execute(
                """
                SELECT 1
                FROM calibration_products
                WHERE release = ?
                  AND product_type = ?
                  AND detector_id = ?
                  AND validation_status = 'valid'
                  AND active = 1
                LIMIT 1
                """,
                (config.calibration.release, product, detector),
            ).fetchone()
            if row is None:
                missing.append(f"{product}:D{detector}")
    return missing
