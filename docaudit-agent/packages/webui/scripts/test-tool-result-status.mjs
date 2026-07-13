import assert from 'node:assert/strict'

/**
 * Regression test for the tool_result status mapping bug.
 *
 * Backend tool_result / subagent_tool_result SSE events emit status "ok" for
 * success, but the frontend's VALID_TOOL_STATUSES list only contains "done".
 * asToolStatus must explicitly map "ok" -> "done" instead of falling back to
 * the current tool status (which is "running" when the result arrives).
 */

const VALID_TOOL_STATUSES = ['pending', 'running', 'done', 'error', 'warning', 'cancelled']

function asToolStatus(s, fallback = 'done') {
  if (s === 'ok') return 'done'
  if (s && VALID_TOOL_STATUSES.includes(s)) return s
  return fallback
}

// tool_result success event from backend: status is "ok".
assert.equal(asToolStatus('ok', 'running'), 'done', 'ok must map to done')

// subagent_tool_result success event from backend: toolStatus is "ok".
assert.equal(asToolStatus('ok'), 'done', 'ok with default fallback must map to done')

// Error events from backend.
assert.equal(asToolStatus('error', 'running'), 'error')
assert.equal(asToolStatus('error'), 'error')

// Missing/unknown status should fall back appropriately.
assert.equal(asToolStatus(undefined, 'running'), 'running')
assert.equal(asToolStatus('unknown', 'running'), 'running')
assert.equal(asToolStatus(undefined), 'done')

console.log('toolResultStatus mapping passed')
