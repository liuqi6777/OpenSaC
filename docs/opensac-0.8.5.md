# OpenSAC v0.8.5

OpenSAC v0.8.5 repairs broker communication for Docker Compose deployments on Docker Desktop for
macOS. Sandboxes now mount the broker socket's dedicated parent directory instead of Docker
Desktop's single-file Unix-socket forwarder, which accepted connections but did not relay HTTP
responses when the socket was created by the service container.

Generated-program APIs and broker operations are unchanged. Capability contract 15 and sandbox
contract 14 remain unchanged.

## Docker Desktop broker transport

On Darwin Docker hosts, OpenSAC now mounts the broker directory read-only at `/run/opensac` and
points `OPENSAC_BROKER_SOCKET` at the configured socket filename inside that directory. Linux keeps
the narrower single-file bind mount.

Long configured socket paths are shortened into a dedicated temporary directory. This keeps the
Darwin directory mount isolated instead of exposing the host's shared temporary directory.

## Secure socket directory

Darwin deployments must place `storage.broker_socket` in a dedicated subdirectory of
`storage.data_dir`. OpenSAC rejects configurations that put the socket directly in the data root,
because mounting that directory would expose all stored session workspaces to generated programs.

The release profiles now use:

```yaml
storage:
  data_dir: /absolute/path/to/opensac-data
  broker_socket: /absolute/path/to/opensac-data/broker/broker.sock
```

The sandbox receives the `broker` directory read-only. Session directories, tombstones, logs, and
other runtime data remain outside its mount namespace.

## Migration

Before upgrading a Docker Desktop deployment, drain or close active sessions. Move
`storage.broker_socket` from the data root into a dedicated child directory, then deploy matching
v0.8.5 service and sandbox image tags. The broker socket is ephemeral and does not need to be moved
on disk; OpenSAC creates the new directory and socket during startup.

Linux deployments can retain an existing socket location, but adopting the release profile keeps
one configuration portable across Linux and Docker Desktop. No stored execution records or
generated programs require migration.
