# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.12-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.33
ARG DOCKER_CLI_IMAGE=docker:29.7.2-cli

FROM ${UV_IMAGE} AS uv
FROM ${DOCKER_CLI_IMAGE} AS docker-cli

FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /uvx /bin/
WORKDIR /build

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY dashboard ./dashboard
COPY sac_agent ./sac_agent
COPY packages/opensac-sdk ./packages/opensac-sdk

RUN uv build --all-packages --wheel --out-dir /dist

FROM ${PYTHON_IMAGE}

ARG OPENSAC_VERSION=0.0.0
LABEL org.opencontainers.image.title="OpenSAC"
LABEL org.opencontainers.image.description="OpenSAC API and capability broker"
LABEL org.opencontainers.image.version=$OPENSAC_VERSION

ENV HOME=/tmp/opensac-home \
    PATH=/usr/local/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 opensac \
    && useradd --uid 10001 --gid opensac --no-create-home opensac

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=builder /dist /dist
RUN set -- /dist/opensac-*.whl; \
    python -m pip install --no-cache-dir "$1[llm]" \
    && rm -rf /dist

COPY docker/service-entrypoint.sh /usr/local/bin/opensac-container-entrypoint
RUN chmod 0755 /usr/local/bin/opensac-container-entrypoint

WORKDIR /opt/opensac
USER opensac
EXPOSE 8000
STOPSIGNAL SIGTERM

ENTRYPOINT ["opensac-container-entrypoint"]
CMD ["opensac", "serve"]
