FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY run.py .
COPY lidarr_hits_bot/ lidarr_hits_bot/

# Create data directory for SQLite DB
RUN mkdir -p /data

# Environment defaults
ENV DB_PATH=/data/watchlist.db
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["python", "run.py"]
