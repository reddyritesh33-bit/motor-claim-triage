# syntax=docker/dockerfile:1
# Light image: api, llm-gateway, triage-agent, coverage-agent, eval tooling.
# (No torch / model weights here - those live only in the retriever image.)
FROM python:3.11-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10
WORKDIR /app
COPY requirements/base.txt requirements/base.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements/base.txt
COPY claimtriage ./claimtriage
COPY prompts ./prompts
COPY eval/scenarios.yaml ./eval/scenarios.yaml
RUN useradd --create-home --uid 10001 app && mkdir -p /app/eval/results && chown -R app /app
USER app

# Load-test image = app + locust (compose profile "loadtest")
FROM app AS loadtest
USER root
RUN --mount=type=cache,target=/root/.cache/pip pip install locust==2.32.4
COPY loadtest ./loadtest
RUN chown -R app /app
USER app
