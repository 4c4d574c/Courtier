<template>
  <div class="ext-page">
    <div class="ext-header">
      <h1 class="ext-title">插件与技能</h1>
      <button class="ext-refresh" type="button" :disabled="loading" @click="reload">
        {{ loading ? "刷新中..." : "刷新" }}
      </button>
    </div>

    <div class="ext-tabs">
      <button
        type="button"
        class="ext-tab"
        :class="{ 'ext-tab--active': tab === 'plugins' }"
        @click="tab = 'plugins'"
      >
        插件（{{ plugins.length }}）
      </button>
      <button
        type="button"
        class="ext-tab"
        :class="{ 'ext-tab--active': tab === 'skills' }"
        @click="tab = 'skills'"
      >
        技能（{{ skillCount }}）
      </button>
    </div>

    <p v-if="errorMsg" class="ext-error">{{ errorMsg }}</p>

    <!-- ── 插件 ─────────────────────────────────────────────── -->
    <section v-show="tab === 'plugins'">
      <div v-for="g in pluginGroups" :key="g.key" class="ext-domain">
        <div class="ext-domain-header">
          <h2 class="ext-domain-title">{{ g.title }}</h2>
          <span class="ext-badge ext-badge--source">{{ g.items.length }} 个插件</span>
        </div>
        <div class="ext-grid">
          <div v-for="p in g.items" :key="p.name" class="ext-card">
            <div class="ext-card-header">
              <span class="ext-card-name">{{ p.name }}</span>
              <span class="ext-badge" :class="stateClass(p)">{{ stateLabel(p) }}</span>
            </div>
            <p class="ext-card-desc">{{ p.description || "（无描述）" }}</p>
            <div class="ext-card-meta">
              v{{ p.version }} · 重连 {{ p.restartCount }} 次
            </div>
            <div v-if="p.tools.length" class="ext-chips">
              <span v-for="t in p.tools" :key="t.name" class="ext-chip" :title="t.description">
                {{ t.displayName }}
              </span>
            </div>
            <p v-if="p.scanError" class="ext-card-error">{{ p.scanError }}</p>
            <p v-if="p.blockedReason" class="ext-card-error">{{ p.blockedReason }}</p>
            <div class="ext-card-actions">
              <template v-if="isTransitional(p.state)">
                <button class="ext-btn" disabled>处理中…</button>
              </template>
              <template v-else-if="p.scanStatus !== 'BLOCKED'">
                <button
                  v-if="p.state === 'ACTIVE'"
                  class="ext-btn"
                  :disabled="acting === p.name"
                  @click="doAction(p, 'stop')"
                >
                  停止
                </button>
                <button
                  v-else
                  class="ext-btn ext-btn--primary"
                  :disabled="acting === p.name"
                  @click="doAction(p, 'start')"
                >
                  启动
                </button>
                <button
                  class="ext-btn"
                  :disabled="acting === p.name"
                  @click="doAction(p, 'restart')"
                >
                  重启
                </button>
              </template>
              <button class="ext-btn" type="button" @click="openLog(p)">日志</button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ── 技能 ─────────────────────────────────────────────── -->
    <section v-show="tab === 'skills'">
      <div class="ext-skill-toolbar">
        <button class="ext-btn" type="button" @click="showCreateDomain = true">
          + 新建 domain 包
        </button>
        <button class="ext-btn ext-btn--primary" type="button" @click="showCreate = true">
          + 新建技能
        </button>
      </div>
      <div v-for="d in skillDomains" :key="d.name" class="ext-domain">
        <div class="ext-domain-header">
          <h2 class="ext-domain-title">{{ d.title }}</h2>
          <span class="ext-badge ext-badge--source">{{ d.name }} · {{ d.items.length }} 个技能</span>
          <span class="ext-badge" :class="d.enabled ? 'ext-badge--ok' : 'ext-badge--dim'">
            {{ d.enabled ? "已启用" : "已禁用" }}
          </span>
          <label
            class="ext-switch"
            :title="d.enabled ? '点击禁用该 domain 包' : '点击启用该 domain 包'"
          >
            <input
              type="checkbox"
              :checked="d.enabled"
              :disabled="togglingDomain === d.name"
              @change="toggleDomain(d)"
            />
            <span class="ext-switch-track"></span>
          </label>
        </div>
        <template v-if="d.enabled">
          <div v-if="d.errors.length" class="ext-banner">
            <div v-for="(e, i) in d.errors" :key="i" class="ext-banner-item">{{ e }}</div>
          </div>
          <p v-if="!d.items.length" class="ext-dim ext-domain-empty">该 domain 包下暂无技能</p>
        </template>
        <div class="ext-grid">
          <div v-for="s in d.items" :key="s.name" class="ext-card" :class="{ 'ext-card--disabled': !s.enabled }">
            <div class="ext-card-header">
              <span class="ext-card-name">{{ s.displayName }}</span>
              <span class="ext-badge" :class="s.enabled ? 'ext-badge--ok' : 'ext-badge--dim'">
                {{ s.enabled ? "已启用" : "已禁用" }}
              </span>
              <label class="ext-switch" :title="s.enabled ? '点击禁用' : '点击启用'">
                <input
                  type="checkbox"
                  :checked="s.enabled"
                  :disabled="toggling === s.name"
                  @change="toggleSkill(s, d.name)"
                />
                <span class="ext-switch-track"></span>
              </label>
            </div>
            <p class="ext-card-desc">{{ s.description || "（无描述）" }}</p>
            <div class="ext-card-meta">
              {{ s.name }} · {{ s.source }} · {{ modeLabel(s) }}
            </div>
            <div v-if="s.tools.length" class="ext-chips">
              <span v-for="t in s.tools" :key="t" class="ext-chip">{{ t }}</span>
            </div>
            <div v-if="s.skills.length" class="ext-chips">
              <span v-for="t in s.skills" :key="t" class="ext-chip ext-chip--sub">子:{{ t }}</span>
            </div>
            <div v-if="s.tags.length" class="ext-chips">
              <span v-for="t in s.tags" :key="t" class="ext-chip ext-chip--tag">#{{ t }}</span>
            </div>
            <div class="ext-card-actions">
              <button class="ext-btn" type="button" @click="openEdit(s, d.name)">
                查看 / 编辑
              </button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ── 插件日志对话框 ────────────────────────────────────── -->
    <div v-if="logView.open" class="ext-modal-mask" @click.self="closeLog">
      <div class="ext-modal ext-modal--wide">
        <h2 class="ext-modal-title">插件日志：{{ logView.name }}</h2>
        <div class="ext-log-toolbar">
          <select v-model.number="logTail" class="ext-log-select" @change="refreshLogs">
            <option :value="100">最近 100 行</option>
            <option :value="200">最近 200 行</option>
            <option :value="500">最近 500 行</option>
            <option :value="1000">最近 1000 行</option>
          </select>
          <label class="ext-log-auto">
            <input v-model="logAuto" type="checkbox" /> 自动刷新（3s）
          </label>
          <button class="ext-btn" type="button" :disabled="logView.loading" @click="refreshLogs">
            {{ logView.loading ? "刷新中..." : "刷新" }}
          </button>
          <span class="ext-dim">
            共 {{ logView.totalLines }} 行 · {{ (logView.sizeBytes / 1024).toFixed(1) }} KiB
          </span>
        </div>
        <p v-if="logView.error" class="ext-error">{{ logView.error }}</p>
        <pre v-if="logView.lines.length" class="ext-log-view">{{ logView.lines.join("\n") }}</pre>
        <p v-else class="ext-dim ext-log-empty">暂无日志（插件启动后，其 stderr 输出会记录在这里）</p>
        <div class="ext-modal-actions">
          <button class="ext-btn" type="button" @click="closeLog">关闭</button>
        </div>
      </div>
    </div>

    <!-- ── 新建 domain 包对话框 ──────────────────────────────── -->
    <div v-if="showCreateDomain" class="ext-modal-mask" @click.self="showCreateDomain = false">
      <div class="ext-modal ext-modal--narrow">
        <h2 class="ext-modal-title">新建 domain 包</h2>
        <div class="ext-form">
          <label class="ext-field">
            <span>名称（英文标识，小写/数字/下划线）*</span>
            <input v-model.trim="domainForm.name" placeholder="如 legal" />
          </label>
          <label class="ext-field">
            <span>标题</span>
            <input v-model.trim="domainForm.title" placeholder="如：法务文档" />
          </label>
          <label class="ext-field">
            <span>描述</span>
            <textarea v-model.trim="domainForm.description" rows="3" placeholder="这个领域包面向什么场景"></textarea>
          </label>
          <p class="ext-dim">
            将创建 domains/{{ domainForm.name || "<name>" }}/（config/domain.yaml + prompts + skills/），创建后立即生效。
          </p>
          <p v-if="domainError" class="ext-error">{{ domainError }}</p>
          <div class="ext-modal-actions">
            <button class="ext-btn" type="button" @click="showCreateDomain = false">取消</button>
            <button
              class="ext-btn ext-btn--primary"
              type="button"
              :disabled="creatingDomain"
              @click="submitCreateDomain"
            >
              {{ creatingDomain ? "创建中..." : "创建" }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ── 新建技能对话框 ────────────────────────────────────── -->
    <div v-if="showCreate" class="ext-modal-mask" @click.self="showCreate = false">
      <div class="ext-modal">
        <h2 class="ext-modal-title">新建技能</h2>
        <div class="ext-form">
          <label class="ext-field">
            <span>名称（英文标识，小写/数字/下划线）*</span>
            <input v-model.trim="createForm.name" placeholder="如 weekly_report" />
          </label>
          <label class="ext-field">
            <span>中文名</span>
            <input v-model.trim="createForm.display_name" placeholder="如：周报生成" />
          </label>
          <label class="ext-field">
            <span>所属 domain 包 *</span>
            <select v-model="createForm.domain">
              <option v-for="d in enabledDomains" :key="d.name" :value="d.name">
                {{ d.title }}（{{ d.name }}）
              </option>
            </select>
          </label>
          <label class="ext-field">
            <span>描述</span>
            <input v-model.trim="createForm.description" placeholder="这个技能做什么" />
          </label>
          <label class="ext-field">
            <span>默认执行模式</span>
            <select v-model="createForm.default_mode">
              <option value="">由模型选择</option>
              <option value="subagent">subagent（独立子代理）</option>
              <option value="inline">inline（主代理内联执行）</option>
            </select>
          </label>
          <div class="ext-field">
            <span>可用工具（勾选）</span>
            <div class="ext-checklist">
              <label v-for="t in availableTools" :key="t" class="ext-check">
                <input type="checkbox" :value="t" v-model="createForm.tools" /> {{ t }}
              </label>
              <p v-if="!availableTools.length" class="ext-dim">暂无可选工具</p>
            </div>
          </div>
          <div class="ext-field">
            <span>子技能（勾选）</span>
            <div class="ext-checklist">
              <template v-for="d in skillDomains" :key="d.name">
                <label v-for="s in d.items" :key="s.name" class="ext-check">
                  <input type="checkbox" :value="s.name" v-model="createForm.skills" />
                  {{ s.displayName }}（{{ s.name }}）
                </label>
              </template>
            </div>
          </div>
          <label class="ext-field">
            <span>标签（逗号分隔）</span>
            <input v-model.trim="createForm.tagsText" placeholder="如：报告,周报" />
          </label>
          <label class="ext-field">
            <span>工作流指令（Markdown，将作为技能的 system prompt）*</span>
            <textarea
              v-model="createForm.system_prompt"
              rows="8"
              placeholder="# 目标&#10;描述这个技能要完成的任务、执行步骤和输出格式要求。"
            ></textarea>
          </label>
          <p v-if="createError" class="ext-error">{{ createError }}</p>
          <div class="ext-modal-actions">
            <button class="ext-btn" type="button" @click="showCreate = false">取消</button>
            <button
              class="ext-btn ext-btn--primary"
              type="button"
              :disabled="creating"
              @click="submitCreate"
            >
              {{ creating ? "创建中..." : "创建" }}
            </button>
          </div>
        </div>
      </div>
    </div>
    <!-- ── 查看/编辑技能对话框 ───────────────────────────────── -->
    <div v-if="editView.open" class="ext-modal-mask" @click.self="closeEdit">
      <div class="ext-modal">
        <h2 class="ext-modal-title">查看 / 编辑技能：{{ editForm.name }}</h2>
        <p v-if="editView.loading" class="ext-dim">加载中...</p>
        <div v-else class="ext-form">
          <label class="ext-field">
            <span>名称（不可修改）</span>
            <input :value="editForm.name" disabled />
          </label>
          <label class="ext-field">
            <span>中文名</span>
            <input v-model.trim="editForm.display_name" placeholder="如：周报生成" />
          </label>
          <label class="ext-field">
            <span>描述</span>
            <input v-model.trim="editForm.description" placeholder="这个技能做什么" />
          </label>
          <label class="ext-field">
            <span>执行策略（mode）</span>
            <select v-model="editForm.mode">
              <option value="">未指定</option>
              <option value="auto">auto（由模型选择）</option>
              <option value="sequential">sequential（顺序执行）</option>
              <option value="parallel">parallel（并行执行）</option>
            </select>
          </label>
          <label class="ext-field">
            <span>默认执行模式</span>
            <select v-model="editForm.default_mode">
              <option value="">由模型选择</option>
              <option value="subagent">subagent（独立子代理）</option>
              <option value="inline">inline（主代理内联执行）</option>
            </select>
          </label>
          <div class="ext-field">
            <span>可用工具（勾选）</span>
            <div class="ext-checklist">
              <label v-for="t in availableTools" :key="t" class="ext-check">
                <input type="checkbox" :value="t" v-model="editForm.tools" /> {{ t }}
              </label>
              <p v-if="!availableTools.length" class="ext-dim">暂无可选工具</p>
            </div>
          </div>
          <div class="ext-field">
            <span>子技能（勾选）</span>
            <div class="ext-checklist">
              <template v-for="d in skillDomains" :key="d.name">
                <label
                  v-for="s in d.items.filter((x) => x.name !== editForm.name)"
                  :key="s.name"
                  class="ext-check"
                >
                  <input type="checkbox" :value="s.name" v-model="editForm.skills" />
                  {{ s.displayName }}（{{ s.name }}）
                </label>
              </template>
            </div>
          </div>
          <label class="ext-field">
            <span>标签（逗号分隔）</span>
            <input v-model.trim="editForm.tagsText" placeholder="如：报告,周报" />
          </label>
          <label class="ext-field">
            <span>工作流指令（Markdown，将作为技能的 system prompt）*</span>
            <textarea v-model="editForm.system_prompt" rows="14"></textarea>
          </label>
          <p class="ext-dim">保存后立即生效（新会话按最新技能文件执行）。</p>
          <p v-if="editView.error" class="ext-error">{{ editView.error }}</p>
          <div class="ext-modal-actions">
            <button class="ext-btn" type="button" @click="closeEdit">取消</button>
            <button
              class="ext-btn ext-btn--primary"
              type="button"
              :disabled="editView.saving"
              @click="submitEdit"
            >
              {{ editView.saving ? "保存中..." : "保存" }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { api, type DomainSkills, type PluginInfo, type SkillInfo } from "../api/client";

const tab = ref<"plugins" | "skills">("plugins");
const plugins = ref<PluginInfo[]>([]);
const skillDomains = ref<DomainSkills[]>([]);
const loading = ref(false);
const errorMsg = ref("");
const acting = ref("");
const toggling = ref("");

// Plugin log viewer state
const logView = reactive({
  open: false,
  name: "",
  lines: [] as string[],
  totalLines: 0,
  sizeBytes: 0,
  loading: false,
  error: "",
});
const logTail = ref(200);
const logAuto = ref(false);
let logTimer: ReturnType<typeof setInterval> | null = null;

const showCreate = ref(false);
const creating = ref(false);
const createError = ref("");
const showCreateDomain = ref(false);
const creatingDomain = ref(false);
const domainError = ref("");
const togglingDomain = ref("");
const domainForm = reactive({ name: "", title: "", description: "" });
const createForm = reactive({
  name: "",
  display_name: "",
  description: "",
  domain: "",
  default_mode: "",
  tools: [] as string[],
  skills: [] as string[],
  tagsText: "",
  system_prompt: "",
});

// Skill view/edit dialog state
const editView = reactive({ open: false, loading: false, saving: false, error: "" });
const editForm = reactive({
  name: "",
  domain: "",
  display_name: "",
  description: "",
  mode: "",
  default_mode: "",
  tools: [] as string[],
  skills: [] as string[],
  tagsText: "",
  system_prompt: "",
});

const skillCount = computed(() =>
  skillDomains.value.reduce((sum, d) => sum + d.items.length, 0),
);

const enabledDomains = computed(() =>
  skillDomains.value.filter((d) => d.enabled),
);

interface PluginGroup {
  key: string;
  title: string;
  items: PluginInfo[];
}

/** Plugins grouped like skills: shared first, then one section per domain. */
const pluginGroups = computed<PluginGroup[]>(() => {
  const groups: PluginGroup[] = [];
  const shared = plugins.value.filter((p) => p.source === "shared");
  if (shared.length) {
    groups.push({ key: "shared", title: "共享插件", items: shared });
  }
  const bySource = new Map<string, PluginInfo[]>();
  for (const p of plugins.value) {
    if (p.source === "shared") continue;
    bySource.set(p.source, [...(bySource.get(p.source) ?? []), p]);
  }
  for (const [source, items] of [...bySource.entries()].sort()) {
    const domain = skillDomains.value.find((d) => d.name === source);
    groups.push({
      key: source,
      title: domain ? `${domain.title}插件` : `${source} 插件`,
      items,
    });
  }
  return groups;
});

const availableTools = computed(() => {
  const names = new Set<string>();
  for (const p of plugins.value) {
    if (p.state === "ACTIVE") {
      for (const t of p.tools) names.add(t.name);
    }
  }
  return [...names].sort();
});

const STATE_LABELS: Record<string, string> = {
  ACTIVE: "运行中",
  CONNECTING: "连接中",
  REGISTERING: "注册中",
  DISCONNECTED: "已断连",
  BLOCKED: "配置错误",
  STOPPING: "停止中",
  STOPPED: "已停止",
  SCANNED: "已扫描",
  NOT_STARTED: "未启动",
};

function stateLabel(p: PluginInfo): string {
  if (p.scanStatus === "BLOCKED") return "被阻止";
  return STATE_LABELS[p.state] ?? p.state;
}

function stateClass(p: PluginInfo): string {
  if (p.scanStatus === "BLOCKED") return "ext-badge--err";
  switch (p.state) {
    case "ACTIVE":
      return "ext-badge--ok";
    case "BLOCKED":
      return "ext-badge--err";
    case "DISCONNECTED":
    case "CONNECTING":
    case "REGISTERING":
    case "STOPPING":
      return "ext-badge--warn";
    default:
      return "ext-badge--dim";
  }
}

function isTransitional(state: string): boolean {
  return ["CONNECTING", "REGISTERING", "DISCONNECTED", "STOPPING"].includes(state);
}

function modeLabel(s: SkillInfo): string {
  if (s.defaultMode) return `默认 ${s.defaultMode}`;
  return s.mode || "auto";
}

async function reload() {
  loading.value = true;
  errorMsg.value = "";
  try {
    const [p, s] = await Promise.all([api.listPlugins(), api.listSkillsAdmin()]);
    plugins.value = p.items;
    skillDomains.value = s.domains;
    if (!createForm.domain && s.domains.length) {
      const firstEnabled = s.domains.find((d) => d.enabled);
      if (firstEnabled) createForm.domain = firstEnabled.name;
    }
  } catch (e: unknown) {
    errorMsg.value = e instanceof Error ? e.message : "加载失败";
  } finally {
    loading.value = false;
  }
}

async function doAction(p: PluginInfo, action: "start" | "stop" | "restart") {
  acting.value = p.name;
  errorMsg.value = "";
  try {
    const result = await api.pluginAction(p.name, action);
    p.state = result.state;
    p.restartCount += action === "restart" ? 1 : 0;
    // Refresh status shortly after to catch transitional states.
    setTimeout(() => void reload(), 1500);
  } catch (e: unknown) {
    errorMsg.value = e instanceof Error ? e.message : "操作失败";
  } finally {
    acting.value = "";
  }
}

async function refreshLogs() {
  if (!logView.name) return;
  logView.loading = true;
  logView.error = "";
  try {
    const result = await api.getPluginLogs(logView.name, logTail.value);
    logView.lines = result.lines;
    logView.totalLines = result.totalLines;
    logView.sizeBytes = result.sizeBytes;
  } catch (e: unknown) {
    logView.error = e instanceof Error ? e.message : "读取日志失败";
  } finally {
    logView.loading = false;
  }
}

function openLog(p: PluginInfo) {
  logView.open = true;
  logView.name = p.name;
  logView.lines = [];
  void refreshLogs();
}

function stopLogTimer() {
  if (logTimer !== null) {
    clearInterval(logTimer);
    logTimer = null;
  }
}

function closeLog() {
  logView.open = false;
  logAuto.value = false;
  stopLogTimer();
}

watch(logAuto, (on) => {
  stopLogTimer();
  if (on) {
    logTimer = setInterval(() => void refreshLogs(), 3000);
  }
});

onUnmounted(stopLogTimer);

async function toggleSkill(s: SkillInfo, domain: string) {
  toggling.value = s.name;
  errorMsg.value = "";
  try {
    await api.setSkillEnabled(s.name, !s.enabled, domain);
    s.enabled = !s.enabled;
  } catch (e: unknown) {
    errorMsg.value = e instanceof Error ? e.message : "切换失败";
  } finally {
    toggling.value = "";
  }
}

async function toggleDomain(d: DomainSkills) {
  togglingDomain.value = d.name;
  errorMsg.value = "";
  try {
    await api.setDomainEnabled(d.name, !d.enabled);
    await reload();
  } catch (e: unknown) {
    errorMsg.value = e instanceof Error ? e.message : "切换失败";
  } finally {
    togglingDomain.value = "";
  }
}

async function submitCreateDomain() {
  domainError.value = "";
  creatingDomain.value = true;
  try {
    await api.createDomain({
      name: domainForm.name,
      title: domainForm.title || undefined,
      description: domainForm.description || undefined,
    });
    showCreateDomain.value = false;
    domainForm.name = "";
    domainForm.title = "";
    domainForm.description = "";
    await reload();
  } catch (e: unknown) {
    domainError.value = e instanceof Error ? e.message : "创建失败";
  } finally {
    creatingDomain.value = false;
  }
}

async function submitCreate() {
  createError.value = "";
  creating.value = true;
  try {
    await api.createSkill({
      name: createForm.name,
      domain: createForm.domain,
      display_name: createForm.display_name || undefined,
      description: createForm.description || undefined,
      default_mode: createForm.default_mode || undefined,
      tools: createForm.tools,
      skills: createForm.skills,
      tags: createForm.tagsText
        .split(/[,，]/)
        .map((t) => t.trim())
        .filter(Boolean),
      system_prompt: createForm.system_prompt,
    });
    showCreate.value = false;
    createForm.name = "";
    createForm.display_name = "";
    createForm.description = "";
    createForm.default_mode = "";
    createForm.tools = [];
    createForm.skills = [];
    createForm.tagsText = "";
    createForm.system_prompt = "";
    await reload();
  } catch (e: unknown) {
    createError.value = e instanceof Error ? e.message : "创建失败";
  } finally {
    creating.value = false;
  }
}

async function openEdit(s: SkillInfo, domain: string) {
  editView.open = true;
  editView.loading = true;
  editView.error = "";
  editForm.name = s.name;
  editForm.domain = domain;
  try {
    const detail = await api.getSkill(s.name, domain);
    editForm.display_name = detail.displayName === s.name ? "" : detail.displayName;
    editForm.description = detail.description;
    editForm.mode = detail.mode === "auto" ? "auto" : detail.mode || "";
    editForm.default_mode = detail.defaultMode;
    editForm.tools = [...detail.tools];
    editForm.skills = [...detail.skills];
    editForm.tagsText = detail.tags.join(",");
    editForm.system_prompt = detail.systemPrompt;
  } catch (e: unknown) {
    editView.error = e instanceof Error ? e.message : "加载技能详情失败";
  } finally {
    editView.loading = false;
  }
}

function closeEdit() {
  editView.open = false;
  editView.error = "";
}

async function submitEdit() {
  editView.error = "";
  if (!editForm.system_prompt.trim()) {
    editView.error = "工作流指令不能为空";
    return;
  }
  editView.saving = true;
  try {
    await api.updateSkill(editForm.name, {
      domain: editForm.domain,
      display_name: editForm.display_name || undefined,
      description: editForm.description || undefined,
      mode: editForm.mode || undefined,
      default_mode: editForm.default_mode || undefined,
      tools: editForm.tools,
      skills: editForm.skills,
      tags: editForm.tagsText
        .split(/[,，]/)
        .map((t) => t.trim())
        .filter(Boolean),
      system_prompt: editForm.system_prompt,
    });
    editView.open = false;
    await reload();
  } catch (e: unknown) {
    editView.error = e instanceof Error ? e.message : "保存失败";
  } finally {
    editView.saving = false;
  }
}

onMounted(reload);
</script>

<style scoped>
.ext-page {
  padding: 24px 32px 48px;
  max-width: 1100px;
}

.ext-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.ext-title {
  font-family: "Noto Sans SC", sans-serif;
  font-size: 26px;
  color: var(--chat-text-primary);
  margin: 0;
}

.ext-refresh {
  padding: 7px 18px;
  border: 1px solid var(--chat-border);
  border-radius: 8px;
  background: var(--chat-bg-card);
  color: var(--chat-text-secondary);
  font-size: 15px;
  cursor: pointer;
}

.ext-tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--chat-border);
  margin-bottom: 20px;
}

