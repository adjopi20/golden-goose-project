"""Manual bounded public connectivity check; no ledger writes or orders."""
import time
from utils.engine.adapters.binance_public import BinancePublic


if __name__ == "__main__":
    api = BinancePublic()
    symbols = ["ETHUSDC", "BNBUSDC", "HYPEUSDT"]
    print(api.validate_markets(symbols), flush=True)
    end = api.server_time()//60_000*60_000
    print("Native minute sample:", len(list(api.minute_history("ETHUSDC",end-180_000,end))), flush=True)
    stream = api.stream(symbols)
    seen = set()
    deadline = time.monotonic()+15
    try:
        while time.monotonic()<deadline and len(seen)<len(symbols):
            trade = next(stream)
            if trade:
                seen.add(trade["symbol"])
    finally:
        stream.close()
    print("WebSocket markets received:", sorted(seen), flush=True)
    if seen != set(symbols):
        raise SystemExit("Not every market observed in 15 seconds; verify feed before proceeding")
