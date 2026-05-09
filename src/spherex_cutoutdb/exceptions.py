"""Structured exceptions used for user-facing failures and DB records."""

from __future__ import annotations


class SpxCutoutDBError(Exception):
    """Base package error."""


class ConfigError(SpxCutoutDBError):
    """Invalid configuration."""


class CatalogError(SpxCutoutDBError):
    """Invalid source catalog."""


class DiscoveryError(SpxCutoutDBError):
    """SIA2 discovery failed."""


class PlanningError(SpxCutoutDBError):
    """Download planning failed."""


class DownloadError(SpxCutoutDBError):
    """Cutout download failed."""


class ValidationError(SpxCutoutDBError):
    """FITS validation failed."""
