FROM python:3.12-slim

WORKDIR /app

COPY services/enrich/requirements.txt /app/services/enrich/requirements.txt
RUN pip install --no-cache-dir -r /app/services/enrich/requirements.txt

COPY verifact_core /app/verifact_core
COPY services/enrich /app/services/enrich
COPY events /app/events

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "services.enrich.main"]
