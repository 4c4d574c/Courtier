import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { rmSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const rootDir = resolve(scriptDir, '..')
const outDir = resolve(rootDir, '.tmp/tool-progress-test')

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

  const { normalizeToolResult } = await import(
    pathToFileURL(resolve(outDir, 'utils/toolCalls.js')).href
  )

  // ToolResult should preserve progress and startTime fields
  const tool = normalizeToolResult({
    id: 'tool-1',
    name: 'audit_format',
    status: 'running',
    progress: '正在解析段落...',
    startTime: 1000,
  })

  assert.equal(tool.progress, '正在解析段落...')
  assert.equal(tool.startTime, 1000)
  assert.equal(tool.status, 'running')

  // ToolResult without progress should have undefined progress
  const tool2 = normalizeToolResult({
    id: 'tool-2',
    name: 'echo',
    status: 'done',
  })
  assert.equal(tool2.progress, undefined)

  console.log('toolProgress verification passed')
} finally {
  rmSync(outDir, { recursive: true, force: true })
}
