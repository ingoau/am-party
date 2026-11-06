# syntax=docker/dockerfile:1
# Base images pinned by version and digest (bump both together).
ARG PYTHON_IMAGE=python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d
ARG NODE_IMAGE=node:22.23.4-alpine@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202

FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# Python dependencies in a venv; only the venv is copied into the runtime image.
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 TORCH_HOME=/opt/torch
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
    # C++ headers / cmake files are only needed to build torch extensions
    && rm -rf /opt/venv/lib/python*/site-packages/torch/include \
              /opt/venv/lib/python*/site-packages/torch/share/cmake
# beat_this downloads its checkpoint lazily; bake it into the image
RUN python -c "from beat_this.inference import load_checkpoint; load_checkpoint('small0')"

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    TORCH_HOME=/opt/torch \
    ENCORE_DATA_DIR=/data \
    ENCORE_STATIC_DIR=/app/static
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/torch /opt/torch
WORKDIR /app
RUN useradd --uid 1000 --create-home encore && mkdir -p /data && chown encore:encore /data
COPY docker/entrypoint.sh /usr/local/bin/encore-entrypoint
COPY backend/encore ./encore
COPY --from=web /web/dist ./static
EXPOSE 8930
# Starts as root only to fix ownership of a root-owned /data, then drops to uid 1000.
ENTRYPOINT ["encore-entrypoint"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8930/api/state', timeout=4)" || exit 1
CMD ["python", "-m", "encore"]

# Test image: docker build --target test -t encore:test . && docker run --rm encore:test
FROM runtime AS test
COPY backend/requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt && rm /tmp/requirements-dev.txt
COPY backend/tests ./tests
RUN chown -R encore:encore /app/tests
ENV PYTHONPYCACHEPREFIX=/tmp/pyc
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning", "tests"]

# Default target: the app
FROM runtime AS app
