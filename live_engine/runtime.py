"""Local paper plumbing CLI. No market-data connection or real orders."""
import argparse
import json
from pathlib import Path
import sys

from backtest_engine.replay_aggtrades import Config
from .order_manager import PaperOrderManager
from .state_store import StateStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--action", choices=("init", "status", "consume", "backup"), default="status")
    parser.add_argument("--backup-to", type=Path)
    args = parser.parse_args()
    # JSON is a YAML subset; templates deliberately require no YAML dependency.
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if cfg["mode"] != "paper" or cfg["schema_version"] != 1:
        raise ValueError("Only schema 1 paper configuration is supported")
    store = StateStore(args.database, cfg["venue"])
    try:
        manager = PaperOrderManager(store)
        for account in cfg["accounts"]:
            manager.register(account["id"], account["symbol"], Config(**account["execution"]),
                             allowed_risks=account["allowed_risks"], contract=account["contract"])
        if args.action == "consume":
            # Diagnostic/test harness, not historical sweep or a live feed.
            # Signal event first, then next eligible trade. Input errors stop it.
            for line in sys.stdin:
                event = json.loads(line)
                if event["type"] == "signal":
                    result = manager.submit(event["account"], event["signal"], snapshot=event["snapshot"],
                                            risk_fraction=event["risk_fraction"], selected=event["selected"])
                elif event["type"] == "trade":
                    result = manager.tick(event["account"], event["trade"])
                else:
                    raise ValueError("Unknown event type")
                print(json.dumps({"result": result}), flush=True)
        elif args.action == "backup":
            if args.backup_to is None:
                raise ValueError("--backup-to required")
            store.backup(args.backup_to)
        print(json.dumps({"mode": "paper", "feed_connected": False, "accounts": manager.status()}, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
