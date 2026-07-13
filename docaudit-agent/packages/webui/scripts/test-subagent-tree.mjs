import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { rmSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const rootDir = resolve(scriptDir, '..')
const outDir = resolve(rootDir, '.tmp/subagent-tree-test')

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
      resolve(rootDir, 'src/composables/subagentTree.ts'),
    ],
    { cwd: rootDir, stdio: 'inherit' }
  )

  const tree = await import(pathToFileURL(resolve(outDir, 'composables/subagentTree.js')).href)
  const {
    findNodeByHandleId,
    findRunningParentByHandleId,
    updateNodeInTree,
    upsertNodeInTree,
    upsertNodeInContainer,
    appendSubagentThoughtInContainer,
    newSubagentThoughtBlockInContainer,
    addSubagentToolInContainer,
    routeSubagentWrapper,
  } = tree

  const child = { name: 'child', handleId: 'child-1', task: 't', status: 'running' }
  const parent = { name: 'parent', handleId: 'parent-1', task: 't', status: 'running', children: [child] }
  const roots = [parent]

  // findNodeByHandleId
  assert.equal(findNodeByHandleId(roots, 'child-1'), child)
  assert.equal(findNodeByHandleId(roots, 'missing'), undefined)

  // findRunningParentByHandleId
  assert.equal(findRunningParentByHandleId(roots, 'parent-1'), parent)
  parent.status = 'completed'
  assert.equal(findRunningParentByHandleId(roots, 'parent-1'), undefined)
  parent.status = 'running'

  // updateNodeInTree
  const updatedRoots = updateNodeInTree(roots, 'child-1', node => ({ ...node, task: 'updated' }))
  assert.equal(updatedRoots[0].children[0].task, 'updated')
  assert.equal(child.task, 't', 'original should be immutable')

  // upsertNodeInContainer: create
  const created = upsertNodeInContainer([], { name: 'new', handleId: 'new-1', task: 'nt', status: 'running' })
  assert.equal(created.length, 1)
  assert.equal(created[0].name, 'new')

  // upsertNodeInContainer: update with conclusion accumulation
  const patched = upsertNodeInContainer(created, { name: 'new', handleId: 'new-1', conclusion: 'a' })
  assert.equal(patched[0].conclusion, 'a')
  const patched2 = upsertNodeInContainer(patched, { name: 'new', handleId: 'new-1', conclusion: 'b' })
  assert.equal(patched2[0].conclusion, 'ab')

  // upsertNodeInTree: root level
  const rootUpserted = upsertNodeInTree([], 'root', 'root-1', null, { task: 'rt', status: 'running' })
  assert.equal(rootUpserted.length, 1)
  assert.equal(rootUpserted[0].name, 'root')

  // upsertNodeInTree: nested level
  const nestedUpserted = upsertNodeInTree(rootUpserted, 'nested', 'nested-1', 'root-1', { task: 'nt', status: 'running' })
  assert.equal(nestedUpserted[0].children.length, 1)
  assert.equal(nestedUpserted[0].children[0].name, 'nested')

  // appendSubagentThoughtInContainer
  let thoughtRoots = upsertNodeInTree([], 'sa', 'sa-1', null, { status: 'running' })
  thoughtRoots = appendSubagentThoughtInContainer(thoughtRoots, 'sa-1', 'hello', 1)
  assert.equal(thoughtRoots[0].thoughts.length, 1)
  assert.equal(thoughtRoots[0].thoughts[0].text, 'hello')
  thoughtRoots = appendSubagentThoughtInContainer(thoughtRoots, 'sa-1', ' world', 2)
  assert.equal(thoughtRoots[0].thoughts.length, 1)
  assert.equal(thoughtRoots[0].thoughts[0].text, 'hello world')

  // newSubagentThoughtBlockInContainer
  thoughtRoots = newSubagentThoughtBlockInContainer(thoughtRoots, 'sa-1', 2)
  assert.equal(thoughtRoots[0].thoughts.length, 2)
  assert.equal(thoughtRoots[0].thoughts[1].text, '')

  // addSubagentToolInContainer
  let toolRoots = upsertNodeInTree([], 'sa', 'sa-1', null, { status: 'running' })
  const runningTool = { id: 't-1', name: 'audit', skill: '', status: 'running', callKind: 'tool', callScope: 'parent', subagentName: null, handleId: null, parentHandleId: null }
  toolRoots = addSubagentToolInContainer(toolRoots, 'sa-1', runningTool)
  assert.equal(toolRoots[0].tools.length, 1)
  assert.equal(toolRoots[0].tools[0].status, 'running')
  const doneTool = { id: 't-2', name: 'audit', skill: '', status: 'done', callKind: 'tool', callScope: 'parent', subagentName: null, handleId: null, parentHandleId: null }
  toolRoots = addSubagentToolInContainer(toolRoots, 'sa-1', doneTool)
  assert.equal(toolRoots[0].tools.length, 1)
  assert.equal(toolRoots[0].tools[0].status, 'done')
  assert.equal(toolRoots[0].tools[0].id, 't-2')

  // routeSubagentWrapper: root level
  const wrapper = { id: 'w-1', name: 'format_audit', skill: '', status: 'done', callKind: 'subagent_run', callScope: 'parent', subagentName: 'format_audit', handleId: 'fmt-1', parentHandleId: null }
  let routed = routeSubagentWrapper([], 'format_audit', 'fmt-1', null, wrapper)
  assert.equal(routed.length, 1)
  assert.equal(routed[0].wrapper.id, 'w-1')

  // routeSubagentWrapper: nested level adds wrapper to parent tools too
  const nestedWrapper = { id: 'w-2', name: 'content_audit', skill: '', status: 'done', callKind: 'subagent_run', callScope: 'subagent', subagentName: 'content_audit', handleId: 'cnt-1', parentHandleId: 'fmt-1' }
  routed = routeSubagentWrapper(routed, 'content_audit', 'cnt-1', 'fmt-1', nestedWrapper)
  assert.equal(routed[0].children.length, 1)
  assert.equal(routed[0].children[0].wrapper.id, 'w-2')
  assert.equal(routed[0].tools.length, 1)
  assert.equal(routed[0].tools[0].id, 'w-2')

  console.log('subagentTree verification passed')
} finally {
  rmSync(outDir, { recursive: true, force: true })
}
