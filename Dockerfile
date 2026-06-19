# =============================================================================
# Prism — Multi-stage Dockerfile
#
# Stage 1 (builder): installs Node.js and compiles the React frontend
# Stage 2 (runtime): Python 3.11 slim image with the built frontend embedded
#
# The final image contains no Node.js, no source maps, and no dev dependencies.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1 — Frontend build
# -----------------------------------------------------------------------------
FROM node:24-slim AS frontend-builder

WORKDIR /app/frontend

# Install dependencies first (cached layer)
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --silent

# Copy source and build
COPY frontend/ ./
RUN npm run build


# -----------------------------------------------------------------------------
# Stage 2 — Python runtime
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Keeps Python from buffering stdout/stderr
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY backend/ ./backend/

# Copy compiled frontend from stage 1
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# Non-root user for security
RUN adduser --disabled-password --gecos "" prism
USER prism

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
