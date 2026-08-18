import { readFile } from 'node:fs/promises'
import { isAbsolute, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import type { Context } from '@deepseek-ai/cordis'
import { credentialRef } from '@deepseek-ai/dsh-credentials'
import type {} from '@deepseek-ai/dsh-skill'
import type {} from '@deepseek-ai/dsh-subprocess'
import { defineTool } from '@deepseek-ai/dsh-tools'
import z from '@deepseek-ai/schemastery'

import { runOpenSac } from './runner.js'
import type { OpenSacRunConfig } from './runner.js'

export { OpenSacBridgeError, runOpenSac } from './runner.js'
export type {
  OpenSacInvocation,
  OpenSacRunConfig,
  SubprocessSpawner,
} from './runner.js'

/** Cordis plugin name used by dsh loader diagnostics. */
export const name = 'opensac-dsh'

/** Services required for the native tool, managed process, credential, and bundled skill. */
export const inject = ['tools', 'subprocess', 'credentials', 'skills']

export const DEFAULT_API_BASE = 'http://127.0.0.1:8000'
export const DEFAULT_API_KEY_ENV = 'SAC_API_KEY'
export const DEFAULT_COMMAND = 'opensac'
export const DEFAULT_GRACE_MS = 1_000
export const DEFAULT_LEASE_SECONDS = 3_600
export const DEFAULT_MAX_OUTPUT_BYTES = 262_144
export const DEFAULT_TIMEOUT_MS = 310_000
const MAX_LEASE_SECONDS = 86_400
const MAX_TIMER_DELAY_MS = 2_147_483_647
const SKILL_NAME = 'search-as-code-dsh'
const SKILL_DESCRIPTION = 'Use OpenSAC for programmable, evidence-grounded research through '
  + 'the native sac_run tool.'
const SKILL_URL = new URL('../skills/search-as-code-dsh/SKILL.md', import.meta.url)

/** Configuration accepted by the installable dsh bundle. */
export interface Config {
  /** Executable path or bare PATH name resolved when the plugin loads. */
  command?: string
  /** OpenSAC service URL passed to the CLI adapter. */
  apiBase?: string
  /** dsh credential reference resolved afresh for every call. */
  apiKeyEnv?: string
  /** Renewable OpenSAC session lease in seconds. */
  leaseSeconds?: number
  /** Optional CLI SQLite registry directory. */
  stateDir?: string
  /** Working directory used to launch `opensac agent-run`. */
  cwd?: string
  /** Cooperative dsh tool timeout. */
  timeoutMs?: number
  /** Per-stream stdout/stderr retention bound. */
  maxOutputBytes?: number
  /** SIGTERM-to-SIGKILL subprocess grace period. */
  graceMs?: number
}

export const Config: z<Config> = z.object({
  command: z.string().default(DEFAULT_COMMAND),
  apiBase: z.string().default(DEFAULT_API_BASE),
  apiKeyEnv: z.string().role('credential-ref').default(DEFAULT_API_KEY_ENV),
  leaseSeconds: z.number().step(1).min(1).max(MAX_LEASE_SECONDS).default(DEFAULT_LEASE_SECONDS),
  stateDir: z.string(),
  cwd: z.string(),
  timeoutMs: z.number().step(1).min(1).max(MAX_TIMER_DELAY_MS).default(DEFAULT_TIMEOUT_MS),
  maxOutputBytes: z.number().step(1).min(1).default(DEFAULT_MAX_OUTPUT_BYTES),
  graceMs: z.number().step(1).min(1).max(MAX_TIMER_DELAY_MS).default(DEFAULT_GRACE_MS),
})

/** Configuration after defaults, normalization, and fail-fast validation. */
export interface ResolvedConfig {
  command: string
  apiBase: string
  apiKeyEnv: string
  leaseSeconds: number
  cwd: string
  timeoutMs: number
  maxOutputBytes: number
  graceMs: number
  stateDir?: string
}

function positiveInteger(name: string, value: number, max = Number.MAX_SAFE_INTEGER): number {
  if (!Number.isInteger(value) || value < 1 || value > max) {
    throw new Error(`opensac-dsh: ${name} must be an integer from 1 to ${max}`)
  }
  return value
}

function nonEmpty(name: string, value: string): string {
  const normalized = value.trim()
  if (normalized === '') throw new Error(`opensac-dsh: ${name} must be non-empty`)
  return normalized
}

function apiBase(value: string): string {
  const normalized = nonEmpty('apiBase', value)
  let parsed: URL
  try {
    parsed = new URL(normalized)
  } catch (error: unknown) {
    throw new Error('opensac-dsh: apiBase must be an absolute HTTP(S) URL', { cause: error })
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('opensac-dsh: apiBase must use http or https')
  }
  if (parsed.username !== '' || parsed.password !== '') {
    throw new Error('opensac-dsh: apiBase must not contain credentials')
  }
  if (parsed.search !== '' || parsed.hash !== '') {
    throw new Error('opensac-dsh: apiBase must not contain a query string or fragment')
  }
  return normalized
}

/** Apply defaults even in direct tests, then reject invalid values before registration. */
export function resolveConfig(config: Config): ResolvedConfig {
  const configuredCwd = nonEmpty('cwd', config.cwd ?? process.cwd())
  const stateDir = config.stateDir?.trim()
  return {
    command: nonEmpty('command', config.command ?? DEFAULT_COMMAND),
    apiBase: apiBase(config.apiBase ?? DEFAULT_API_BASE),
    apiKeyEnv: nonEmpty('apiKeyEnv', config.apiKeyEnv ?? DEFAULT_API_KEY_ENV),
    leaseSeconds: positiveInteger(
      'leaseSeconds',
      config.leaseSeconds ?? DEFAULT_LEASE_SECONDS,
      MAX_LEASE_SECONDS,
    ),
    cwd: isAbsolute(configuredCwd) ? configuredCwd : resolve(configuredCwd),
    timeoutMs: positiveInteger(
      'timeoutMs',
      config.timeoutMs ?? DEFAULT_TIMEOUT_MS,
      MAX_TIMER_DELAY_MS,
    ),
    maxOutputBytes: positiveInteger(
      'maxOutputBytes',
      config.maxOutputBytes ?? DEFAULT_MAX_OUTPUT_BYTES,
    ),
    graceMs: positiveInteger('graceMs', config.graceMs ?? DEFAULT_GRACE_MS, MAX_TIMER_DELAY_MS),
    ...(stateDir === undefined || stateDir === '' ? {} : { stateDir }),
  }
}

/** Register the exclusive `sac_run` tool and its dsh-specific usage skill. */
export async function apply(ctx: Context, config: Config): Promise<void> {
  const resolved = resolveConfig(config)
  const apiKeyReference = credentialRef(resolved.apiKeyEnv)
  const setupAbort = new AbortController()
  const stopSetupCancellation = ctx.on('internal/plugin', (fiber) => {
    if (fiber === ctx.fiber && fiber.uid === null) {
      setupAbort.abort(new Error('opensac-dsh setup disposed'))
    }
  })

  try {
    const executable = await ctx.subprocess.resolveExecutable(
      resolved.command,
      undefined,
      setupAbort.signal,
    )
    const runConfig: OpenSacRunConfig = {
      executable,
      apiBase: resolved.apiBase,
      leaseSeconds: resolved.leaseSeconds,
      cwd: resolved.cwd,
      maxOutputBytes: resolved.maxOutputBytes,
      graceMs: resolved.graceMs,
      ...(resolved.stateDir === undefined ? {} : { stateDir: resolved.stateDir }),
    }
    const skillContent = await readFile(SKILL_URL, {
      encoding: 'utf8',
      signal: setupAbort.signal,
    })
    setupAbort.signal.throwIfAborted()

    ctx.skills.register({
      name: SKILL_NAME,
      description: SKILL_DESCRIPTION,
      source: 'bundled',
      provider: name,
      content: skillContent,
      path: fileURLToPath(SKILL_URL),
      resourceBase: {
        kind: 'directory',
        path: fileURLToPath(new URL('../skills/search-as-code-dsh/', import.meta.url)),
      },
      invocation: {
        modelInvocable: true,
        userInvocable: true,
      },
    })

    ctx.tools.register(defineTool({
      name: 'sac_run',
      description: 'Execute a Python Search-as-Code program in this dsh agent\'s persistent '
        + 'OpenSAC session.',
      parameters: {
        code: {
          type: 'string',
          required: true,
          description: 'Complete Python program using opensac_sdk.',
        },
      },
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: value }],
      },
      timeoutMs: resolved.timeoutMs,
      async execute(args, exec) {
        if (exec.agent === undefined) {
          throw new Error('[sac_run] dsh agent context is unavailable')
        }
        if (args.code.trim() === '') {
          throw new Error('[sac_run] expected a non-empty Python program')
        }
        exec.signal.throwIfAborted()
        const credential = await ctx.credentials.resolve(apiKeyReference)
        exec.signal.throwIfAborted()
        const resolvedApiKey = credential?.value
        return runOpenSac(ctx.subprocess, runConfig, {
          code: args.code,
          agentId: String(exec.agent.id),
          signal: exec.signal,
          ...(resolvedApiKey === undefined || resolvedApiKey.length === 0
            ? {}
            : { apiKey: resolvedApiKey }),
        })
      },
    }))
  } catch (error: unknown) {
    setupAbort.abort(error)
    throw error
  } finally {
    stopSetupCancellation()
  }
}
