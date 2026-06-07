"""Repair terminal rounds that are missing AG-UI terminal events."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.api.config import get_settings  # noqa: E402
from src.api.models.database import SessionLocal, init_db  # noqa: E402
from src.api.services.terminal_repair_service import TerminalRepairService  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since-hours",
        type=int,
        default=settings.agui_repair_terminal_since_hours,
        help="Scan terminal rounds updated in the last N hours.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report missing terminal events without writing.")
    args = parser.parse_args(argv)

    init_db()
    with SessionLocal() as db:
        report = TerminalRepairService(db).repair_terminal_runs(
            since_hours=args.since_hours,
            dry_run=args.dry_run,
        )
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
