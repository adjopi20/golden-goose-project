FROM python:3.13.7-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir numpy==2.4.4 pandas==3.0.2 requests==2.33.1 websockets==16.0 tzdata==2026.1
COPY utils/__init__.py /app/utils/__init__.py
COPY indicator/volume_profile.py /app/indicator/volume_profile.py
COPY utils/engine/execution/paper_manager.py /app/utils/engine/execution/paper_manager.py
COPY utils/engine/state/base.py /app/utils/engine/state/base.py
COPY utils/engine/adapters/binance_public.py /app/utils/engine/adapters/binance_public.py
COPY utils/engine/adapters/paper_execution.py /app/utils/engine/adapters/paper_execution.py
COPY model_development/models/mean_reversion_by_volume_profile/paper_v33/ /app/models/mean_reversion_by_volume_profile/paper_v33/
RUN python -m unittest discover -s model_development/models/mean_reversion_by_volume_profile/paper_v33 -p test_worker.py
RUN useradd --uid 10001 --create-home paper && mkdir /data && chown paper:paper /data
USER paper
ENTRYPOINT ["python", "-m", "model_development.models.mean_reversion_by_volume_profile.paper_v33.worker"]
CMD ["--database", "/data/ltcusdc.sqlite", "--action", "run"]
