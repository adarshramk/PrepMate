# ── PrepMate Dockerfile ───────────────────────────────────────────────────
# Target runtime: AWS Elastic Beanstalk Docker platform (single-instance)
# Build: docker build -t prepmate .
# Run:   docker run -p 8000:8000 \
#          -e FLASK_SECRET_KEY=<strong-random-key> \
#          -v /host/data:/data \
#          prepmate

# ── Stage: runtime image ─────────────────────────────────────────────────
FROM python:3.12-slim

# Metadata
LABEL maintainer="PrepMate"
LABEL description="AI-powered placement preparation platform"

# Prevent .pyc files and enable unbuffered stdout/stderr for CloudWatch logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ── System dependencies ───────────────────────────────────────────────────
# sqlite3 CLI is useful for debugging; libsqlite3 is already in the base image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user ─────────────────────────────────────────────────────────
RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser

# ── Application directory ─────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ────────────────────────────────────────────────────
# Copy only requirements first so this layer is cached unless deps change.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ── Application source ─────────────────────────────────────────────────────
COPY . .

# ── Persistent data directory ──────────────────────────────────────────────
# The SQLite database lives here.
# On AWS EB / EC2, mount an EBS volume at /data to persist across deployments.
# Without a volume mount the database is stored inside the container and will
# be lost when the container is replaced (e.g. eb deploy, instance replacement).
# This directory is created so the app starts without a volume in development.
RUN mkdir -p /data && chown appuser:appgroup /data

# ── Ownership ─────────────────────────────────────────────────────────────
RUN chown -R appuser:appgroup /app

USER appuser

# ── Runtime environment ───────────────────────────────────────────────────
# DATABASE_PATH points to the persistent volume mount point.
# FLASK_SECRET_KEY must be injected at runtime — never baked into this image.
# FLASK_ENV defaults to production; override to development for local testing.
ENV DATABASE_PATH=/data/prepmate.db \
    FLASK_ENV=production \
    BEDROCK_REGION=us-east-1 \
    BEDROCK_MODEL_ID=amazon.nova-micro-v1:0 \
    BEDROCK_TIMEOUT=30

EXPOSE 8000

# ── Healthcheck ───────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
    || exit 1

# ── Start command ─────────────────────────────────────────────────────────
# 2 workers is appropriate for a single t3.micro instance.
# Increase workers if deploying on a larger instance.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "wsgi:application"]
