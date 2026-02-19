FROM python:3.9-slim

# Create a non-root user for security (recommended by Coolify)
RUN groupadd -r ankiplace && useradd -r -g ankiplace ankiplace

WORKDIR /app

# Install dependencies first (better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .

# Create data directory for persistence and set permissions
RUN mkdir -p /data && chown -R ankiplace:ankiplace /data /app

# Use /data/canvas.db by default in production
ENV DB_PATH=/data/canvas.db
ENV ANKIPLACE_SECRET=change-me-please

USER ankiplace

# Expose the default FastAPI port
EXPOSE 4201

# Using a single worker for SQLite is safer to avoid locking issues
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "4201", "--workers", "1"]
