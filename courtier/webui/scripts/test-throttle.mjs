import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { rmSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const rootDir = resolve(scriptDir, '..')
const outDir = resolve(rootDir, '.tmp/throttle-test')

rmSync(outDir, { recursive: true, force: true })
mkdirSync(outDir, { recursive: true })

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
    resolve(rootDir, 'src/utils/throttle.ts'),
  ],
  { cwd: rootDir, stdio: 'inherit' }
)

const { createTrailingThrottle } = await import(
  pathToFileURL(resolve(outDir, 'utils/throttle.js')).href
)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// Leading call fires immediately; burst within the window coalesces into one
// trailing call.
{
  let calls = 0
  const throttled = createTrailingThrottle(() => calls++, 100)
  throttled()
  assert.equal(calls, 1, 'leading call fires immediately')
  throttled()
  throttled()
  assert.equal(calls, 1, 'burst calls do not fire immediately')
  await sleep(150)
  assert.equal(calls, 2, 'exactly one trailing call fires after the window')
}

// A call after the window is a new leading call.
{
  let calls = 0
  const throttled = createTrailingThrottle(() => calls++, 60)
  throttled()
  await sleep(90)
  throttled()
  assert.equal(calls, 2, 'post-window call is a new leading call')
}

// A pending trailing timer suppresses later burst calls until it fires.
{
  let calls = 0
  const throttled = createTrailingThrottle(() => calls++, 100)
  throttled() // leading
  throttled() // schedules trailing
  await sleep(60)
  throttled() // must not schedule a second trailing
  await sleep(80)
  assert.equal(calls, 2, 'trailing timer is not duplicated by burst calls')
}

console.log('throttle verification passed')
