# syntax=docker/dockerfile:1

FROM python:3.13-slim-trixie AS runtime

# Client tools must be at least as new as the servers they back up.
ARG PG_MAJOR=18
ARG MONGO_TOOLS_VERSION=100.19.1
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/usr/lib/postgresql/${PG_MAJOR}/bin:$PATH \
    DBBACKUP_CONFIG=/etc/dbbackup/config.yaml

RUN set -eux; \
    if [ "${TARGETARCH:-amd64}" != "amd64" ]; then \
        echo "MongoDB Database Tools are only published for Debian on x86_64" >&2; exit 1; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl zstd mariadb-client; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client-${PG_MAJOR}; \
    curl -fsSL -o /tmp/mongodb-database-tools.deb \
        "https://fastdl.mongodb.org/tools/db/mongodb-database-tools-debian13-x86_64-${MONGO_TOOLS_VERSION}.deb"; \
    apt-get install -y --no-install-recommends /tmp/mongodb-database-tools.deb; \
    rm /tmp/mongodb-database-tools.deb; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*; \
    pg_dump --version; mariadb-dump --version; mongodump --version; zstd --version

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --user-group --home-dir /nonexistent --shell /usr/sbin/nologin dbbackup

USER 10001
ENTRYPOINT ["dbbackup"]
CMD ["backup"]


# Integration-test image: the runtime plus pytest and the tests.
FROM runtime AS test
USER root
RUN pip install --no-cache-dir "pytest>=8"
COPY tests ./tests
USER 10001
ENTRYPOINT []
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider", "-m", "integration", "-v", "tests/integration"]
