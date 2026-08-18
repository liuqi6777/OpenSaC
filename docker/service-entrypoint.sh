#!/bin/sh
set -eu

case "${OPENSAC_DATA_DIR:-}" in
    /*) ;;
    *)
        echo "OPENSAC_DATA_DIR must be an absolute path" >&2
        exit 64
        ;;
esac

case "${OPENSAC_BROKER_SOCKET:-}" in
    /*) ;;
    *)
        echo "OPENSAC_BROKER_SOCKET must be an absolute path" >&2
        exit 64
        ;;
esac

mkdir -p "${HOME}" "${OPENSAC_DATA_DIR}" "$(dirname "${OPENSAC_BROKER_SOCKET}")"

if [ ! -w "${OPENSAC_DATA_DIR}" ]; then
    echo "OpenSAC data directory is not writable by uid=$(id -u) gid=$(id -g)" >&2
    exit 73
fi

if [ ! -S /var/run/docker.sock ]; then
    echo "Docker socket is not mounted at /var/run/docker.sock" >&2
    exit 69
fi

if ! docker info >/dev/null 2>&1; then
    echo "OpenSAC cannot access the Docker daemon; check OPENSAC_DOCKER_GID" >&2
    exit 77
fi

if [ -z "${OPENSAC_SANDBOX_DOCKER_HOST_PLATFORM:-}" ]; then
    docker_operating_system="$(docker info --format '{{.OperatingSystem}}')"
    case "${docker_operating_system}" in
        *"Docker Desktop"*)
            export OPENSAC_SANDBOX_DOCKER_HOST_PLATFORM=darwin
            ;;
        *)
            export OPENSAC_SANDBOX_DOCKER_HOST_PLATFORM=linux
            ;;
    esac
fi

exec "$@"
