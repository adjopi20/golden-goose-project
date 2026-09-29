FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY live_engine/requirements-paper.txt /app/requirements-paper.txt
RUN pip install --no-cache-dir -r /app/requirements-paper.txt \
    && useradd --system --uid 10001 --create-home trader \
    && mkdir /data && chown trader:trader /data

COPY live_engine /app/live_engine
COPY backtest_engine /app/backtest_engine
COPY models/trend_following_preny_profile_15m /app/models/trend_following_preny_profile_15m

USER trader
ENTRYPOINT ["python", "-m", "models.trend_following_preny_profile_15m.paper_worker"]
CMD ["--database", "/data/binance.sqlite", "--action", "run"]
