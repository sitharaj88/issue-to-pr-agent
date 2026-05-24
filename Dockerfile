# syntax=docker/dockerfile:1
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir .

FROM python:3.11-slim

LABEL org.opencontainers.image.title="issue-to-pr-agent" \
      org.opencontainers.image.description="A product-oriented GitHub issue-to-PR agent" \
      org.opencontainers.image.version="0.1.0"

RUN groupadd --gid 1000 agent && useradd --uid 1000 --gid agent --create-home agent

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/issue-to-pr /usr/local/bin/issue-to-pr
COPY --from=builder /usr/local/bin/issue-to-pr-api /usr/local/bin/issue-to-pr-api

RUN mkdir -p /data && chown agent:agent /data
ENV ISSUE_TO_PR_ARTIFACT_DIR=/data

USER agent
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

CMD ["issue-to-pr-api", "--host", "0.0.0.0", "--port", "8080"]
