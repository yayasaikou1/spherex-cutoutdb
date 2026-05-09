"""Official IRSA on-prem cutout URL construction."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def arcsec_to_url_size_deg(size_arcsec: float) -> float:
    if size_arcsec <= 0:
        raise ValueError("cutout size must be positive")
    return float(size_arcsec) / 3600.0


def build_cutout_url(access_url: str, ra_deg: float, dec_deg: float, size_deg: float) -> str:
    if not access_url:
        raise ValueError("parent access_url is required")
    parts = urlsplit(access_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("center", f"{float(ra_deg):.10f},{float(dec_deg):.10f}"))
    query.append(("size", f"{float(size_deg):.10g}"))
    encoded = urlencode(query, doseq=True, safe=",")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, encoded, parts.fragment))
