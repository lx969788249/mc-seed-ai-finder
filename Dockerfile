FROM python:3.11-slim-bookworm AS builder

ARG CUBIOMES_COMMIT=e61f90580cbdd883214a8054670dacae655e59c0

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt Makefile ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r requirements.txt

COPY . .

# A GitHub source archive does not expand submodules. Fetch the pinned
# dependency in that case; normal recursive clones reuse the copied sources.
RUN if [ ! -f vendor/cubiomes/finders.h ]; then \
        rm -rf vendor/cubiomes \
        && git clone https://github.com/Cubitect/cubiomes.git vendor/cubiomes \
        && git -C vendor/cubiomes checkout "${CUBIOMES_COMMIT}"; \
    fi \
    && make native

FROM python:3.11-slim-bookworm AS runtime

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCFINDER_DB=/app/data/app.sqlite3

RUN addgroup --system app \
    && adduser --system --ingroup app app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/backend ./backend
COPY --from=builder /build/frontend ./frontend
COPY --from=builder /build/native/mc_query ./native/mc_query
COPY docker-entrypoint.sh ./docker-entrypoint.sh

RUN mkdir -p /app/data \
    && chmod 755 /app/docker-entrypoint.sh \
    && chown -R app:app /app

USER app

VOLUME ["/app/data"]
EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)"

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
