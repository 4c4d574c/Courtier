import assert from 'node:assert/strict'

/**
 * Regression test for the step-reference synchronization fix in useAgentSession.
 *
 * useAgentSession maintains two parallel step arrays:
 *   - session.steps  (source of truth for stats/export)
 *   - currentTurn.steps inside session.turns (what MainPanel/StepGroup render)
 *
 * The bug: tool-status updates used to replace the object in session.steps while
 * leaving the reference in currentTurn.steps stale, so the UI rendered stale
 * pending/running state until a new step forced a re-render.
 *
 * The fix: every place that assigns to session.steps[...] now uses replaceStepAt,
 * which updates both arrays atomically so the rendered step and the source-of-truth
 * step remain the same object reference.
 */

function createMockSession() {
  return {
    steps: [],
    turns: [{ message: { role: 'user', text: '审核文档', timestamp: 1 }, steps: [] }],
  }
}

function createStep(index, toolNames, turnIndex) {
  return {
    index,
    label: toolNames.join(', '),
    skill: '',
    tools: toolNames.map(name => ({
      id: `tool-${name}`,
      name,
      skill: '',
      status: 'pending',
      callKind: 'tool',
      callScope: 'parent',
      subagentName: null,
    })),
    turnIndex,
  }
}

// Mirrors replaceStepAt from useAgentSession.ts
function replaceStepAt(session, stepIndex, updatedStep) {
  session.steps.splice(stepIndex, 1, updatedStep)
  const turn = updatedStep.turnIndex !== undefined
    ? session.turns[updatedStep.turnIndex - 1]
    : undefined
  if (turn) {
    const turnStepIndex = turn.steps.findIndex(s => s.index === updatedStep.index)
    if (turnStepIndex >= 0) {
      turn.steps.splice(turnStepIndex, 1, updatedStep)
    }
  }
}

const session = createMockSession()
const currentTurnIndex = 1
const currentTurn = session.turns[currentTurnIndex - 1]
const step = createStep(1, ['parse_document'], currentTurnIndex)
session.steps.push(step)
currentTurn.steps.push(step)

assert.equal(session.steps[0].tools[0].status, 'pending')
assert.equal(currentTurn.steps[0].tools[0].status, 'pending')

// Simulate tool_start: pending -> running
let lastIndex = session.steps.length - 1
let currentStep = session.steps[lastIndex]
let targetToolIndex = currentStep.tools.findIndex(t => t.name === 'parse_document')
let updatedTool = { ...currentStep.tools[targetToolIndex], status: 'running', startTime: 1_000_000 }
replaceStepAt(session, lastIndex, {
  ...currentStep,
  tools: currentStep.tools.map((t, idx) => (idx === targetToolIndex ? updatedTool : t)),
})

assert.equal(session.steps[0].tools[0].status, 'running')
assert.equal(currentTurn.steps[0].tools[0].status, 'running')
assert.strictEqual(
  session.steps[0],
  currentTurn.steps[0],
  'rendered step and source-of-truth step must be the same object reference'
)

// Simulate tool_result: running -> done with summary and duration
lastIndex = session.steps.length - 1
currentStep = session.steps[lastIndex]
targetToolIndex = currentStep.tools.findIndex(t => t.name === 'parse_document')
updatedTool = {
  ...currentStep.tools[targetToolIndex],
  status: 'done',
  summary: 'items:5',
  duration: 1.2,
  progress: undefined,
}
replaceStepAt(session, lastIndex, {
  ...currentStep,
  tools: currentStep.tools.map((t, idx) => (idx === targetToolIndex ? updatedTool : t)),
})

assert.equal(session.steps[0].tools[0].status, 'done')
assert.equal(session.steps[0].tools[0].summary, 'items:5')
assert.equal(currentTurn.steps[0].tools[0].status, 'done')
assert.equal(currentTurn.steps[0].tools[0].summary, 'items:5')
assert.strictEqual(
  session.steps[0],
  currentTurn.steps[0],
  'step reference must remain identical after tool_result update'
)

console.log('stepSync verification passed')
