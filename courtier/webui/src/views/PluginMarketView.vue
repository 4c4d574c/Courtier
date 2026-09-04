<template>
  <div class="ext-page">
    <div class="ext-header">
      <div>
        <h1 class="ext-title">插件市场</h1>
        <p class="ext-subtitle">浏览已注册的插件进程，启停与查看日志</p>
      </div>
      <button class="ext-refresh" type="button" :disabled="loading" @click="reload">
        {{ loading ? "刷新中..." : "↻ 刷新" }}
      </button>
    </div>

    <p v-if="errorMsg" class="ext-error">{{ errorMsg }}</p>

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
    <p v-if="!loading && !pluginGroups.length && !errorMsg" class="ext-dim">
      暂无已注册的插件
    </p>

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
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { api, type DomainSkills, type PluginInfo } from "../api/client";

const plugins = ref<PluginInfo[]>([]);
// Domain titles only — used to name the per-domain plugin groups.
const skillDomains = ref<DomainSkills[]>([]);
const loading = ref(false);
const errorMsg = ref("");
const acting = ref("");

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

async function reload() {
  loading.value = true;
  errorMsg.value = "";
  try {
    const [p, s] = await Promise.all([api.listPlugins(), api.listSkillsAdmin()]);
    plugins.value = p.items;
    skillDomains.value = s.domains;
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

onMounted(reload);
</script>