.ext-tab {
  padding: 8px 18px;
  border: none;
  background: transparent;
  font-size: 16px;
  color: var(--chat-text-secondary);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}

.ext-tab--active {
  color: var(--chat-accent);
  border-bottom-color: var(--chat-accent);
  font-weight: 600;
}

.ext-error {
  color: var(--err);
  font-size: 15px;
}

.ext-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px;
}

.ext-card {
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: 12px;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
}

.ext-card--disabled {
  opacity: 0.65;
}

.ext-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.ext-card-name {
  font-size: 18px;
  font-weight: 600;
  color: var(--chat-text-primary);
}

.ext-badge {
  padding: 1px 10px;
  border-radius: 999px;
  font-size: 13px;
}

.ext-badge--source {
  background: var(--chat-bg-hover);
  color: var(--chat-text-secondary);
}

.ext-badge--ok {
  background: rgba(90, 138, 74, 0.14);
  color: var(--ok-dim);
}

.ext-badge--warn {
  background: rgba(184, 122, 14, 0.14);
  color: var(--warn);
}

.ext-badge--err {
  background: var(--chat-accent-soft);
  color: var(--err);
}

.ext-badge--dim {
  background: var(--chat-bg-hover);
  color: var(--chat-text-tertiary);
}

.ext-card-desc {
  font-size: 15px;
  color: var(--chat-text-secondary);
  margin: 0 0 8px;
  min-height: 20px;
}

