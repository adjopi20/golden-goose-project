from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonlStorage:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def write(self, stream: str, record: dict[str, Any]) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.log_dir / day / f"{stream}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"logged_at_utc": datetime.now(timezone.utc).isoformat(), **record}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")

    def read_recent_raw_aggtrades(self, lookback_hours: int = 25) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for day_dir in sorted((p for p in self.log_dir.glob("*") if p.is_dir()), reverse=True)[:3]:
            path = day_dir / "raw_aggtrade.jsonl"
            if not path.exists():
                continue
            with path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "timestamp" in row:
                        rows.append(row)
        if not rows:
            return []
        max_ts = max(int(row["timestamp"]) for row in rows)
        cutoff = max_ts - lookback_hours * 60 * 60 * 1000
        recent = [row for row in rows if int(row["timestamp"]) >= cutoff]
        return sorted(recent, key=lambda row: (int(row["timestamp"]), int(row.get("agg_trade_id", 0))))
