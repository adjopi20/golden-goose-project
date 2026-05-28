import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

from indicator.volume_profile import (
    generate_historical_session_profiles,
)

INPUT_FILES = [
    "storage/btcusdt/BTCUSDT-aggTrades-2026-04.parquet",
    # "storage/btcusdt_aggtrade/BTCUSDT-aggTrades-2021-05.parquet",
    # "storage/btcusdt_aggtrade/BTCUSDT-aggTrades-2021-06.parquet",
]

OUTPUT_PATH = "session_profiles_202604.jsonl"

generate_historical_session_profiles(
    input_files=INPUT_FILES,
    output_path=OUTPUT_PATH,
    n_bins=50,
)

print("Done generating session profiles.")