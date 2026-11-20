FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY services/fetch_pages/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY verifact_core /app/verifact_core
COPY services /app/services

ENTRYPOINT ["python", "-m", "services.fetch_pages.main"]
