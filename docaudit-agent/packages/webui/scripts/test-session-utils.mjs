import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { rmSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const rootDir = resolve(scriptDir, '..')
const outDir = resolve(rootDir, '.tmp/session-utils-test')

rmSync(outDir, { recursive: true, force: true })
mkdirSync(outDir, { recursive: true })

try {
  execFileSync(
    process.execPath,
    [
      resolve(rootDir, 'node_modules/typescript/bin/tsc'),
      '--target',
      'ES2020',
      '--module',
      'ES2020',
      '--moduleResolution',
      'bundler',
      '--strict',
      '--skipLibCheck',
      '--outDir',
      outDir,
      '--rootDir',
      resolve(rootDir, 'src'),
      resolve(rootDir, 'src/utils/sessionUtils.ts'),
    ],
    { cwd: rootDir, stdio: 'inherit' }
  )

  const sessionUtils = await import(pathToFileURL(resolve(outDir, 'utils/sessionUtils.js')).href)
  const { thoughtsForStep } = sessionUtils

  const makeThought = (overrides = {}) => ({
    id: 1,
    text: '思考',
    turn: 1,
    turnIndex: 1,
    segmentIndex: 0,
    segmentType: 'observe',
    timestamp: 1,
    ...overrides,
  })

  const makeStep = (index, overrides = {}) => ({
    index,
    numeral: '壹',
    label: 'step',
    skill: '',
    tools: [],
    turnIndex: 1,
    startSegmentIndex: 0,
    endSegmentIndex: Infinity,
    ...overrides,
  })

  // Case 1: thoughts with explicit stepIndex are assigned to the matching step.
  {
    const turn = {
      message: { role: 'user', text: '审核', timestamp: 1 },
      steps: [makeStep(1), makeStep(2)],
    }
    const thoughts = [
      makeThought({ id: 1, text: 'step1思考', stepIndex: 1 }),
      makeThought({ id: 2, text: 'step2思考', stepIndex: 2 }),
    ]
    const step1Thoughts = thoughtsForStep(thoughts, turn, 0)
    assert.equal(step1Thoughts.length, 1)
    assert.equal(step1Thoughts[0].text, 'step1思考')

    const step2Thoughts = thoughtsForStep(thoughts, turn, 1)
    assert.equal(step2Thoughts.length, 1)
    assert.equal(step2Thoughts[0].text, 'step2思考')
  }

  // Case 2: subagent thoughts (with source) are excluded from step thoughts.
  {
    const turn = {
      message: { role: 'user', text: '审核', timestamp: 1 },
      steps: [makeStep(1)],
    }
    const thoughts = [
      makeThought({ id: 1, text: 'parent', stepIndex: 1 }),
      makeThought({ id: 2, text: 'subagent', stepIndex: 1, source: 'parser' }),
    ]
    const result = thoughtsForStep(thoughts, turn, 0)
    assert.equal(result.length, 1)
    assert.equal(result[0].text, 'parent')
  }

  // Case 3: fallback to segment range when no thought has stepIndex (history sessions).
  {
    const turn = {
      message: { role: 'user', text: '审核', timestamp: 1 },
      steps: [
        makeStep(1, { startSegmentIndex: 0, endSegmentIndex: 2 }),
        makeStep(2, { startSegmentIndex: 2, endSegmentIndex: 4 }),
      ],
    }
    const thoughts = [
      makeThought({ id: 1, text: 'first', segmentIndex: 0 }),
      makeThought({ id: 2, text: 'second', segmentIndex: 2 }),
    ]
    const step1 = thoughtsForStep(thoughts, turn, 0)
    assert.equal(step1.length, 1)
    assert.equal(step1[0].text, 'first')

    const step2 = thoughtsForStep(thoughts, turn, 1)
    assert.equal(step2.length, 1)
    assert.equal(step2[0].text, 'second')
  }

  // Case 4: duplicated text across consecutive steps is hidden in the later step.
  {
    const turn = {
      message: { role: 'user', text: '审核', timestamp: 1 },
      steps: [makeStep(1), makeStep(2)],
    }
    const thoughts = [
      makeThought({ id: 1, text: 'shared', stepIndex: 1, turn: 1 }),
      makeThought({ id: 2, text: 'shared', stepIndex: 2, turn: 2 }),
      makeThought({ id: 3, text: 'unique', stepIndex: 2, turn: 3 }),
    ]
    const step2 = thoughtsForStep(thoughts, turn, 1)
    assert.equal(step2.length, 1)
    assert.equal(step2[0].text, 'unique')
  }

  // Case 5: consecutive tokens sharing the same turn are merged into one thought block.
  {
    const turn = {
      message: { role: 'user', text: '审核', timestamp: 1 },
      steps: [makeStep(1)],
    }
    const thoughts = [
      makeThought({ id: 1, text: '第', turn: 1, stepIndex: 1 }),
      makeThought({ id: 2, text: '一', turn: 1, stepIndex: 1 }),
      makeThought({ id: 3, text: '段', turn: 1, stepIndex: 1 }),
      makeThought({ id: 4, text: '第二段', turn: 2, stepIndex: 1 }),
    ]
    const result = thoughtsForStep(thoughts, turn, 0)
    assert.equal(result.length, 2)
    assert.equal(result[0].id, 1)
    assert.equal(result[0].text, '第一段')
    assert.equal(result[1].id, 4)
    assert.equal(result[1].text, '第二段')
  }

  // Case 6: legacy turn 0 thoughts are not collapsed into a single block.
  {
    const turn = {
      message: { role: 'user', text: '审核', timestamp: 1 },
      steps: [makeStep(1)],
    }
    const thoughts = [
      makeThought({ id: 1, text: 'a', turn: 0, stepIndex: 1 }),
      makeThought({ id: 2, text: 'b', turn: 0, stepIndex: 1 }),
    ]
    const result = thoughtsForStep(thoughts, turn, 0)
    assert.equal(result.length, 2)
    assert.equal(result[0].text, 'a')
    assert.equal(result[1].text, 'b')
  }

  // Case 7: deduplication compares merged text against merged previous-step text.
  {
    const turn = {
      message: { role: 'user', text: '审核', timestamp: 1 },
      steps: [makeStep(1), makeStep(2)],
    }
    const thoughts = [
      makeThought({ id: 1, text: '第', stepIndex: 1, turn: 1 }),
      makeThought({ id: 2, text: '一段', stepIndex: 1, turn: 1 }),
      makeThought({ id: 3, text: '第一段', stepIndex: 2, turn: 2 }),
    ]
    const step2 = thoughtsForStep(thoughts, turn, 1)
    assert.equal(step2.length, 0)
  }

  // Case 8: boundary condition — thought at endSegmentIndex is excluded.
  {
    const turn = {
      message: { role: 'user', text: '审核', timestamp: 1 },
      steps: [
        makeStep(1, { startSegmentIndex: 0, endSegmentIndex: 2 }),
        makeStep(2, { startSegmentIndex: 2, endSegmentIndex: 4 }),
      ],
    }
    const thoughts = [
      makeThought({ id: 1, text: 'inside', segmentIndex: 1 }),
      makeThought({ id: 2, text: 'at boundary', segmentIndex: 2 }),
    ]
    const step1 = thoughtsForStep(thoughts, turn, 0)
    assert.equal(step1.length, 1)
    assert.equal(step1[0].text, 'inside')

    const step2 = thoughtsForStep(thoughts, turn, 1)
    assert.equal(step2.length, 1)
    assert.equal(step2[0].text, 'at boundary')
  }

  // Case 9: turnIndex mismatch filters out thoughts from other turns.
  {
    const turn = {
      message: { role: 'user', text: '审核', timestamp: 1 },
      steps: [makeStep(1, { turnIndex: 2 })],
    }
    const thoughts = [
      makeThought({ id: 1, text: 'same turn', stepIndex: 1, turnIndex: 2 }),
      makeThought({ id: 2, text: 'other turn', stepIndex: 1, turnIndex: 1 }),
    ]
    const result = thoughtsForStep(thoughts, turn, 0)
    assert.equal(result.length, 1)
    assert.equal(result[0].text, 'same turn')
  }

  // Case 10: mixed stepIndex — each thought evaluated independently.
  // Thoughts with stepIndex match by index; thoughts without fall back
  // to segment-range matching.
  {
    const turn = {
      message: { role: 'user', text: '审核', timestamp: 1 },
      steps: [
        makeStep(1, { startSegmentIndex: 0, endSegmentIndex: 2 }),
      ],
    }
    const thoughts = [
      makeThought({ id: 1, text: 'with stepIndex', stepIndex: 1, segmentIndex: 5, turn: 1 }),
      makeThought({ id: 2, text: 'fallback match', segmentIndex: 0, turn: 2 }),
    ]
    const result = thoughtsForStep(thoughts, turn, 0)
    assert.equal(result.length, 2)
    // Sorted by segmentIndex ascending: fallback match (0) before with stepIndex (5)
    assert.equal(result[0].text, 'fallback match')
    assert.equal(result[1].text, 'with stepIndex')
  }

  // Case 11: out-of-bounds step index returns empty array safely.
  {
    const turn = {
      message: { role: 'user', text: '审核', timestamp: 1 },
      steps: [makeStep(1)],
    }
    const result = thoughtsForStep([], turn, 5)
    assert.equal(result.length, 0)
  }

  console.log('sessionUtils verification passed')
} finally {
  rmSync(outDir, { recursive: true, force: true })
}
