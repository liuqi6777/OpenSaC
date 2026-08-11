FROM python:3.12.12-slim

ARG OPENSAC_SANDBOX_CONTRACT=3
ENV OPENSAC_SANDBOX_CONTRACT=$OPENSAC_SANDBOX_CONTRACT
LABEL org.opensac.sandbox.contract=$OPENSAC_SANDBOX_CONTRACT

RUN groupadd --gid 10001 opensac \
    && useradd --uid 10001 --gid opensac --create-home opensac

COPY _sandbox_wheel /opt/opensac-sdk
COPY sandbox/entrypoint.py /opt/runner/entrypoint.py
RUN pip install --no-cache-dir /opt/opensac-sdk/*.whl \
    && rm -rf /opt/opensac-sdk

USER opensac
WORKDIR /workspace
ENTRYPOINT ["python", "-I", "/opt/runner/entrypoint.py"]
