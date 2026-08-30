FROM ghcr.io/astral-sh/uv:0.12.7 AS uv

FROM python:3.14-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

COPY --from=uv /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl firefox-esr \
    && ln -sf /usr/bin/firefox-esr /usr/local/bin/firefox \
    && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH=amd64
ARG GECKODRIVER_VERSION=0.37.1
RUN case "${TARGETARCH}" in \
      amd64) gecko_arch="linux64" ;; \
      arm64) gecko_arch="linux-aarch64" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --silent --show-error \
         "https://github.com/mozilla/geckodriver/releases/download/v${GECKODRIVER_VERSION}/geckodriver-v${GECKODRIVER_VERSION}-${gecko_arch}.tar.gz" \
       | tar --extract --gzip --directory /usr/local/bin \
    && chmod 0755 /usr/local/bin/geckodriver

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_DEV=1 \
    PATH="/app/.venv/bin:${PATH}" \
    PORT=8080

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8080') + '/health', timeout=4).read()"

CMD ["seat-notifier", "--auto-login", "--no-dbus", "poll"]
