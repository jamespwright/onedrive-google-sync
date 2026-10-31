FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/config

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY odgsync ./odgsync
COPY config.example.yaml .

VOLUME /config
ENTRYPOINT ["python", "-m", "odgsync"]
# One sync, then exit. Use "run" instead to keep syncing on a loop.
CMD ["sync"]
