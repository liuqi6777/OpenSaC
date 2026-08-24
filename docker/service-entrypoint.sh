#!/bin/sh
set -eu

mkdir -p "${HOME}"

if [ ! -S /var/run/docker.sock ]; then
    echo "Docker socket is not mounted at /var/run/docker.sock" >&2
    exit 69
fi

if ! docker info >/dev/null 2>&1; then
    echo "OpenSAC cannot access the Docker daemon; check OPENSAC_DOCKER_GID" >&2
    exit 77
fi

exec "$@"
