FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py ./

# Create data directory for SQLite DB
RUN mkdir -p /data

# Environment defaults (override in docker-compose or .env)
ENV DB_PATH=/data/watchlist.db
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
