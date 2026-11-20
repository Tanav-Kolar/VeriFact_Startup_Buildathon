FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

COPY services/collect_youtube/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY verifact_core /app/verifact_core
COPY services/collect_youtube /app/services/collect_youtube
COPY events /app/events

ENTRYPOINT ["python", "-m", "services.collect_youtube.main"]
