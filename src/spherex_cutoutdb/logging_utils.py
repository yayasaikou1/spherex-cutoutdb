"""Terminal and log helpers."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console

from .config import Config


def make_console(config: Config, *, quiet: bool = False) -> Console:
    return Console(quiet=quiet or not config.logging.rich)


def configure_logging(config: Config, level: str | None = None) -> None:
    log_level = getattr(logging, (level or config.logging.log_level).upper(), logging.INFO)
    config.project.log_root.mkdir(parents=True, exist_ok=True)
    log_path = Path(config.project.log_root) / "spxcutdb.log"
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path)],
    )
    logging.getLogger("astropy").setLevel(logging.WARNING)
