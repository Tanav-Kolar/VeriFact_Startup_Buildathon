FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY services/x_backfill/requirements.txt /app/services/x_backfill/requirements.txt
RUN pip install --no-cache-dir -r /app/services/x_backfill/requirements.txt

COPY verifact_core /app/verifact_core
COPY services/x_backfill /app/services/x_backfill
COPY events /app/events

ENTRYPOINT ["python", "-m", "services.x_backfill.main"]
