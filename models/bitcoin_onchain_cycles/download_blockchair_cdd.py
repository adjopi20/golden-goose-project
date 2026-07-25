from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import requests
import websocket


CHART_URL = "https://blockchair.com/bitcoin/charts/coindays-destroyed?interval=3m"
DEFAULT_OUTPUT = Path(__file__).parent / "data" / "source" / "data.tsv"


def find_browser() -> Path:
    configured = os.environ.get("BLOCKCHAIR_BROWSER")
    candidates = [
        configured,
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise FileNotFoundError(
        "Chrome/Chromium was not found. Set BLOCKCHAIR_BROWSER to its executable."
    )


def validate_tsv(text: str) -> tuple[int, str]:
    rows = list(csv.reader(text.splitlines(), delimiter="\t"))
    if len(rows) < 2 or len(rows[0]) != 2:
        raise ValueError("Blockchair response is not a two-column TSV")
    for row in rows[1:]:
        if len(row) != 2:
            raise ValueError("Blockchair TSV contains a malformed row")
        datetime.strptime(row[0], "%d.%m.%Y")
        if float(row[1]) < 0:
            raise ValueError("Blockchair TSV contains a negative CDD value")
    return len(rows) - 1, rows[-1][0]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def cdp_call(ws: websocket.WebSocket, state: dict[str, int], method: str, **params):
    state["id"] += 1
    request_id = state["id"]
    ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
    while True:
        message = json.loads(ws.recv())
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise RuntimeError(f"{method} failed: {message['error']}")
        return message.get("result", {})


def download_tsv(timeout: int) -> str:
    browser = find_browser()
    port = free_port()
    cache_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache"))
    profile = cache_root / "golden-goose" / "blockchair-browser"
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        str(browser),
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        CHART_URL,
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(
        "Chrome opened. If Blockchair shows a verification page, complete it; "
        "the TSV download will then continue automatically."
    )
    ws = None
    deadline = time.monotonic() + timeout
    try:
        target = None
        while time.monotonic() < deadline:
            try:
                targets = requests.get(
                    f"http://127.0.0.1:{port}/json/list", timeout=2
                ).json()
                target = next(
                    (
                        item
                        for item in targets
                        if item.get("type") == "page"
                        and "blockchair.com/bitcoin/charts/" in item.get("url", "")
                    ),
                    None,
                )
                if target:
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(0.5)
        if not target:
            raise TimeoutError("Chrome did not open the Blockchair chart")

        ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=5, suppress_origin=True
        )
        state = {"id": 0}
        expression = """
        (() => {
          const button = document.getElementById("download-tsv-button");
          return Boolean(button && !button.disabled);
        })()
        """
        while time.monotonic() < deadline:
            result = cdp_call(
                ws,
                state,
                "Runtime.evaluate",
                expression=expression,
                returnByValue=True,
            )
            if result.get("result", {}).get("value") is True:
                break
            time.sleep(1)
        else:
            raise TimeoutError(
                "Blockchair did not load the TSV button; complete any browser check "
                "shown in Chrome and run the script again"
            )

        capture = """
        (() => {
          let tsv = null;
          const originalClick = HTMLAnchorElement.prototype.click;
          HTMLAnchorElement.prototype.click = function () {
            if (this.download === "data.tsv") tsv = this.href;
            else originalClick.call(this);
          };
          try {
            document.getElementById("download-tsv-button").click();
          } finally {
            HTMLAnchorElement.prototype.click = originalClick;
          }
          return tsv;
        })()
        """
        result = cdp_call(
            ws,
            state,
            "Runtime.evaluate",
            expression=capture,
            returnByValue=True,
        )
        data_uri = result.get("result", {}).get("value")
        if not data_uri or not data_uri.startswith("data:text/tsv"):
            raise RuntimeError("Blockchair did not generate its TSV download")
        return unquote(data_uri.split(",", 1)[1])
    finally:
        if ws is not None:
            ws.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Blockchair's Bitcoin Coin-Days Destroyed TSV."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        assert validate_tsv("Time\tCDD\n03.01.2009\t0\n04.01.2009\t10") == (
            2,
            "04.01.2009",
        )
        print("self-test passed")
        return

    text = download_tsv(args.timeout)
    rows, latest_date = validate_tsv(text)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    temporary.replace(args.output)
    print(f"saved {rows:,} rows through {latest_date} to {args.output}")


if __name__ == "__main__":
    main()
