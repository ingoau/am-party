# syntax=docker/dockerfile:1
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.14-slim-trixie
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TORCH_HOME=/opt/torch \
    ENCORE_DATA_DIR=/data \
    ENCORE_STATIC_DIR=/app/static
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# beat_this downloads its checkpoint lazily; bake it into the image
RUN python -c "from beat_this.inference import load_checkpoint; load_checkpoint('small0')" \
    && chmod -R a+rX /opt/torch
COPY backend/encore ./encore
COPY --from=web /web/dist ./static
RUN useradd --uid 1000 --create-home encore && mkdir -p /data && chown encore /data
USER encore
EXPOSE 8930
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8930/api/state', timeout=4)" || exit 1
CMD ["python", "-m", "encore"]
