"""Simple logging setup."""
from __future__ import annotations

import logging
from pathlib import Path


def setup_logging(level: str = "INFO", logs_dir: Path | None = None) -> None:
    logs_dir = logs_dir or Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(logs_dir / "vpn_manager.log", encoding="utf-8"),
        ],
    )
