import type {
  SubprocessHandle,
  SubprocessOutcome,
  SubprocessSpawnSpec,
} from '@deepseek-ai/dsh-subprocess'

const DIAGNOSTIC_MAX_CHARS = 4_000

/** Subprocess surface used by the OpenSAC bridge. Kept structural for focused tests. */
export interface SubprocessSpawner {
  spawn(spec: SubprocessSpawnSpec): SubprocessHandle
}

/** Fully resolved values needed for one `opensac agent-run` invocation. */
export interface OpenSacRunConfig {
  executable: string
  apiBase: string
  leaseSeconds: number
  cwd: string
  maxOutputBytes: number
  graceMs: number
  stateDir?: string
}

/** Per-call values that must never be persisted in plugin configuration. */
export interface OpenSacInvocation {
  code: string
  agentId: string
  signal: AbortSignal
  apiKey?: string
}

/** Stable error class for failures owned by the dsh-to-OpenSAC bridge. */
export class OpenSacBridgeError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(`[sac_run] bridge_error: ${message}`, options)
    this.name = 'OpenSacBridgeError'
  }
}

function redactDiagnostic(value: string, apiKey: string | undefined): string {
  let text = value.trim()
  if (apiKey !== undefined && apiKey.length > 0) {
    text = text.replaceAll(apiKey, '[REDACTED]')
  }
  if (text.length > DIAGNOSTIC_MAX_CHARS) {
    return `${text.slice(0, DIAGNOSTIC_MAX_CHARS)}…`
  }
  return text
}

function errorMessage(error: unknown, apiKey: string | undefined): string {
  const message = error instanceof Error ? error.message : String(error)
  return redactDiagnostic(message, apiKey)
}

function outcomeLabel(outcome: SubprocessOutcome): string {
  if (outcome.exitCode !== null) return `exit code ${outcome.exitCode}`
  if (outcome.signal !== null) return `signal ${outcome.signal}`
  return 'an unknown process status'
}

function withoutFinalNewlines(value: string): string {
  return value.replace(/(?:\r?\n)+$/u, '')
}

function explicitEnvironment(
  config: OpenSacRunConfig,
  invocation: OpenSacInvocation,
): NodeJS.ProcessEnv {
  return {
    SAC_AGENT_CONTEXT_ID: invocation.agentId,
    SAC_AGENT_HOST: 'dsh',
    SAC_API_BASE: config.apiBase,
    SAC_CLI_LEASE_SECONDS: String(config.leaseSeconds),
    ...(config.stateDir === undefined ? {} : { SAC_CLI_STATE_DIR: config.stateDir }),
    ...(invocation.apiKey === undefined ? {} : { SAC_API_KEY: invocation.apiKey }),
  }
}

async function settleProcess(
  handle: SubprocessHandle,
  invocation: OpenSacInvocation,
): Promise<SubprocessOutcome> {
  let outcome: SubprocessOutcome | undefined
  let spawnFailure: unknown
  try {
    outcome = await handle.done
  } catch (error: unknown) {
    spawnFailure = error
  }

  const exited = await handle.waitForExit()
  invocation.signal.throwIfAborted()
  if (!exited) {
    throw new OpenSacBridgeError('the OpenSAC process tree did not reach quiescence')
  }
  if (spawnFailure !== undefined) {
    throw new OpenSacBridgeError(
      'could not start the configured OpenSAC executable: '
        + errorMessage(spawnFailure, invocation.apiKey),
      { cause: spawnFailure },
    )
  }
  if (outcome === undefined) {
    throw new OpenSacBridgeError('the OpenSAC process completed without an outcome')
  }
  return outcome
}

/**
 * Execute one Python Search-as-Code program through the existing OpenSAC CLI adapter.
 * The subprocess service owns environment scrubbing and tree-scoped cancellation.
 */
export async function runOpenSac(
  subprocess: SubprocessSpawner,
  config: OpenSacRunConfig,
  invocation: OpenSacInvocation,
): Promise<string> {
  invocation.signal.throwIfAborted()

  let handle: SubprocessHandle
  try {
    handle = subprocess.spawn({
      argv: [config.executable, 'agent-run'],
      cwd: config.cwd,
      stdio: {
        stdin: { data: invocation.code },
        stdout: { maxBytes: config.maxOutputBytes },
        stderr: { maxBytes: config.maxOutputBytes },
      },
      graceMs: config.graceMs,
      signal: invocation.signal,
      env: explicitEnvironment(config, invocation),
    })
  } catch (error: unknown) {
    invocation.signal.throwIfAborted()
    throw new OpenSacBridgeError(
      'could not spawn the configured OpenSAC executable: '
        + errorMessage(error, invocation.apiKey),
      { cause: error },
    )
  }

  const outcome = await settleProcess(handle, invocation)
  const stdout = handle.collected.stdout?.readFrom(0)
  const stderr = handle.collected.stderr?.readFrom(0)
  if (stdout === undefined || stderr === undefined) {
    throw new OpenSacBridgeError('the subprocess provider did not return collected output')
  }
  if (stdout.lossy || stderr.lossy) {
    throw new OpenSacBridgeError(
      `OpenSAC output exceeded the configured ${config.maxOutputBytes}-byte stream limit`,
    )
  }

  if (outcome.exitCode !== 0) {
    const diagnostic = redactDiagnostic(stderr.text, invocation.apiKey)
    throw new OpenSacBridgeError(
      `OpenSAC CLI ended with ${outcomeLabel(outcome)}`
        + (diagnostic === '' ? '' : `: ${diagnostic}`),
    )
  }

  const observation = withoutFinalNewlines(stdout.text)
  if (observation.trim().length === 0) {
    const diagnostic = redactDiagnostic(stderr.text, invocation.apiKey)
    throw new OpenSacBridgeError(
      `OpenSAC CLI returned no observation${diagnostic === '' ? '' : `: ${diagnostic}`}`,
    )
  }
  return observation
}