.ext-card-meta {
  font-size: 13px;
  color: var(--chat-text-tertiary);
  margin-bottom: 8px;
}

.ext-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}

.ext-chip {
  padding: 2px 10px;
  border-radius: 6px;
  background: var(--chat-bg-hover);
  border: 1px solid var(--chat-border);
  font-size: 13px;
  color: var(--chat-text-secondary);
}

.ext-chip--sub {
  border-style: dashed;
}

.ext-chip--tag {
  background: transparent;
}

.ext-card-error {
  font-size: 13px;
  color: var(--err);
  margin: 4px 0;
}

.ext-card-actions {
  display: flex;
  gap: 8px;
  margin-top: auto;
  padding-top: 12px;
  border-top: 1px solid var(--chat-border);
}

.ext-btn {
  padding: 6px 16px;
  border: 1px solid var(--chat-border);
  border-radius: 8px;
  background: var(--chat-bg-card);
  color: var(--chat-text-primary);
  font-size: 14px;
  cursor: pointer;
}

.ext-btn:disabled {
  opacity: 0.5;
  cursor: default;
}

.ext-btn--primary {
  background: var(--chat-accent);
  border-color: var(--chat-accent);
  color: #fff;
}

.ext-skill-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 16px;
}

.ext-domain {
  margin-bottom: 24px;
}

