"""Conservative parsing and access decisions for official cloud metadata."""

from __future__ import annotations

import json
from typing import Any

from .config import Config
from .models import AccessDecision


def parse_cloud_access(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    text = str(value).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"raw": text}


def cloud_full_product_available(cloud: dict[str, Any]) -> bool:
    text = json.dumps(cloud).lower()
    return "s3" in text or "cloud" in text or "uri" in text


def cloud_cutout_available(cloud: dict[str, Any], capability_key: str | None = None) -> bool:
    if capability_key:
        value = cloud.get(capability_key)
        return bool(value)
    text = json.dumps(cloud).lower()
    return "cutout" in text and ("url" in text or "template" in text or "operation" in text)


def select_access_method(product: dict[str, Any], requested_product: str, config: Config) -> AccessDecision:
    cloud = parse_cloud_access(product.get("cloud_access_json") or product.get("cloud_access"))
    if requested_product == "cutout":
        if config.cloud.prefer_cloud_for_cutouts:
            if cloud_cutout_available(cloud, config.cloud.official_cutout_capability_key):
                return AccessDecision("cloud_cutout", True, "official cloud cutout metadata available")
            return AccessDecision("cloud_cutout", False, "official cloud cutout metadata is not available")
        access_url = product.get("access_url")
        if access_url:
            return AccessDecision("onprem_cutout", True, "using parent access_url cutout service", access_url)
        return AccessDecision("onprem_cutout", False, "missing parent access_url")

    if requested_product == "full_product" and config.cloud.prefer_cloud_for_full_products:
        if cloud_full_product_available(cloud):
            return AccessDecision("cloud_full_product", True, "official cloud full-product metadata available")
        return AccessDecision("cloud_full_product", False, "official cloud full-product metadata is not available")

    return AccessDecision("unavailable", False, f"unsupported requested product: {requested_product}")
