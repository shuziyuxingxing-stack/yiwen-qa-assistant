from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_sysu_anything_cli() -> Path:
    configured = os.getenv("SYSU_ANYTHING_CLI", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()
    return (PROJECT_ROOT / "node_modules" / "sysu-anything" / "bin" / "sysu-anything.js").resolve()