.ext-domain-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--chat-border);
}

.ext-domain-title {
  font-family: "Noto Sans SC", sans-serif;
  font-size: 19px;
  color: var(--chat-text-primary);
  margin: 0;
}

.ext-domain-empty {
  margin: 0 0 12px;
}

.ext-banner {
  background: rgba(184, 122, 14, 0.08);
  border: 1px solid var(--warn);
  border-radius: 8px;
  padding: 10px 14px;
  margin-bottom: 16px;
}

.ext-banner-item {
  font-size: 14px;
  color: var(--warn);
}

.ext-switch {
  margin-left: auto;
  position: relative;
  display: inline-flex;
  align-items: center;
  cursor: pointer;
  flex: none;
}

.ext-switch input {
  position: absolute;
  opacity: 0;
  width: 0;
  height: 0;
}

.ext-switch-track {
  position: relative;
  width: 38px;
  height: 22px;
  border-radius: 999px;
  background: var(--chat-bg-hover);
  border: 1px solid var(--chat-border);
  transition:
    background 0.15s ease,
    border-color 0.15s ease;
}

.ext-switch-track::after {
  content: "";
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--chat-text-tertiary);
  transition:
    transform 0.15s ease,
    background 0.15s ease;
}

.ext-switch input:checked + .ext-switch-track {
  background: rgba(90, 138, 74, 0.22);
  border-color: var(--ok-dim);
}

