FROM python:3.13.7-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir numpy==2.4.4 pandas==3.0.2 requests==2.33.1 websockets==16.0 tzdata==2026.1
COPY indicator/volume_profile.py /app/indicator/volume_profile.py
COPY live_engine/order_manager.py live_engine/state_store.py /app/live_engine/
COPY live_engine/adapters/binance.py /app/live_engine/adapters/binance.py
COPY trading_core/paper_execution.py /app/trading_core/paper_execution.py
COPY models/mean_reversion_by_volume_profile/paper_v33/ /app/models/mean_reversion_by_volume_profile/paper_v33/
RUN python -m unittest discover -s models/mean_reversion_by_volume_profile/paper_v33 -p test_worker.py
RUN useradd --uid 10001 --create-home paper && mkdir /data && chown paper:paper /data
USER paper
ENTRYPOINT ["python", "-m", "models.mean_reversion_by_volume_profile.paper_v33.worker"]
CMD ["--database", "/data/ltcusdc.sqlite", "--action", "run"]
