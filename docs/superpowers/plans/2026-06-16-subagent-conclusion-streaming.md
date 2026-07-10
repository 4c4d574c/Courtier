# 子代理结论流式 Markdown 显示实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在前端把子代理结论输出改为字符级流式展示，并在运行期间按完整 Markdown 块实时渲染。

**架构：** 新增纯函数 `renderStreamingMarkdown` 负责“完整块 Markdown + 未完成块纯文本”的拼接逻辑；新增 Vue 组件 `StreamingMarkdown` 负责按 `speed` 递增 `displayLength` 并调用该函数；`StepGroup.vue` 替换原有结论渲染为 `StreamingMarkdown`。

**技术栈：** Vue 3 + TypeScript + `marked` ^18.0.4

---

## 文件清单

| 文件 | 职责 |
|------|------|
| `tui/src/utils/streamingMarkdown.ts` | 块感知流式 Markdown 渲染纯函数 |
| `tui/src/components/StreamingMarkdown.vue` | 按字符递增显示结论的 Vue 组件 |
| `tui/src/components/StepGroup.vue` | 用 `StreamingMarkdown` 替换原有结论渲染 |
| `tui/scripts/test-streaming-markdown.mjs` | 对 `streamingMarkdown.ts` 的单元测试脚本 |
| `tui/package.json` | 扩展 `test` 脚本以包含新测试 |

---

## 任务 1：实现 `streamingMarkdown.ts` 并编写测试（TDD）

**文件：**
- 创建：`tui/src/utils/streamingMarkdown.ts`
- 创建：`tui/scripts/test-streaming-markdown.mjs`

### 步骤 1：编写失败的测试

创建 `tui/scripts/test-streaming-markdown.mjs`：

```javascript
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

  const { renderStreamingMarkdown } = await import(
    pathToFileURL(resolve(outDir, 'utils/streamingMarkdown.js')).href
  )

  assert.equal(renderStreamingMarkdown(''), '')

  assert.equal(
    renderStreamingMarkdown('hello world', { isStreaming: true }),
    'hello world'
  )

  assert.equal(
    renderStreamingMarkdown('hello world', { isStreaming: false }).trim(),
    '<p>hello world</p>'
  )

  const streamingCode = renderStreamingMarkdown('```py\nprint(1 < 2)\n', {
    isStreaming: true,
  })
  assert.ok(!streamingCode.includes('<pre>'))
  assert.ok(streamingCode.includes('&lt;'))

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

  const inline = renderStreamingMarkdown('**bold** text', { isStreaming: true })
  assert.equal(inline, '**bold** text')

  const inlineComplete = renderStreamingMarkdown('**bold** text', {
    isStreaming: false,
  })
  assert.ok(inlineComplete.includes('<strong>bold</strong>'))

  console.log('streamingMarkdown verification passed')
} finally {
  rmSync(outDir, { recursive: true, force: true })
}
```

### 步骤 2：运行测试验证失败

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
node scripts/test-streaming-markdown.mjs
```

**预期：** 失败，提示 `Cannot find module '/.../streamingMarkdown.js'` 或 `renderStreamingMarkdown is not a function`。

### 步骤 3：编写最少实现代码

创建 `tui/src/utils/streamingMarkdown.ts`：

```typescript
import { marked, type Token } from 'marked'

export interface RenderStreamingMarkdownOptions {
  isStreaming?: boolean
}

function escapeHtml(raw: string): string {
  return raw
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;')
}

export function renderStreamingMarkdown(
  text: string,
  options: RenderStreamingMarkdownOptions = {}
): string {
  if (!text) return ''

  const tokens = marked.lexer(text)
  if (tokens.length === 0) return ''

  const completeTokens: Token[] = []
  let trailingHtml = ''

  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i]
    const isLast = i === tokens.length - 1

    if (!isLast || !options.isStreaming) {
      completeTokens.push(token)
    } else {
      trailingHtml += escapeHtml(token.raw)
    }
  }

  const html = completeTokens.length > 0 ? marked.parser(completeTokens) : ''
  return (
    html +
    (trailingHtml
      ? `<span class="streaming-trailing">${trailingHtml}</span>`
      : '')
  )
}
```

### 步骤 4：运行测试验证通过

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
node scripts/test-streaming-markdown.mjs
```

**预期：** 输出 `streamingMarkdown verification passed`。

### 步骤 5：Commit

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
git add src/utils/streamingMarkdown.ts scripts/test-streaming-markdown.mjs
git commit -m "feat(tui): add block-aware streaming markdown renderer"
```

---

## 任务 2：实现 `StreamingMarkdown.vue` 组件

**文件：**
- 创建：`tui/src/components/StreamingMarkdown.vue`

### 步骤 1：创建组件

