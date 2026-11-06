# Test image for the Sendspin output module (python side).
# Build from backend/:  docker build -f tests/sendspin/Dockerfile -t am-party-sendspin-test:local .
FROM python:3.14-slim-trixie
COPY am-party/player/SENDSPIN_REQUIREMENTS.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt pytest==8.4.2 pytest-asyncio==1.2.0
WORKDIR /app
