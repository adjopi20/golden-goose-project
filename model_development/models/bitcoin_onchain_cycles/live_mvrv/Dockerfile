FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && useradd --create-home --uid 1000 monitor \
    && mkdir /data && chown monitor:monitor /data
COPY . /app/live_mvrv/
USER monitor
ENTRYPOINT ["python", "-m", "live_mvrv.main"]
CMD ["run"]
