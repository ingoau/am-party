#!/usr/bin/env bash
# Runs the sendspin-js 5 probes (Node SendspinCore x2, headless Chromium SendspinPlayer x2)
# against tests/sendspin/serve_demo.py, all inside Docker on a private network.
# usage: tests/sendspin/run_js_probes.sh   (from backend/)
set -euo pipefail
cd "$(dirname "$0")/../.."
NET=encore-sendspin-net SRV=encore-sendspin-srv JS="$PWD/tests/sendspin/js"
PW_IMAGE=${PW_IMAGE:-mcr.microsoft.com/playwright:v1.64.1-noble}
cleanup() { docker rm -f "$SRV" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker build -q --label owner=encore -f tests/sendspin/Dockerfile -t encore-sendspin-test:local . >/dev/null
docker network create --label owner=encore "$NET" >/dev/null
docker run -d --name "$SRV" --label owner=encore --network "$NET" --network-alias encore \
  -v "$PWD":/app encore-sendspin-test:local \
  python -m tests.sendspin.serve_demo --port 8080 --static tests/sendspin/js/web >/dev/null
node() { docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp --label owner=encore -v "$JS":/w -w /w "$@"; }
node node:22-slim npm install --no-audit --no-fund --silent
node node:22-slim npx esbuild core_probe.mjs --bundle --platform=node --format=esm --outfile=build/probe.mjs --log-level=warning
node node:22-slim npx esbuild page.mjs --bundle --format=esm --outfile=web/page.js --log-level=warning
sleep 2
node --network "$NET" node:22-slim node build/probe.mjs http://encore:8080 8
node --network "$NET" --ipc=host "$PW_IMAGE" node browser_probe.mjs http://encore:8080
