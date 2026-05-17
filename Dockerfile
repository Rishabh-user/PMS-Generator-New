# syntax=docker/dockerfile:1
#
# Dockerfile for the PMS Generator backend.
#
# Why a Dockerfile instead of relying on Render's apt.txt? The /api/export/pdf
# endpoint needs LibreOffice on the host (it shells out to `soffice
# --headless --convert-to pdf`). Render's native Python runtime doesn't
# always pick up apt.txt — depends on the runtime version and plan — so
# building from this Dockerfile guarantees libreoffice-calc is present
# regardless of which Render runtime tier is selected.
#
# Build size budget: python:3.12-slim (~150 MB) + libreoffice-calc + deps
# (~250 MB) + Python wheels (~100 MB) ≈ 500 MB total image. Comfortably
# under Render's image-size limits on every paid plan.

FROM python:3.12-slim AS runtime

# ── System packages ─────────────────────────────────────────────────
# - libreoffice-calc: pulls in libreoffice-core which provides /usr/bin/
#   soffice. Calc-only avoids the ~400 MB extra weight of the full
#   `libreoffice` meta-package (Writer / Impress / Draw / Base / Math).
# - fonts-liberation + fonts-dejavu-core: web-safe fallback fonts so
#   the rendered PDF text looks the same on the server as on dev. Tiny
#   (~10 MB combined) but eliminates "missing font → ugly substitute"
#   surprises in the output.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libreoffice-calc \
        fonts-liberation \
        fonts-dejavu-core && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ── Python dependencies ─────────────────────────────────────────────
# requirements.txt is copied first so this layer is cached across code
# changes — only re-runs when the pin list itself changes.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App source ──────────────────────────────────────────────────────
# Copy everything after the pip layer so editing app code doesn't bust
# the dependency-install cache.
COPY . .

# ── Runtime config ──────────────────────────────────────────────────
# Render injects $PORT at container start; default to 8000 for local
# `docker run` testing. uvicorn binds 0.0.0.0 so requests outside the
# container reach the app. `--reload` is OFF in production — code is
# baked into the image.
ENV PORT=8000
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