.ext-switch input:checked + .ext-switch-track::after {
  transform: translateX(16px);
  background: var(--ok-dim);
}

.ext-switch input:disabled + .ext-switch-track {
  opacity: 0.5;
  cursor: not-allowed;
}

.ext-modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(45, 37, 32, 0.4);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 60px;
  z-index: 200;
}

.ext-modal {
  background: var(--chat-bg-card);
  border-radius: var(--chat-radius-lg);
  width: 640px;
  max-width: 92vw;
  max-height: 84vh;
  overflow-y: auto;
  padding: 24px 28px;
}

.ext-modal--narrow {
  width: 480px;
}

.ext-modal--wide {
  width: 860px;
}

.ext-log-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.ext-log-select {
  padding: 6px 10px;
  border: 1px solid var(--chat-border);
  border-radius: 8px;
  background: var(--chat-bg-body);
  color: var(--chat-text-primary);
  font-size: 14px;
}

.ext-log-auto {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 14px;
  color: var(--chat-text-primary);
  cursor: pointer;
}

.ext-log-view {
  background: var(--chat-bg-hover);
  border: 1px solid var(--chat-border);
  border-radius: 10px;
  padding: 12px 14px;
  max-height: 56vh;
  overflow: auto;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}

.ext-log-empty {
  padding: 24px 0;
  text-align: center;
}

.ext-modal-title {
  font-family: "Noto Sans SC", sans-serif;
  font-size: 22px;
  color: var(--chat-text-primary);
  margin: 0 0 16px;
}

.ext-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 14px;
}

.ext-field > span {
  font-size: 15px;
  color: var(--chat-text-secondary);
  font-weight: 500;
}

.ext-field input,
.ext-field select,
.ext-field textarea {
  padding: 8px 12px;
  border: 1px solid var(--chat-border);
  border-radius: 8px;
  background: var(--chat-bg-card);
  font-size: 15px;
  color: var(--chat-text-primary);
  font-family: inherit;
}

.ext-field textarea {
  font-family: "JetBrains Mono", monospace;
  font-size: 14px;
  resize: vertical;
}

.ext-checklist {
  max-height: 140px;
  overflow-y: auto;
  border: 1px solid var(--chat-border);
  border-radius: 8px;
  padding: 8px 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
}

.ext-check {
  font-size: 14px;
  color: var(--chat-text-secondary);
  display: flex;
  align-items: center;
  gap: 4px;
}

.ext-dim {
  color: var(--chat-text-tertiary);
  font-size: 14px;
  margin: 0;
}

.ext-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 8px;
}
</style>
