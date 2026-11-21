FROM python:3.12-slim

WORKDIR /app

COPY services/check/requirements.txt /app/services/check/requirements.txt
RUN pip install --no-cache-dir -r /app/services/check/requirements.txt

COPY verifact_core /app/verifact_core
COPY services/check /app/services/check
COPY events /app/events

RUN useradd --create-home app
USER app

ENV PYTHONUNBUFFERED=1 PORT=8080

CMD exec uvicorn services.check.main:create_app --factory --host 0.0.0.0 --port ${PORT}
