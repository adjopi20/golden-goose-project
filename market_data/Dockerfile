FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY market_data/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 --create-home collector \
    && mkdir /data && chown collector:collector /data
COPY market_data /app/market_data
COPY trading_core/__init__.py trading_core/contracts.py trading_core/session.py trading_core/profiles.py /app/trading_core/
COPY live_engine/__init__.py live_engine/state_store.py /app/live_engine/
COPY live_engine/adapters/__init__.py live_engine/adapters/binance.py live_engine/adapters/lighter.py live_engine/adapters/lighter_public.py /app/live_engine/adapters/
COPY infra/deploy/phase2/binance.json infra/deploy/phase2/lighter.json /app/config/
USER collector
ENTRYPOINT ["python", "-m", "market_data.run"]
