# OpenSAC v0.7.0

OpenSAC v0.7.0 adds an opt-in persistent Python interpreter for session-scoped research
experiments while keeping the existing process-per-program runtime and agent skills as the default
baseline.

## Experimental session REPL

Create a session with `execution_mode="persistent_interpreter"` after enabling
`OPENSAC_EXPERIMENTAL_PERSISTENT_INTERPRETER=true`. The session lazily starts one internal Python
kernel and keeps top-level variables, functions, imports, and assignments completed before ordinary
exceptions available to later `/exec` calls.

The existing `execution_mode="program"` remains the default. Files continue to follow
`mechanisms.persistence`: disabling filesystem persistence gives every cell a temporary workspace
without clearing Python globals.

Persistent interpreter containers are pinned until their session is deleted or expires and do not
participate in warm-container idle or LRU eviction. A timeout, output overflow, kernel exit,
protocol failure, or leftover background thread destroys the container and marks the interpreter
lost. The failed cell is never replayed, and later direct execution returns
`410 interpreter_lost`.

Execution and session responses expose the actual execution mode, interpreter state and optional
loss reason. Persistent cells also record the count of top-level user symbols without recording
their names or values.

## Agent integrations and skills

MCP and CLI adapters select the treatment mode through `SAC_MCP_EXECUTION_MODE` and
`SAC_CLI_EXECUTION_MODE`. Adapter observations report the server's actual mode, interpreter state,
and namespace size so a skill/runtime mismatch is immediately visible.

Two self-contained experimental skills are included:

- `search-as-code-repl` for MCP `sac_run(code)`;
- `search-as-code-repl-cli` for `opensac agent-run`.

Both disable implicit invocation and must be selected explicitly. They teach agents to reuse
semantic variables, end review cells with named `NEXT:` state, checkpoint only at meaningful phase
boundaries, inspect globals and usage after uncertain failures, and submit final results through
`sdk.output.submit(...)`. The existing `search-as-code` and `search-as-code-cli` skills are
unchanged for baseline experiments.

## SDK execution context

The bundled SDK now resolves the active execution credential, execution ID, workspace, output path,
and diagnostics target at call time. An SDK client imported in an earlier cell therefore follows the
current cell instead of writing through stale execution context.

## Deployment compatibility

The sandbox contract increases from `12` to `13` because the image now contains the persistent
kernel daemon and relay protocol. Deploy matching v0.7.0 service and sandbox images. Existing
sessions continue to use `program` mode unless the experimental feature and execution mode are both
enabled.
