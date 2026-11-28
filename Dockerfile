# Multi-stage build for frontend and backend
FROM --platform=$BUILDPLATFORM node:22-alpine@sha256:1314fb30ec37592d5845e5a160d522c0e680972aa534549940b8d73bde88481a AS frontend-builder

# Install git for version tag detection
RUN apk add --no-cache git

WORKDIR /app/frontend
COPY frontend/package*.json ./
COPY frontend/vite.config.js ./
COPY frontend/index.html ./
COPY frontend/.env ./
COPY frontend/src/ ./src/
# Build with empty VITE_API_BASE_URL by default so the SPA uses relative /api/v1/...
# URLs (same-origin) in Docker/Kubernetes behind any port or reverse proxy.
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm ci
RUN npm run build

# Main application container
FROM python:3.14-slim@sha256:f57c4c23b985d95deea5bc5249e2f9c8a512b4cc9d5d3fd31900cbd2f5f0a604

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    procps \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libgtk-3-0 \
    libgbm1 \
    libasound2 \
    libglib2.0-0 \
    libgobject-2.0-0 \
    libnspr4 \
    libgio-2.0-0 \
    libdbus-1-3 \
    libatk1.0-0 \
    libcups2 \
    libexpat1 \
    libxcb1 \
    libatspi2.0-0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libcairo2 \
    libpango-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy Python requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade setuptools \
    && pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip wheel \
    && rm -rf /usr/local/lib/python3.14/ensurepip

# Install Playwright browsers into a shared path readable by non-root user
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium && chmod -R a+rX /ms-playwright

# Copy application code (secrets/dev files excluded via root .dockerignore)
COPY . .
# Remove config.yaml (mounted as volume) and raw frontend sources (only dist is needed)
RUN rm -rf /app/config.yaml /app/frontend

# Copy built frontend from builder stage
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# Create necessary directories and dedicated non-root user (UID/GID 1000)
RUN groupadd -r -g 1000 appuser \
    && useradd -r -u 1000 -g appuser -m -d /home/appuser appuser \
    && mkdir -p data projects logs \
    && chown -R appuser:appuser /app

# Bind on all interfaces inside the container so port forwarding / K8s Services work
ENV FLASK_HOST=0.0.0.0
ENV FLASK_PORT=8002

USER appuser

# Expose port
EXPOSE 8002

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8002/api/v1/health || exit 1

# Run the Flask application via Gunicorn (1 worker so APScheduler runs once, 4 threads for concurrency)
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8002", "--timeout", "120", "wsgi:app"]