```vue
<template>
  <div v-html="renderedHtml"></div>
</template>

<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from 'vue'
import { renderStreamingMarkdown } from '../utils/streamingMarkdown'

interface Props {
  content: string
  isStreaming: boolean
  speed?: number
}

const props = withDefaults(defineProps<Props>(), {
  speed: 20,
})

const displayLength = ref(0)
let timer: number | null = null

function clearTimer() {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}

function startAnimation() {
  clearTimer()
  if (!props.isStreaming) {
    displayLength.value = props.content.length
    return
  }
  if (displayLength.value >= props.content.length) return

  timer = window.setInterval(() => {
    if (displayLength.value < props.content.length) {
      displayLength.value++
    } else {
      clearTimer()
    }
  }, props.speed)
}

watch(
  () => props.content,
  (newContent, oldContent) => {
    if (newContent.length < displayLength.value || newContent.length < (oldContent ?? '').length) {
      displayLength.value = 0
    }
    startAnimation()
  }
)

watch(
  () => props.isStreaming,
  (streaming) => {
    if (!streaming) {
      clearTimer()
      displayLength.value = props.content.length
    } else {
      startAnimation()
    }
  }
)

const renderedHtml = computed(() => {
  const visibleText = props.content.slice(0, displayLength.value)
  return renderStreamingMarkdown(visibleText, { isStreaming: props.isStreaming })
})

onUnmounted(clearTimer)

startAnimation()
</script>
```

### 步骤 2：运行类型检查

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
npx vue-tsc --noEmit
```

**预期：** 无类型错误。

### 步骤 3：Commit

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
git add src/components/StreamingMarkdown.vue
git commit -m "feat(tui): add StreamingMarkdown component"
```

---

## 任务 3：在 `StepGroup.vue` 中集成 `StreamingMarkdown`

**文件：**
- 修改：`tui/src/components/StepGroup.vue`

### 步骤 1：替换结论渲染

修改 `tui/src/components/StepGroup.vue`：

1. 删除 `<script setup>` 中的 `renderMarkdown` 函数（第 97-99 行）。
2. 删除 `import { marked } from 'marked'`。
3. 添加 `import StreamingMarkdown from './StreamingMarkdown.vue'`。
4. 把模板中这两行：

```vue
<div v-if="item.conclusion && item.runStatus !== 'running'" class="subagent-conclusion markdown-content" v-html="renderMarkdown(item.conclusion)"></div>
<div v-else-if="item.conclusion && item.runStatus === 'running'" class="subagent-conclusion subagent-conclusion--streaming">{{ item.conclusion }}</div>
```

替换为：

```vue
<StreamingMarkdown
  v-if="item.conclusion"
  class="subagent-conclusion markdown-content"
  :content="item.conclusion"
  :is-streaming="item.runStatus === 'running'"
  :speed="20"
/>
```

### 步骤 2：运行类型检查

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
npx vue-tsc --noEmit
```

**预期：** 无类型错误。

### 步骤 3：Commit

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
git add src/components/StepGroup.vue
git commit -m "feat(tui): stream subagent conclusion with markdown"
```

---

## 任务 4：更新测试脚本并运行全部测试

**文件：**
- 修改：`tui/package.json`

### 步骤 1：扩展 `test` 脚本

将 `tui/package.json` 中的：

```json
"test": "node scripts/test-tool-calls.mjs"
```

改为：

```json
"test": "node scripts/test-tool-calls.mjs && node scripts/test-streaming-markdown.mjs"
```

### 步骤 2：运行全部测试

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
npm test
```

**预期：**

```
toolCalls verification passed
streamingMarkdown verification passed
```

### 步骤 3：Commit

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
git add package.json
git commit -m "chore(tui): include streaming markdown tests in npm test"
```

---

## 任务 5：构建与人工验证

### 步骤 1：生产构建

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
npm run build
```

**预期：** 构建成功，无 TypeScript 或 Vite 错误。

### 步骤 2：启动开发服务器验证

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
npm run dev
```

在后端服务已启动的前提下，触发一个会调用子代理的审核任务，观察：

1. 子代理结论是否逐字出现。
2. 已完整的 Markdown 块（如前面的标题、已结束的段落）是否实时渲染。
3. 最后一个不完整的块是否保持纯文本，直到子代理完成。
4. 子代理完成后，整个结论是否切换为完整 Markdown。

### 步骤 3：Commit（如有调整）

如果在验证中做了任何修复，单独 commit：

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
git add .
git commit -m "fix(tui): <具体修复内容>"
```

---

## 自检

- **规格覆盖度：** 规格中的组件 API、块感知渲染、StepGroup 集成、测试计划均已对应到具体任务。
- **占位符扫描：** 无 TODO、无“后续实现”、所有步骤均含实际代码或命令。
- **类型一致性：** `renderStreamingMarkdown` 在 `streamingMarkdown.ts`、`StreamingMarkdown.vue` 和测试中签名一致；`StreamingMarkdown` props 与 `StepGroup` 传入方式一致。
