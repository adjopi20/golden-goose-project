FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY live_engine/requirements-paper.txt /app/requirements-paper.txt
RUN pip install --no-cache-dir -r /app/requirements-paper.txt \
    && useradd --system --uid 10001 --create-home trader \
    && mkdir /data && chown trader:trader /data

COPY trading_core /app/trading_core
COPY market_data/__init__.py market_data/aggregation.py /app/market_data/
COPY live_engine /app/live_engine
COPY backtest_engine/__init__.py backtest_engine/replay_aggtrades.py backtest_engine/results.py backtest_engine/exit_policy.py /app/backtest_engine/
COPY models/trend_following_preny_profile_15m/runtime /app/models/trend_following_preny_profile_15m/runtime
COPY models/trend_following_preny_profile_15m/paper_worker.py models/trend_following_preny_profile_15m/lighter_worker.py models/trend_following_preny_profile_15m/calibrate_c1_native.py /app/models/trend_following_preny_profile_15m/
COPY models/trend_following_preny_profile_15m/deploy /app/models/trend_following_preny_profile_15m/deploy
COPY models/trend_following_preny_profile_15m/contracts /app/models/trend_following_preny_profile_15m/contracts

USER trader
ENTRYPOINT ["python", "-m", "models.trend_following_preny_profile_15m.paper_worker"]
CMD ["--database", "/data/binance.sqlite", "--action", "run"]
