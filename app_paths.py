"""Shared paths and dependency discovery for the desktop app."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def downloader_root() -> Path:
    """Find the locally installed upstream downloader without reading credentials."""
    configured = os.environ.get("DOUYIN_DOWNLOADER_ROOT", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())

    # Preferred layout after running scripts/bootstrap.ps1.
    candidates.extend(
        [
            ROOT / ".deps" / "douyin-downloader",
            # Compatibility with older local installations.
            ROOT / "vendor" / "douyin-downloader",
            ROOT.parent / "vendor" / "douyin-downloader",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


VENDOR_ROOT = downloader_root()
