import type {
  SubprocessHandle,
  SubprocessOutcome,
  SubprocessOutputReader,
  SubprocessSpawnSpec,
} from '@deepseek-ai/dsh-subprocess'
import { describe, expect, it, vi } from 'vitest'

import {
  OpenSacBridgeError,
  runOpenSac,
} from '../src/runner.js'
import type {
  OpenSacInvocation,
  OpenSacRunConfig,
  SubprocessSpawner,
} from '../src/runner.js'

const CONFIG: OpenSacRunConfig = {
  executable: '/usr/local/bin/opensac',
  apiBase: 'http://127.0.0.1:8000',
  leaseSeconds: 3_600,
  cwd: '/workspace',
  maxOutputBytes: 262_144,
  graceMs: 1_000,
}

function reader(text: string, lossy = false): SubprocessOutputReader {
  return {
    readFrom: vi.fn(() => ({ text, nextOffset: Buffer.byteLength(text), lossy })),
  }
}

function fakeHandle(options: {
  stdout?: string
  stderr?: string
  stdoutLossy?: boolean
  stderrLossy?: boolean
  outcome?: SubprocessOutcome
  done?: Promise<SubprocessOutcome>
  waitForExit?: () => Promise<boolean>
} = {}): SubprocessHandle {
  return {
    pid: 123,
    stdin: undefined,
    stdout: undefined,
    stderr: undefined,
    collected: {
      stdout: reader(options.stdout ?? 'observation\n', options.stdoutLossy),
      stderr: reader(options.stderr ?? '', options.stderrLossy),
    },
    done: options.done ?? Promise.resolve(options.outcome ?? { exitCode: 0, signal: null }),
    terminate: vi.fn(),
    waitForExit: vi.fn(options.waitForExit ?? (async () => true)),
  }
}

function invocation(overrides: Partial<OpenSacInvocation> = {}): OpenSacInvocation {
  return {
    code: 'from opensac_sdk import sdk\nprint(sdk.session.usage())',
    agentId: 'agent-123',
    signal: new AbortController().signal,
    ...overrides,
  }
}

describe('runOpenSac', () => {
  it('spawns agent-run without a shell and binds the dsh agent context', async () => {
    const handle = fakeHandle({ stdout: 'answer\n' })
    const spawn = vi.fn((_spec: SubprocessSpawnSpec) => handle)
    const call = invocation({ apiKey: 'secret-key' })

    await expect(runOpenSac({ spawn }, CONFIG, call)).resolves.toBe('answer')
    expect(spawn).toHaveBeenCalledWith({
      argv: ['/usr/local/bin/opensac', 'agent-run'],
      cwd: '/workspace',
      stdio: {
        stdin: { data: call.code },
        stdout: { maxBytes: 262_144 },
        stderr: { maxBytes: 262_144 },
      },
      graceMs: 1_000,
      signal: call.signal,
      env: {
        SAC_AGENT_CONTEXT_ID: 'agent-123',
        SAC_AGENT_HOST: 'dsh',
        SAC_API_BASE: 'http://127.0.0.1:8000',
        SAC_CLI_LEASE_SECONDS: '3600',
        SAC_API_KEY: 'secret-key',
      },
    })
    expect(handle.waitForExit).toHaveBeenCalledOnce()
  })

  it('omits an unavailable credential and forwards an explicit state directory', async () => {
    const spawn = vi.fn((_spec: SubprocessSpawnSpec) => fakeHandle())
    await runOpenSac(
      { spawn },
      { ...CONFIG, stateDir: '/state/opensac' },
      invocation(),
    )

    const spec = spawn.mock.calls[0]?.[0]
    expect(spec?.env).toMatchObject({ SAC_CLI_STATE_DIR: '/state/opensac' })
    expect(spec?.env).not.toHaveProperty('SAC_API_KEY')
  })

  it('fails closed on truncated output', async () => {
    const subprocess: SubprocessSpawner = {
      spawn: () => fakeHandle({ stdoutLossy: true }),
    }
    await expect(runOpenSac(subprocess, CONFIG, invocation())).rejects.toThrow(
      'output exceeded the configured 262144-byte stream limit',
    )
  })

  it('redacts the resolved credential from child diagnostics', async () => {
    const subprocess: SubprocessSpawner = {
      spawn: () => fakeHandle({
        stderr: 'request failed for secret-key',
        outcome: { exitCode: 2, signal: null },
      }),
    }
    const result = runOpenSac(subprocess, CONFIG, invocation({ apiKey: 'secret-key' }))

    await expect(result).rejects.toThrow('[REDACTED]')
    await expect(result).rejects.not.toThrow('secret-key')
  })

  it('waits for process-tree quiescence and propagates cancellation', async () => {
    const controller = new AbortController()
    let finish: ((outcome: SubprocessOutcome) => void) | undefined
    const done = new Promise<SubprocessOutcome>((resolveDone) => {
      finish = resolveDone
    })
    const handle = fakeHandle({ done })
    const result = runOpenSac(
      { spawn: () => handle },
      CONFIG,
      invocation({ signal: controller.signal }),
    )
    const reason = new Error('cancelled by agent')

    controller.abort(reason)
    finish?.({ exitCode: null, signal: 'SIGTERM' })

    await expect(result).rejects.toBe(reason)
    expect(handle.waitForExit).toHaveBeenCalledOnce()
  })

  it('uses a stable bridge error for empty successful output', async () => {
    const result = runOpenSac(
      { spawn: () => fakeHandle({ stdout: '\n', stderr: 'no observation' }) },
      CONFIG,
      invocation(),
    )
    await expect(result).rejects.toBeInstanceOf(OpenSacBridgeError)
    await expect(result).rejects.toThrow('returned no observation: no observation')
  })
})
