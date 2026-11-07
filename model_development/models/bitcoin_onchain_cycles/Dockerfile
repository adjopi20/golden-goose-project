# LEGACY accumulation-only image; not the selected deployment target.
# Build live_mvrv/compose.yaml for the 0/7 + P5/P95 daily service.
FROM python:3.13-slim

# The live monitor needs Coin Metrics only. Blockchair Coin Days Destroyed is
# optional (`--with-cdd`), requires a real browser, and is deliberately not
# installed here: without it the image needs no Chromium and no Xvfb.
RUN pip install --no-cache-dir \
    numpy==2.4.4 \
    pandas==3.0.2 \
    requests==2.33.1

RUN useradd --create-home --uid 1000 monitor

WORKDIR /app
COPY model_development/models/bitcoin_onchain_cycles /app/models/bitcoin_onchain_cycles

RUN mkdir -p /app/models/bitcoin_onchain_cycles/data \
    && chown -R monitor:monitor /app

USER monitor
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "model_development/models/bitcoin_onchain_cycles/run_daily_monitor.py"]
