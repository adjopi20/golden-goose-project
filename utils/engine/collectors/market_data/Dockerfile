FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY utils/engine/collectors/market_data/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 --create-home collector \
    && mkdir /data && chown collector:collector /data
COPY utils/engine/collectors/market_data /app/utils/engine/collectors/market_data
COPY utils/__init__.py /app/utils/
COPY utils/market_session.py /app/utils/
COPY indicator/__init__.py /app/indicator/
COPY indicator/value_area.py /app/indicator/
COPY utils/engine/contracts.py /app/utils/engine/contracts.py
COPY utils/engine/__init__.py /app/utils/engine/__init__.py
COPY utils/engine/state/base.py /app/utils/engine/state/base.py
COPY utils/engine/monitoring/service_health.py /app/utils/engine/monitoring/service_health.py
COPY utils/engine/adapters/__init__.py /app/utils/engine/adapters/__init__.py
COPY utils/engine/adapters/binance_public.py /app/utils/engine/adapters/binance_public.py
COPY utils/engine/adapters/lighter.py /app/utils/engine/adapters/lighter.py
COPY utils/engine/adapters/lighter_public.py /app/utils/engine/adapters/lighter_public.py
COPY utils/engine/deploy/market_feed/binance.json /app/config/
COPY utils/engine/deploy/market_feed/lighter.json /app/config/
USER collector
ENTRYPOINT ["python", "-m", "utils.engine.collectors.market_data.run"]
