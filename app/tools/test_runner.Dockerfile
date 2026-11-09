FROM python:3.11-slim

RUN python -m pip install --no-cache-dir pytest==8.4.2 pytest-timeout==2.4.0

WORKDIR /workspace
USER 65534:65534
