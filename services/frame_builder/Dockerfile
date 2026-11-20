FROM python:3.12-slim

WORKDIR /app

COPY services/frame_builder/requirements.txt /app/services/frame_builder/requirements.txt
RUN pip install --no-cache-dir -r /app/services/frame_builder/requirements.txt

COPY verifact_core /app/verifact_core
COPY services/frame_builder /app/services/frame_builder
COPY events /app/events

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "services.frame_builder.main"]
