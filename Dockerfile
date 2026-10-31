FROM python:3.12-slim

# tzdata so TZ (which Unraid passes to every container) sets the schedule's time zone.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/config

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY odgsync ./odgsync
COPY config.example.yaml .

VOLUME /config
EXPOSE 8765
ENTRYPOINT ["python", "-m", "odgsync"]
# Web UI plus a daily scheduled sync. Use "sync" instead to sync once and exit.
CMD ["serve"]
