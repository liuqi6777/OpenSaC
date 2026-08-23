FROM python:3.12.12-slim

ARG OPENSAC_SANDBOX_CONTRACT=11
ARG OPENSAC_VERSION=0.0.0
ENV OPENSAC_SANDBOX_CONTRACT=$OPENSAC_SANDBOX_CONTRACT
LABEL org.opensac.sandbox.contract=$OPENSAC_SANDBOX_CONTRACT
LABEL org.opencontainers.image.version=$OPENSAC_VERSION

RUN groupadd --gid 10001 opensac \
    && useradd --uid 10001 --gid opensac --create-home opensac

ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""
ENV PIP_INDEX_URL=$PIP_INDEX_URL
ENV PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST

COPY packages/opensac-sdk /opt/opensac-sdk
COPY sandbox/entrypoint.py /opt/runner/entrypoint.py
RUN pip install --no-cache-dir /opt/opensac-sdk \
    && rm -rf /opt/opensac-sdk

USER opensac
WORKDIR /workspace
ENTRYPOINT ["python", "-I", "/opt/runner/entrypoint.py"]
