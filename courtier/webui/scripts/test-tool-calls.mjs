import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { rmSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const rootDir = resolve(scriptDir, '..')
const outDir = resolve(rootDir, '.tmp/tool-calls-test')

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
      resolve(rootDir, 'src/utils/toolCalls.ts'),
    ],
    { cwd: rootDir, stdio: 'inherit' }
  )

  const toolCalls = await import(pathToFileURL(resolve(outDir, 'utils/toolCalls.js')).href)
  const {
    UNKNOWN_SUBAGENT_NAME,
    normalizeToolResult,
    normalizeSession,
    displayToolName,
    buildStepToolGroups,
    isDeprecatedTool,
    parseDeprecatedReplacement,
  } = toolCalls

  const parentTool = normalizeToolResult({
    id: 'tool-1',
    name: 'parse_document',
    status: 'done',
  })
  assert.equal(parentTool.callKind, 'tool')
  assert.equal(parentTool.callScope, 'parent')
  assert.equal(parentTool.subagentName, null)
  assert.equal(parentTool.skill, '')

  const subagentTool = normalizeToolResult({
    id: 'tool-2',
    name: '[format_auditor] audit_format',
    skill: 'format',
    status: 'done',
    callKind: 'tool',
    callScope: 'subagent',
    subagentName: 'format_auditor',
  })
  assert.equal(displayToolName(subagentTool), 'audit_format')

  const wrapperTool = normalizeToolResult({
    id: 'tool-3',
    name: 'run_format_auditor',
    skill: 'format_auditor',
    status: 'done',
    callKind: 'subagent_run',
    callScope: 'parent',
    subagentName: 'format_auditor',
  })

  const grouped = buildStepToolGroups([parentTool, wrapperTool, subagentTool])
  assert.equal(grouped.length, 2)
  assert.equal(grouped[0].type, 'tool')
  assert.equal(grouped[0].tool.id, 'tool-1')
  assert.equal(grouped[1].type, 'subagent')
  assert.equal(grouped[1].name, 'format_auditor')
  assert.equal(grouped[1].isSynthetic, false)
  assert.equal(grouped[1].wrapper.id, 'tool-3')
  assert.equal(grouped[1].tools.length, 1)
  assert.equal(grouped[1].tools[0].id, 'tool-2')

  // Nested sub-agents: parent subagent_run with child subagent in step.subagents
  const childRun = {
    name: 'content_audit',
    handleId: 'content-1',
    parentHandleId: 'format-1',
    task: 'audit content',
    status: 'running',
    tools: [normalizeToolResult({
      id: 'tool-5',
      name: 'check_policy',
      skill: '',
      status: 'done',
      callKind: 'tool',
      callScope: 'subagent',
      subagentName: 'content_audit',
      handleId: 'content-1',
      parentHandleId: 'format-1',
    })],
  }
  const parentRun = {
    name: 'format_audit',
    handleId: 'format-1',
    task: 'audit format',
    status: 'running',
    wrapper: normalizeToolResult({
      id: 'tool-6',
      name: 'format_audit',
      skill: 'format_audit',
      status: 'done',
      callKind: 'subagent_run',
      callScope: 'parent',
      subagentName: 'format_audit',
      handleId: 'format-1',
    }),
    children: [childRun],
  }
  const nestedGrouped = buildStepToolGroups([parentRun.wrapper], [parentRun])
  assert.equal(nestedGrouped.length, 1)
  assert.equal(nestedGrouped[0].type, 'subagent')
  assert.equal(nestedGrouped[0].children.length, 1)
  assert.equal(nestedGrouped[0].children[0].type, 'subagent')
  assert.equal(nestedGrouped[0].children[0].name, 'content_audit')
  assert.equal(nestedGrouped[0].children[0].tools.length, 1)
  assert.equal(nestedGrouped[0].children[0].tools[0].name, 'check_policy')

  // Subagent-scoped tool referencing an existing SubagentRun should not duplicate the run
  const scopedToolForParent = normalizeToolResult({
    id: 'tool-7',
    name: 'report_issue',
    skill: '',
    status: 'done',
    callKind: 'tool',
    callScope: 'subagent',
    subagentName: 'format_audit',
    handleId: 'format-1',
  })
  const noDuplicateGrouped = buildStepToolGroups([scopedToolForParent], [parentRun])
  assert.equal(noDuplicateGrouped.length, 1)
  assert.equal(noDuplicateGrouped[0].type, 'subagent')
  assert.equal(noDuplicateGrouped[0].tools.length, 1)
  assert.equal(noDuplicateGrouped[0].tools[0].id, 'tool-7')

  const orphanGrouped = buildStepToolGroups([subagentTool])
  assert.equal(orphanGrouped.length, 1)
  assert.equal(orphanGrouped[0].type, 'subagent')
  assert.equal(orphanGrouped[0].name, 'format_auditor')
  assert.equal(orphanGrouped[0].isSynthetic, true)
  assert.equal(orphanGrouped[0].wrapper, undefined)
  assert.equal(orphanGrouped[0].tools[0].id, 'tool-2')

  const unknownSubagentTool = normalizeToolResult({
    id: 'tool-4',
    name: 'audit_format',
    status: 'done',
    callKind: 'tool',
    callScope: 'subagent',
    subagentName: null,
  })
  const unknownGrouped = buildStepToolGroups([unknownSubagentTool])
  assert.equal(unknownGrouped[0].type, 'subagent')
  assert.equal(unknownGrouped[0].name, UNKNOWN_SUBAGENT_NAME)

  // Pending subagent_run wrapper (handleId null while the skill is running)
  // must merge with the SubagentRun by name — not render twice.
  const pendingWrapper = normalizeToolResult({
    id: 'tool-8',
    name: 'full_government_audit',
    skill: 'full_government_audit',
    status: 'running',
    callKind: 'subagent_run',
    callScope: 'parent',
    subagentName: 'full_government_audit',
    handleId: null,
  })
  const runningRun = {
    name: 'full_government_audit',
    handleId: 'h-abc123',
    task: '完整审核',
    status: 'running',
  }
  const pendingGrouped = buildStepToolGroups([pendingWrapper], [runningRun])
  assert.equal(pendingGrouped.length, 1)
  assert.equal(pendingGrouped[0].type, 'subagent')
  assert.equal(pendingGrouped[0].key, 'h-abc123')

  // Same scenario but the wrapper's callKind is still "tool" (classification
  // only arrives with tool_result) — it must merge by name as the run's
  // wrapper instead of rendering as a duplicate standalone card.
  const unclassifiedWrapper = normalizeToolResult({
    id: 'tool-9',
    name: 'full_government_audit',
    skill: '',
    status: 'running',
    callKind: 'tool',
    callScope: 'parent',
    handleId: null,
  })
  const dupGrouped = buildStepToolGroups([unclassifiedWrapper], [runningRun])
  assert.equal(dupGrouped.length, 1)
  assert.equal(dupGrouped[0].type, 'subagent')
  assert.equal(dupGrouped[0].wrapper.id, 'tool-9')

  const legacyStep = {
    index: 1,
    numeral: '壹',
    label: 'legacy_tool',
    skill: '',
    tools: [{ id: 'legacy-1', name: 'legacy_tool', skill: '', status: 'done' }],
  }
  const legacySession = {
    id: 'sess_1',
    task: '审核合同',
    modelName: 'Claude Opus 4.8',
    status: 'completed',
    turns: [
      {
        message: { role: 'user', text: '审核合同', timestamp: 1 },
        steps: [legacyStep],
      },
    ],
    steps: [legacyStep],
    thoughts: [],
    stats: { tokensIn: 0, tokensOut: 0, elapsed: 0 },
    createdAt: 1,
  }

  const normalizedSession = normalizeSession(legacySession)
  assert.equal(normalizedSession.steps[0].tools[0].callKind, 'tool')
  assert.equal(normalizedSession.steps[0].tools[0].callScope, 'parent')
  assert.equal(normalizedSession.turns[0].steps[0].tools[0].subagentName, null)
  assert.notEqual(normalizedSession.steps[0], legacyStep)
  assert.equal('callKind' in legacyStep.tools[0], false)

  // Deprecated tool detection
  const deprecatedTool = normalizeToolResult({
    id: 'tool-d1',
    name: 'old_check_format',
    skillDescription: '[DEPRECATED (use check_format_v2)] 旧版格式检查',
    status: 'done',
  })
  assert.equal(isDeprecatedTool(deprecatedTool), true)
  assert.equal(parseDeprecatedReplacement(deprecatedTool), 'check_format_v2')

  const plainTool = normalizeToolResult({
    id: 'tool-d2',
    name: 'check_format',
    status: 'done',
  })
  assert.equal(isDeprecatedTool(plainTool), false)
  assert.equal(parseDeprecatedReplacement(plainTool), undefined)

  console.log('toolCalls verification passed')
} finally {
  rmSync(outDir, { recursive: true, force: true })
}
