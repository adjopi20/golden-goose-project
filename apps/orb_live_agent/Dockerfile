FROM python:3.13-slim

WORKDIR /app

COPY apps/orb_live_agent/requirements.txt /tmp/orb-live-agent-requirements.txt
RUN pip install --no-cache-dir -r /tmp/orb-live-agent-requirements.txt

COPY apps/orb_live_agent /app/apps/orb_live_agent
COPY indicator /app/indicator

ENV PYTHONPATH=/app:/app/apps/orb_live_agent/src
CMD ["python", "-m", "orb_live_agent.main"]
