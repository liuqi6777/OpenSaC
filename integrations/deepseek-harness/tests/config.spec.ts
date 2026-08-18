import { describe, expect, it } from 'vitest'

import {
  DEFAULT_API_BASE,
  DEFAULT_API_KEY_ENV,
  DEFAULT_COMMAND,
  DEFAULT_GRACE_MS,
  DEFAULT_LEASE_SECONDS,
  DEFAULT_MAX_OUTPUT_BYTES,
  DEFAULT_TIMEOUT_MS,
  resolveConfig,
} from '../src/index.js'

describe('resolveConfig', () => {
  it('fills safe defaults', () => {
    expect(resolveConfig({})).toMatchObject({
      command: DEFAULT_COMMAND,
      apiBase: DEFAULT_API_BASE,
      apiKeyEnv: DEFAULT_API_KEY_ENV,
      leaseSeconds: DEFAULT_LEASE_SECONDS,
      timeoutMs: DEFAULT_TIMEOUT_MS,
      maxOutputBytes: DEFAULT_MAX_OUTPUT_BYTES,
      graceMs: DEFAULT_GRACE_MS,
    })
  })

  it.each([
    ['ftp://example.com', 'must use http or https'],
    ['https://user:password@example.com', 'must not contain credentials'],
    ['https://example.com?token=value', 'must not contain a query string or fragment'],
  ])('rejects unsafe API base %s', (value, message) => {
    expect(() => resolveConfig({ apiBase: value })).toThrow(message)
  })

  it('rejects an invalid lease before the tool is registered', () => {
    expect(() => resolveConfig({ leaseSeconds: 86_401 })).toThrow('leaseSeconds')
  })
})
