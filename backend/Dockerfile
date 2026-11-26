FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Runtime data (SQLite, conversation checkpoints, lead memory) goes on a mounted disk.
# The defaults below point there; the folder also works without a disk, but is wiped on each deploy.
ENV DB_PATH=/var/data/real_estate_data.db \
    CHECKPOINT_DB=/var/data/checkpoints.db \
    MEMORY_DIR=/var/data/memory
RUN mkdir -p /var/data && useradd --create-home appuser && chown -R appuser /app /var/data
USER appuser

EXPOSE 8000
# One worker on purpose: the routine scheduler and per-conversation locks live in this process.
CMD ["sh", "-c", "uvicorn app_whatsapp.server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
