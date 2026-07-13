import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { rmSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const rootDir = resolve(scriptDir, '..')
const outDir = resolve(rootDir, '.tmp/streaming-markdown-test')

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
      resolve(rootDir, 'src/utils/streamingMarkdown.ts'),
    ],
    { cwd: rootDir, stdio: 'inherit' }
  )

  const { renderStreamingMarkdown, renderStreamingParts, createStreamingRenderer } = await import(
    pathToFileURL(resolve(outDir, 'utils/streamingMarkdown.js')).href
  )

  assert.equal(renderStreamingMarkdown(''), '')

  // Cache isolation: two concurrent renderers do not interfere.
  const rA = createStreamingRenderer()
  const rB = createStreamingRenderer()

  const a1 = rA.renderStreamingHtml('hello **bold**', true)
  assert.ok(a1.includes('<strong>bold</strong>'))

  // rB processes completely different content — should not affect rA's cache.
  const b1 = rB.renderStreamingHtml('other text', true)
  assert.ok(b1.includes('other text'))

  // rA continues streaming — cache should still be valid.
  const a2 = rA.renderStreamingHtml('hello **bold** world', true)
  assert.ok(a2.includes('<strong>bold</strong>'), 'cache isolated: bold still rendered')
  assert.ok(a2.includes('world'))

  const streamingPlain = renderStreamingMarkdown('hello world', { isStreaming: true })
  assert.ok(streamingPlain.includes('hello world'))
  assert.ok(streamingPlain.includes('<span class="streaming-trailing">'))

  const xssPara = renderStreamingMarkdown('hello <script>alert(1)</script> world', {
    isStreaming: true,
  })
  assert.ok(!xssPara.includes('<script>'))
  assert.ok(xssPara.includes('&lt;script&gt;'))

  assert.equal(
    renderStreamingMarkdown('hello world', { isStreaming: false }).trim(),
    '<p>hello world</p>'
  )

  const streamingCode = renderStreamingMarkdown('```py\nprint(1 < 2)\n', {
    isStreaming: true,
  })
  assert.ok(!streamingCode.includes('<pre>'))
  assert.ok(streamingCode.includes('&lt;'))
  assert.ok(streamingCode.includes('```py'))
  assert.ok(streamingCode.includes('print(1 &lt; 2)'))

  const completeCode = renderStreamingMarkdown('```py\nprint(1 < 2)\n```', {
    isStreaming: false,
  })
  assert.ok(completeCode.includes('<pre>'))
  assert.ok(completeCode.includes('<code'))

  const multi = renderStreamingMarkdown('First para.\n\nSecond para', {
    isStreaming: true,
  })
  assert.ok(multi.includes('<p>First para.</p>'))
  assert.ok(multi.includes('Second para'))
  assert.ok(!multi.includes('<p>Second para</p>'))

  // Inline markdown renders immediately when syntax completes — only the
  // last text token stays as raw trailing.
  const inline = renderStreamingMarkdown('**bold** text', { isStreaming: true })
  assert.ok(inline.includes('<strong>bold</strong>'), 'bold should render immediately')
  assert.ok(!inline.includes('**bold**'), 'raw bold markers should not appear')
  assert.ok(inline.includes('<span class="streaming-trailing"> text</span>'))

  // Completed inline at end of text — no trailing, full render.
  const inlineCompleteEnd = renderStreamingMarkdown('hello **bold**', { isStreaming: true })
  assert.ok(inlineCompleteEnd.includes('<strong>bold</strong>'), 'bold at end should render')
  assert.ok(!inlineCompleteEnd.includes('**bold**'))

  // Multi-block: complete first paragraph, second with inline + trailing.
  const multiInline = renderStreamingMarkdown('First para.\n\nSecond **bold** text', {
    isStreaming: true,
  })
  assert.ok(multiInline.includes('<p>First para.</p>'))
  assert.ok(multiInline.includes('<strong>bold</strong>'))
  assert.ok(multiInline.includes('<span class="streaming-trailing"> text</span>'))

  // Incomplete inline syntax — stays as raw trailing text.
  const partialInline = renderStreamingMarkdown('hello **bo', { isStreaming: true })
  assert.ok(partialInline.includes('**bo'))
  assert.ok(partialInline.includes('<span class="streaming-trailing">'))

  // renderStreamingParts: split output for incremental DOM updates.
  const parts = renderStreamingParts('hello **bold** world', true)
  assert.equal(parts.trailingText, ' world')
  assert.ok(parts.completeHtml.includes('<strong>bold</strong>'))

  const partsNoTrailing = renderStreamingParts('hello **bold**', true)
  assert.equal(partsNoTrailing.trailingText, '')
  assert.ok(partsNoTrailing.completeHtml.includes('<strong>bold</strong>'))

  const partsNonStreaming = renderStreamingParts('hello world', false)
  assert.equal(partsNonStreaming.trailingText, '')
  assert.ok(partsNonStreaming.completeHtml.includes('<p>hello world</p>'))

  console.log('streamingMarkdown verification passed')
} finally {
  rmSync(outDir, { recursive: true, force: true })
}
