<template>
  <div class="settings-page">
    <header class="settings-header">
      <div class="settings-heading">
        <h1>系统设置</h1>
        <p class="settings-subtitle">管理模型、检索、插件等运行时配置，按生效方式即时或延迟应用</p>
      </div>
      <div class="settings-status">
        <span class="status-chip" :class="view.mode === 'db' ? 'ok' : 'warn'">
          {{ view.mode === "db" ? "数据库配置" : "env 降级 · 只读" }}
        </span>
        <span v-if="view.version !== null" class="status-chip">v{{ view.version }}</span>
        <button class="refresh-btn" :disabled="loading" @click="load">↻ 刷新</button>
      </div>
    </header>

    <div v-if="view.unreadable.length" class="alert alert-error">
      {{ view.unreadable.length }} 项密文不可读：加密密钥与加密时不一致，对对应项重新保存即可修复。
    </div>
    <p v-if="loading && !view.categories.length" class="settings-notice">加载中…</p>
    <div v-else-if="loadError" class="alert alert-error">
      加载失败：{{ loadError }}
      <button class="link-btn" @click="load">重试</button>
    </div>
    <div v-if="view.mode === 'env' && !loadError" class="alert alert-warn">
      当前为 env-only 降级模式：设置不可编辑。配置 MYSQL_URL 并重启后启用数据库配置。
    </div>

    <div v-if="view.categories.length" class="settings-body">
      <nav class="settings-nav" aria-label="设置分类">
        <button
          v-for="cat in view.categories"
          :key="cat.key"
          class="nav-item"
          :class="{ active: !showDeployment && cat.key === activeCategory }"
          @click="selectCategory(cat.key)"
        >
          <span class="nav-label">{{ cat.label }}</span>
          <span class="nav-count">{{ cat.fields.length }}</span>
          <span
            v-if="hasDeferredEffect(cat)"
            class="nav-dot"
            :title="`${deferredCount(cat)} 项需重启容器或保存后重建`"
          />
        </button>
        <div class="nav-sep" />
        <button class="nav-item" :class="{ active: showDeployment }" @click="showDeployment = true">
          <span class="nav-label">部署层配置</span>
          <span class="nav-count ro">只读</span>
        </button>
      </nav>

      <div class="settings-pane">
        <div v-if="saveBanner" class="save-strip" :class="{ warn: saveBanner.restart.length }">
          <span>
            已保存 {{ saveBanner.applied.length }} 项<template v-if="saveBanner.cleared.length"
              >，清除 {{ saveBanner.cleared.length }} 项</template
            ><template v-if="saveBanner.restart.length">
              ；{{ saveBanner.restart.join("、") }} 需重启容器后生效</template
            >
          </span>
          <button class="strip-close" aria-label="关闭" @click="saveBanner = null">×</button>
        </div>

        <template v-if="!showDeployment">
          <header class="pane-head">
            <h2>{{ activeCategoryLabel }}</h2>
            <p>{{ CATEGORY_DESCRIPTIONS[activeCategory] }}</p>
            <span v-if="catEffectSummary" class="pane-hint">{{ catEffectSummary }}</span>
          </header>

          <section v-for="group in activeGroups" :key="group.key" class="group-card">
            <header class="group-head">
              <h3>{{ group.label }}</h3>
              <span v-if="groupHint(group)" class="group-hint">{{ groupHint(group) }}</span>
            </header>
            <div
              v-for="field in group.fields"
              :key="field.name"
              class="field-row"
              :class="{
                stacked: field.name === ENDPOINTS_FIELD,
                dirty: isFieldDirty(field.name),
                error: formErrors[field.name],
              }"
            >
              <div class="field-info">
                <div class="field-title">
                  <span class="field-name">{{ fieldDisplayName(field) }}</span>
                  <span v-if="isFieldDirty(field.name)" class="badge badge-dirty">已修改</span>
                  <span v-else-if="field.effect === 'restart'" class="badge badge-restart">需重启</span>
                  <span v-else-if="field.effect === 'rebuild'" class="badge badge-rebuild">保存后重建</span>
                </div>
                <div class="field-meta">
                  <code>{{ field.env_name }}</code>
                  <span v-if="field.source === 'db'" class="badge badge-db">数据库</span>
                </div>
                <p v-if="cleanedDesc(field)" class="field-desc">{{ cleanedDesc(field) }}</p>
              </div>
              <div class="field-control">
                <div v-if="field.name === ENDPOINTS_FIELD" class="endpoint-editor">
                  <div class="endpoint-head" aria-hidden="true">
                    <span>插件名称</span>
                    <span>端点地址</span>
                    <span />
                  </div>
                  <div v-for="(row, i) in endpointRows" :key="i" class="endpoint-row">
                    <input
                      v-model="row.name"
                      class="endpoint-input"
                      :disabled="!editable"
                      placeholder="插件名"
                      spellcheck="false"
                      @input="writeEndpoints"
                    />
                    <input
                      v-model="row.addr"
                      class="endpoint-input endpoint-input--addr"
                      :disabled="!editable"
                      placeholder="host:port"
                      spellcheck="false"
                      @input="writeEndpoints"
                    />
                    <button
                      class="endpoint-remove"
                      type="button"
                      :aria-label="`删除 ${row.name || '端点'}`"
                      :disabled="!editable"
                      @click="removeEndpointRow(i)"
                    >
                      ×
                    </button>
                  </div>
                  <button class="endpoint-add" type="button" :disabled="!editable" @click="addEndpointRow">
                    ＋ 添加端点
                  </button>
                </div>
                <div v-else-if="field.is_secret" class="secret-wrap">
                  <input
                    v-model="formState[activeCategory][field.name]"
                    class="field-input"
                    :type="revealed[field.name] ? 'text' : 'password'"
                    autocomplete="new-password"
                    :placeholder="secretPlaceholder(field)"
                    :disabled="!editable"
                  />
                  <button
                    class="secret-toggle"
                    type="button"
                    :disabled="!formState[activeCategory][field.name]"
                    @click="revealed[field.name] = !revealed[field.name]"
                  >
                    {{ revealed[field.name] ? "隐藏" : "显示" }}
                  </button>
                </div>
                <label v-else-if="field.type === 'bool'" class="switch-label">
                  <span class="switch">
                    <input
                      v-model="formState[activeCategory][field.name]"
                      type="checkbox"
                      :disabled="!editable"
                    />
                    <span class="track" />
                  </span>
                  <span class="switch-state">{{
                    formState[activeCategory][field.name] ? "已启用" : "已停用"
                  }}</span>
                </label>
                <textarea
                  v-else-if="field.type === 'list' || field.type === 'json'"
                  :value="stringValue(field.name)"
                  class="field-input field-textarea"
                  :disabled="!editable"
                  spellcheck="false"
                  @input="setFieldValue(field.name, ($event.target as HTMLTextAreaElement).value)"
                />
                <input
                  v-else
                  :value="stringValue(field.name)"
                  class="field-input"
                  :type="field.type === 'string' ? 'text' : 'number'"
                  :step="field.type === 'float' ? 'any' : '1'"
                  :disabled="!editable"
                  @input="setFieldValue(field.name, ($event.target as HTMLInputElement).value)"
                />

                <div v-if="field.source === 'db'" class="clear-zone">
                  <button
                    v-if="!cleared[activeCategory][field.name]"
                    class="clear-link"
                    type="button"
                    :disabled="!editable"
                    @click="cleared[activeCategory][field.name] = true"
                  >
                    清除并回退 env/默认
                  </button>
                  <span v-else class="cleared-chip">
                    保存后清除并回退 env/默认
                    <button
                      class="clear-link"
                      type="button"
                      :disabled="!editable"
                      @click="cleared[activeCategory][field.name] = false"
                    >
                      撤销
                    </button>
                  </span>
                </div>
                <p v-if="formErrors[field.name]" class="field-error">{{ formErrors[field.name] }}</p>
              </div>
            </div>
          </section>

          <section v-if="activeCategory === 'web'" class="group-card danger-zone">
            <header class="group-head">
              <h3>安全操作</h3>
              <span class="group-hint">立即生效且影响所有登录会话</span>
            </header>
            <div class="danger-row">
              <div class="danger-info">
                <span class="danger-title">轮换 JWT 签名密钥</span>
                <p>所有用户（包括你自己）的会话立即失效，需重新登录。</p>
              </div>
              <div class="danger-actions">
                <button class="btn danger" :disabled="rotating" @click="rotateJwt">
                  {{ rotating ? "轮换中…" : "轮换 JWT 密钥" }}
                </button>
                <span v-if="rotateResult" class="llm-test" :class="rotateResult.ok ? 'ok' : 'fail'">
                  {{ rotateResult.ok ? "已轮换：所有会话已失效，请重新登录" : `失败：${rotateResult.error}` }}
                </span>
              </div>
            </div>
          </section>

          <div v-if="testResult" class="test-card" :class="testResult.ok ? 'ok' : 'fail'">
            <div class="test-card-head">
              <span style="display:inline-flex;align-items:center;gap:4px;">
                <AppIcon :name="testResult.ok ? 'check' : 'x'" :size="13" />
                {{ testResult.ok ? "连接成功" : "连接失败" }}
              </span>
              <button class="strip-close" aria-label="关闭" @click="testResult = null">×</button>
            </div>
            <p v-if="testResult.ok" class="test-detail">{{ testResultText }}</p>
            <ul v-else class="test-errors">
              <li v-for="(msg, name) in testResult.errors ?? {}" :key="name">
                <code>{{ name }}</code> {{ msg }}
              </li>
              <li v-if="testResult.error">{{ testResult.error }}</li>
            </ul>
          </div>
        </template>

        <section v-else class="group-card">
          <header class="group-head">
            <h3>部署层配置（Tier 0 · 只读）</h3>
            <span class="group-hint">随容器环境变量注入，不落数据库</span>
          </header>
          <div v-for="item in view.deployment ?? []" :key="item.name" class="deploy-row">
            <code class="deploy-name">{{ item.env_name }}</code>
            <code class="deploy-value">{{ deployValue(item) }}</code>
          </div>
        </section>

        <footer v-if="!showDeployment" class="action-bar">
          <span class="dirty-summary" :class="{ none: !dirtyCount }">
            {{
              dirtyCount
                ? `${changedCount} 项已修改${clearedCount ? ` · ${clearedCount} 项待清除` : ""}`
                : "无更改"
            }}
          </span>
          <div class="action-buttons">
            <button v-if="dirtyCount" class="btn ghost" @click="abandon">放弃更改</button>
            <button
              v-for="target in testTargets"
              :key="target"
              class="btn"
              :disabled="testing"
              @click="testConnection(target)"
            >
              {{ testing ? "测试中…" : TEST_TARGET_LABELS[target] }}
            </button>
            <button
              class="btn primary"
              :disabled="!editable || saving || !dirtyCount"
              @click="save"
            >
              {{ saving ? "保存中…" : "保存更改" }}
            </button>
          </div>
        </footer>
      </div>
    </div>

    <div v-if="confirmState.open" class="modal-backdrop" @click.self="resolveConfirm(false)">
      <div class="modal" role="dialog" aria-modal="true" :aria-label="confirmState.title">
        <h3 class="modal-title">{{ confirmState.title }}</h3>
        <p class="modal-message">{{ confirmState.message }}</p>
        <div class="modal-actions">
          <button class="btn" @click="resolveConfirm(false)">取消</button>
          <button class="btn confirm-btn" @click="resolveConfirm(true)">
            {{ confirmState.confirmLabel }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import AppIcon from "../components/AppIcon.vue";
import { computed, onMounted, reactive, ref } from "vue";

import { api, type DeploymentField, type SettingsView } from "../api/client";
import {
  buildUpdateBody,
  initFormState,
  secretPlaceholder,
  type FormState,
  type SettingsCategory,
  type SettingsField,
} from "../utils/settingsForm";
import {
  CATEGORY_DESCRIPTIONS,
  cleanDescription,
  fieldDisplayName,
  groupCategoryFields,
  type SettingsGroup,
} from "../utils/settingsLabels";

const loading = ref(true);
const loadError = ref("");
const view = ref<SettingsView>({
  mode: "env",
  version: null,
  unreadable: [],
  categories: [],
});
const activeCategory = ref("");
const showDeployment = ref(false);
const formState = reactive<Record<string, FormState>>({});
const cleared = reactive<Record<string, Record<string, boolean>>>({});
/** Per-secret reveal state (field name → plaintext visible). */
const revealed = reactive<Record<string, boolean>>({});
const formErrors = reactive<Record<string, string>>({});
const saving = ref(false);
const testing = ref(false);
const rotating = ref(false);
const testResult = ref<
  { ok: boolean; model?: string; reply?: string; error?: string; errors?: Record<string, string> } | null
>(null);
const rotateResult = ref<{ ok: boolean; error?: string } | null>(null);
const saveBanner = ref<{ applied: string[]; cleared: string[]; restart: string[] } | null>(null);

/** Per-category connectivity tests offered in the action bar. */
const CATEGORY_TEST_TARGETS: Record<string, Array<"llm" | "es" | "minio" | "plugins">> = {
  model: ["llm"],
  retrieval: ["es", "minio"],
  plugins: ["plugins"],
};
const TEST_TARGET_LABELS: Record<string, string> = {
  llm: "测试 LLM 连接",
  es: "测试 Elasticsearch 连接",
  minio: "测试 MinIO 连接",
  plugins: "测试插件连接",
};
const testTargets = computed(() => CATEGORY_TEST_TARGETS[activeCategory.value] ?? []);

const testResultText = computed(() => {
  const result = testResult.value;
  if (!result) return "";
  if (result.ok) {
    return result.model ? `连通（${result.model}${result.reply ? `，${result.reply}` : ""}）` : "连通";
  }
  const detail = result.error ?? Object.values(result.errors ?? {}).join("；");
  return `失败：${detail}`;
});

/** Promise-based confirm dialog (replaces window.confirm). */
const confirmState = reactive<{
  open: boolean;
  title: string;
  message: string;
  confirmLabel: string;
  resolve: (ok: boolean) => void;
}>({ open: false, title: "", message: "", confirmLabel: "确认", resolve: () => {} });

function askConfirm(title: string, message: string, confirmLabel = "确认"): Promise<boolean> {
  return new Promise((resolve) => {
    confirmState.open = true;
    confirmState.title = title;
    confirmState.message = message;
    confirmState.confirmLabel = confirmLabel;
    confirmState.resolve = resolve;
  });
}

function resolveConfirm(ok: boolean) {
  confirmState.open = false;
  confirmState.resolve(ok);
}

/**
 * Plugin endpoint mapping editor: the stored value is a comma-separated
 * `name=host:port` string; the UI edits structured rows and writes the
 * serialized form back into formState, so dirty detection, save, and
 * connection tests keep using the unchanged string pipeline.
 */
const ENDPOINTS_FIELD = "courtier_plugin_endpoints";
const endpointRows = ref<Array<{ name: string; addr: string }>>([]);

function parseEndpoints(raw: string): Array<{ name: string; addr: string }> {
  return (raw ?? "")
    .split(",")
    .map((pair) => pair.trim())
    .filter(Boolean)
    .map((pair) => {
      const idx = pair.indexOf("=");
      return idx === -1
        ? { name: pair, addr: "" }
        : { name: pair.slice(0, idx).trim(), addr: pair.slice(idx + 1).trim() };
    });
}

function syncEndpointRows() {
  endpointRows.value = parseEndpoints(String(formState["plugins"]?.[ENDPOINTS_FIELD] ?? ""));
}

function writeEndpoints() {
  const state = formState["plugins"];
  if (state) {
    state[ENDPOINTS_FIELD] = endpointRows.value
      .map((r) => `${r.name.trim()}=${r.addr.trim()}`)
      .filter((s) => s !== "=")
      .join(",");
  }
}

function addEndpointRow() {
  endpointRows.value.push({ name: "", addr: "" });
}

function removeEndpointRow(index: number) {
  endpointRows.value.splice(index, 1);
  writeEndpoints();
}

/** A row must be fully filled or completely empty (unsaved placeholder). */
function endpointRowErrors(): string {
  const partial = endpointRows.value.some(
    (r) => (r.name.trim() === "") !== (r.addr.trim() === ""),
  );
  return partial ? "每行的插件名称与地址需同时填写" : "";
}

/**
 * Destructive-adjacent saves ask for confirmation: index/vector-dim changes
 * need a reindex, the plugin token is shared with plugin-side env, CORS
 * misconfiguration can lock the frontend out.
 */
const CONFIRM_RULES: Array<{
  fields: string[];
  message: string;
}> = [
  {
    fields: ["es_index_chunks", "es_index_results", "llm_embedding_dim"],
    message: "索引名/向量维度已变更：需要重建索引（reindex）才能对现有数据生效。",
  },
  {
    fields: ["courtier_plugin_token"],
    message:
      "插件 token 是主进程/插件双侧共享的：保存后插件将断开重连，插件侧 env 需同步修改，否则全部插件 401。",
  },
  {
    fields: ["cors_origins", "cors_allow_credentials"],
    message:
      "CORS 配置需重启后生效；配置错误可能导致前端无法访问（可用环境变量 CORS_ORIGINS 救援覆盖）。",
  },
];

async function confirmIfNeeded(changed: string[]): Promise<boolean> {
  for (const rule of CONFIRM_RULES) {
    if (changed.some((name) => rule.fields.includes(name))) {
      const ok = await askConfirm("确认保存更改？", rule.message, "确认保存");
      if (!ok) return false;
    }
  }
  return true;
}

const editable = computed(() => view.value.mode === "db");

const activeFields = computed<SettingsField[]>(
  () => view.value.categories.find((c) => c.key === activeCategory.value)?.fields ?? [],
);
const activeCategoryLabel = computed(
  () => view.value.categories.find((c) => c.key === activeCategory.value)?.label ?? "",
);
const activeGroups = computed<Array<SettingsGroup<SettingsField>>>(() =>
  groupCategoryFields(activeCategory.value, activeFields.value),
);

function selectCategory(key: string) {
  activeCategory.value = key;
  showDeployment.value = false;
}

function hasDeferredEffect(cat: SettingsCategory): boolean {
  return cat.fields.some((f) => f.effect !== "hot");
}
function deferredCount(cat: SettingsCategory): number {
  return cat.fields.filter((f) => f.effect !== "hot").length;
}

const catEffectSummary = computed(() => {
  const parts: string[] = [];
  const restart = activeFields.value.filter((f) => f.effect === "restart").length;
  const rebuild = activeFields.value.filter((f) => f.effect === "rebuild").length;
  if (restart) parts.push(`${restart} 项需重启容器生效`);
  if (rebuild) parts.push(`${rebuild} 项保存后重建生效`);
  return parts.join(" · ");
});

function groupHint(group: SettingsGroup<SettingsField>): string {
  const parts: string[] = [];
  const restart = group.fields.filter((f) => f.effect === "restart").length;
  const rebuild = group.fields.filter((f) => f.effect === "rebuild").length;
  if (restart) parts.push(`${restart} 项需重启容器`);
  if (rebuild) parts.push(`${rebuild} 项保存后重建`);
  return parts.join(" · ");
}

function cleanedDesc(field: SettingsField): string {
  const text = cleanDescription(field.description ?? "");
  return text && text !== fieldDisplayName(field) ? text : "";
}

/** Changed-only diff of the active category, driving dirty marks + action bar. */
const dirtyInfo = computed(() => {
  const cat = activeCategory.value;
  if (!cat || !formState[cat]) {
    return { changed: new Set<string>(), clearedNames: new Set<string>() };
  }
  const { body } = buildUpdateBody(activeFields.value, formState[cat], cleared[cat]);
  const changed = new Set<string>();
  const clearedNames = new Set<string>();
  for (const [name, value] of Object.entries(body)) {
    if (value === null) clearedNames.add(name);
    else changed.add(name);
  }
  return { changed, clearedNames };
});
const changedCount = computed(() => dirtyInfo.value.changed.size);
const clearedCount = computed(() => dirtyInfo.value.clearedNames.size);
const dirtyCount = computed(() => changedCount.value + clearedCount.value);

function isFieldDirty(name: string): boolean {
  return dirtyInfo.value.changed.has(name) || dirtyInfo.value.clearedNames.has(name);
}

function resetForms(data: SettingsView) {
  for (const cat of data.categories) {
    formState[cat.key] = initFormState(cat.fields);
    cleared[cat.key] = {};
  }
  for (const key of Object.keys(revealed)) delete revealed[key];
  syncEndpointRows();
}

function stringValue(name: string): string {
  const value = formState[activeCategory.value]?.[name];
  return typeof value === "string" ? value : String(value ?? "");
}

function setFieldValue(name: string, value: string) {
  const state = formState[activeCategory.value];
  if (state) state[name] = value;
}

function deployValue(item: DeploymentField): string {
  return typeof item.value === "string" ? item.value : item.value.set ? "已设置" : "未设置";
}

/** Discard unsaved edits of the active category. */
function abandon() {
  resetForms(view.value);
  for (const key of Object.keys(formErrors)) delete formErrors[key];
  saveBanner.value = null;
}

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    view.value = await api.getSettings();
    if (!activeCategory.value || !view.value.categories.some((c) => c.key === activeCategory.value)) {
      activeCategory.value = view.value.categories[0]?.key ?? "";
      showDeployment.value = false;
    }
    resetForms(view.value);
  } catch (exc) {
    loadError.value = String(exc);
  } finally {
    loading.value = false;
  }
}

async function save() {
  const cat = activeCategory.value;
  const { body, errors } = buildUpdateBody(activeFields.value, formState[cat], cleared[cat]);
  for (const key of Object.keys(formErrors)) delete formErrors[key];
  Object.assign(formErrors, errors);
  const endpointError = cat === "plugins" ? endpointRowErrors() : "";
  if (endpointError) formErrors[ENDPOINTS_FIELD] = endpointError;
  if (Object.keys(errors).length || endpointError) return;
  const changed = Object.keys(body);
  if (!changed.length) {
    saveBanner.value = { applied: [], cleared: [], restart: [] };
    return;
  }
  if (!(await confirmIfNeeded(changed))) return;
  saving.value = true;
  try {
    const result = await api.updateSettings(cat, body);
    saveBanner.value = {
      applied: result.applied,
      cleared: result.cleared,
      restart: result.restart_required,
    };
    // Sources and masked secret tails change after saving — reload.
    await load();
  } finally {
    saving.value = false;
  }
}

async function testConnection(target: "llm" | "es" | "minio" | "plugins") {
  testing.value = true;
  testResult.value = null;
  try {
    // Send unsaved edits of this category so the admin can test BEFORE saving.
    const { body } = buildUpdateBody(
      activeFields.value,
      formState[activeCategory.value],
      cleared[activeCategory.value],
    );
    testResult.value = await api.testConnection(target, body);
  } catch (exc) {
    testResult.value = { ok: false, error: String(exc) };
  } finally {
    testing.value = false;
  }
}

async function rotateJwt() {
  const ok = await askConfirm(
    "确认轮换 JWT 密钥？",
    "轮换会使所有用户（包括你自己）的会话立即失效，需重新登录。",
    "确认轮换",
  );
  if (!ok) return;
  rotating.value = true;
  rotateResult.value = null;
  try {
    await api.rotateJwtSecret();
    rotateResult.value = { ok: true };
  } catch (exc) {
    rotateResult.value = { ok: false, error: String(exc) };
  } finally {
    rotating.value = false;
  }
}

onMounted(load);
</script>

<style scoped>
.settings-page {
  max-width: 1160px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  font-size: 15px;
}

/* ---- Header ---- */
.settings-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
}
.settings-heading h1 {
  margin: 0;
  font-size: 26px;
  font-weight: 600;
  font-family: "Noto Sans SC", sans-serif;
  color: var(--chat-text-primary);
}
.settings-subtitle {
  margin-top: 4px;
  font-size: 13px;
  color: var(--chat-text-tertiary);
}
.settings-status {
  display: flex;
  align-items: center;
  gap: 8px;
}
.status-chip {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 999px;
  background: var(--chat-bg-hover);
  border: 1px solid var(--chat-border);
  color: var(--chat-text-secondary);
  white-space: nowrap;
}
.status-chip.ok {
  background: color-mix(in srgb, var(--ok) 13%, transparent);
  border-color: transparent;
  color: var(--ok);
}
.status-chip.warn {
  background: color-mix(in srgb, var(--warn) 13%, transparent);
  border-color: transparent;
  color: var(--warn);
}
.refresh-btn {
  height: 28px;
  padding: 0 12px;
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-sm);
  color: var(--chat-text-secondary);
  font-size: 13px;
  cursor: pointer;
  transition: background 150ms;
}
.refresh-btn:hover:not(:disabled) {
  background: var(--chat-bg-hover);
}
.refresh-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ---- Alerts ---- */
.alert {
  padding: 10px 14px;
  border-radius: var(--chat-radius-sm);
  font-size: 13px;
  line-height: 1.5;
}
.alert-error {
  background: color-mix(in srgb, var(--err) 10%, transparent);
  color: var(--err);
}
.alert-warn {
  background: color-mix(in srgb, var(--warn) 12%, transparent);
  color: var(--warn);
}
.settings-notice {
  color: var(--chat-text-tertiary);
}
.link-btn {
  background: none;
  border: none;
  padding: 0;
  color: inherit;
  text-decoration: underline;
  cursor: pointer;
  font-size: inherit;
}

/* ---- Two-pane body ---- */
.settings-body {
  display: flex;
  align-items: flex-start;
  gap: 24px;
}
.settings-nav {
  width: 200px;
  flex-shrink: 0;
  position: sticky;
  top: 24px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: none;
  background: transparent;
  color: var(--chat-text-primary);
  font-size: 14px;
  text-align: left;
  border-radius: var(--chat-radius-sm);
  cursor: pointer;
  transition: background 150ms;
}
.nav-item:hover {
  background: var(--chat-bg-hover);
}
.nav-item.active {
  background: var(--chat-accent-soft);
  color: var(--chat-accent);
  font-weight: 600;
}
.nav-label {
  flex: 1;
}
.nav-count {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--chat-bg-hover);
  color: var(--chat-text-tertiary);
}
.nav-item.active .nav-count {
  background: transparent;
  color: inherit;
  opacity: 0.7;
}
.nav-count.ro {
  background: transparent;
  border: 1px dashed var(--chat-border);
}
.nav-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--warn);
  flex-shrink: 0;
}
.nav-sep {
  height: 1px;
  background: var(--chat-border);
  margin: 8px 4px;
}

/* ---- Pane ---- */
.settings-pane {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.pane-head h2 {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
  color: var(--chat-text-primary);
}
.pane-head p {
  margin-top: 2px;
  font-size: 13px;
  color: var(--chat-text-tertiary);
}
.pane-hint {
  display: inline-block;
  margin-top: 6px;
  font-size: 12px;
  color: var(--warn);
}

.group-card {
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  box-shadow: var(--chat-shadow);
  padding: 0 20px;
}
.group-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 14px 0 10px;
  border-bottom: 1px solid var(--chat-border);
}
.group-head h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--chat-text-primary);
}
.group-hint {
  font-size: 12px;
  color: var(--chat-text-tertiary);
}

/* ---- Field rows ---- */
.field-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 380px;
  gap: 8px 24px;
  padding: 14px 0;
}
.field-row + .field-row {
  border-top: 1px solid var(--chat-border);
}
.field-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.field-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--chat-text-primary);
}
.field-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 3px;
}
.field-meta code {
  font-size: 11px;
  letter-spacing: 0.02em;
  color: var(--chat-text-tertiary);
}
.field-desc {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--chat-text-secondary);
  white-space: pre-wrap;
}

.field-control {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-self: center;
}
.field-input {
  width: 100%;
  padding: 7px 10px;
  border-radius: var(--chat-radius-sm);
  border: 1px solid var(--chat-border);
  background: var(--chat-bg-body);
  color: var(--chat-text-primary);
  font-size: 13px;
  outline: none;
  transition:
    border-color 150ms,
    box-shadow 150ms;
}
.field-input:focus {
  border-color: var(--chat-accent);
  box-shadow: 0 0 0 2px var(--chat-accent-soft);
}
.field-input:disabled {
  opacity: 0.55;
}
.field-textarea {
  min-height: 64px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  line-height: 1.5;
}
/* Secret input with inline reveal toggle. */
.secret-wrap {
  position: relative;
  display: flex;
  align-items: center;
}
.secret-wrap .field-input {
  padding-right: 52px;
}
.secret-toggle {
  position: absolute;
  right: 6px;
  padding: 2px 6px;
  background: none;
  border: none;
  border-radius: 4px;
  color: var(--chat-text-tertiary);
  font-size: 12px;
  cursor: pointer;
}
.secret-toggle:hover:not(:disabled) {
  color: var(--chat-text-primary);
  background: var(--chat-bg-hover);
}
.secret-toggle:disabled {
  cursor: default;
}

/* Toggle switch for bool fields. */
.switch-label {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
}
.switch {
  position: relative;
  display: inline-block;
  width: 38px;
  height: 21px;
  flex-shrink: 0;
}
.switch input {
  position: absolute;
  width: 0;
  height: 0;
  opacity: 0;
}
.switch .track {
  position: absolute;
  inset: 0;
  background: var(--chat-bg-hover);
  border: 1px solid var(--chat-border);
  border-radius: 999px;
  transition:
    background 150ms,
    border-color 150ms;
}
.switch .track::before {
  content: "";
  position: absolute;
  top: 2px;
  left: 2px;
  width: 15px;
  height: 15px;
  border-radius: 50%;
  background: var(--chat-bg-card);
  box-shadow: var(--chat-shadow);
  transition: transform 150ms;
}
.switch input:checked + .track {
  background: var(--chat-accent);
  border-color: var(--chat-accent);
}
.switch input:checked + .track::before {
  transform: translateX(17px);
}
.switch input:disabled + .track {
  opacity: 0.5;
  cursor: not-allowed;
}
.switch input:focus-visible + .track {
  box-shadow: 0 0 0 2px var(--chat-accent-soft);
}
.switch-state {
  font-size: 13px;
  color: var(--chat-text-secondary);
}

/* Clear-and-revert link (replaces the old clear checkbox). */
.clear-zone {
  min-height: 18px;
}
.clear-link {
  padding: 0;
  background: none;
  border: none;
  font-size: 12px;
  color: var(--chat-text-tertiary);
  text-decoration: underline;
  cursor: pointer;
}
.clear-link:hover:not(:disabled) {
  color: var(--err);
}
.clear-link:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.cleared-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  border-radius: 6px;
  font-size: 12px;
  background: color-mix(in srgb, var(--err) 10%, transparent);
  color: var(--err);
}
.cleared-chip .clear-link {
  color: inherit;
}
.field-row.error .field-input {
  border-color: var(--err);
}
.field-error {
  margin: 0;
  font-size: 12px;
  color: var(--err);
}

/* ---- Badges (denoised: only noteworthy states) ---- */
.badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 999px;
  white-space: nowrap;
}
.badge-db {
  background: var(--chat-accent-soft);
  color: var(--chat-accent);
}
.badge-dirty {
  background: var(--chat-accent-soft);
  color: var(--chat-accent);
}
.badge-restart {
  background: color-mix(in srgb, var(--err) 12%, transparent);
  color: var(--err);
}
.badge-rebuild {
  background: color-mix(in srgb, var(--warn) 14%, transparent);
  color: var(--warn);
}
.field-row.dirty .field-input {
  border-color: color-mix(in srgb, var(--chat-accent) 55%, transparent);
}

/* ---- Deployment (Tier 0, read-only) ---- */
.deploy-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 16px;
  padding: 9px 0;
}
.deploy-row + .deploy-row {
  border-top: 1px solid var(--chat-border);
}
.deploy-name {
  font-size: 12px;
  color: var(--chat-text-primary);
}
.deploy-value {
  font-size: 12px;
  color: var(--chat-text-secondary);
  text-align: right;
  word-break: break-all;
}

/* ---- Sticky action bar ---- */
.action-bar {
  position: sticky;
  bottom: 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  padding: 12px 16px;
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  box-shadow: var(--chat-shadow);
}
.dirty-summary {
  font-size: 13px;
  color: var(--chat-text-secondary);
}
.dirty-summary.none {
  color: var(--chat-text-tertiary);
}
.action-buttons {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.btn {
  height: 32px;
  padding: 0 14px;
  border-radius: var(--chat-radius-sm);
  border: 1px solid var(--chat-border);
  background: var(--chat-bg-card);
  color: var(--chat-text-primary);
  font-size: 13px;
  cursor: pointer;
  white-space: nowrap;
  transition: background 150ms;
}
.btn:hover:not(:disabled) {
  background: var(--chat-bg-hover);
}
.btn.primary {
  background: var(--chat-accent);
  border-color: var(--chat-accent);
  color: var(--chat-accent-contrast);
}
.btn.primary:hover:not(:disabled) {
  background: var(--chat-accent-hover);
}
.btn.ghost {
  background: transparent;
  border-color: transparent;
  color: var(--chat-text-secondary);
}
.btn.danger {
  background: transparent;
  border-color: color-mix(in srgb, var(--err) 40%, transparent);
  color: var(--err);
}
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.llm-test {
  font-size: 12px;
  max-width: 360px;
}
.llm-test.ok {
  color: var(--ok);
}
.llm-test.fail {
  color: var(--err);
}

/* ---- Save result strip ---- */
.save-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-radius: var(--chat-radius-sm);
  font-size: 13px;
  background: color-mix(in srgb, var(--ok) 12%, transparent);
  color: var(--ok-dim);
}
.save-strip.warn {
  background: color-mix(in srgb, var(--warn) 12%, transparent);
  color: var(--warn);
}
.strip-close {
  background: none;
  border: none;
  color: inherit;
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
  padding: 2px;
}

/* ---- Connection test result card ---- */
.test-card {
  padding: 12px 14px;
  border-radius: var(--chat-radius-sm);
  font-size: 13px;
}
.test-card.ok {
  background: color-mix(in srgb, var(--ok) 10%, transparent);
  color: var(--ok-dim);
}
.test-card.fail {
  background: color-mix(in srgb, var(--err) 10%, transparent);
  color: var(--err);
}
.test-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
}
.test-detail {
  margin: 6px 0 0;
  font-size: 12px;
}
.test-errors {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.test-errors code {
  opacity: 0.75;
  margin-right: 4px;
}

/* ---- Danger zone (JWT rotation) ---- */
.danger-zone .group-head {
  border-bottom-color: color-mix(in srgb, var(--err) 25%, transparent);
}
.danger-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  padding: 14px 0;
}
.danger-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--chat-text-primary);
}
.danger-info p {
  margin-top: 3px;
  font-size: 12px;
  color: var(--chat-text-secondary);
}
.danger-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

/* ---- Plugin endpoint mapping editor ----
   A bordered table-like list spanning the full row (the field row switches
   to .stacked): column headers, borderless row inputs with hover tint,
   and a full-width dashed add-row footer. */
.field-row.stacked {
  grid-template-columns: 1fr;
  row-gap: 10px;
}
.endpoint-editor {
  max-width: 640px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-sm);
  background: var(--chat-bg-body);
  overflow: hidden;
}
.endpoint-head,
.endpoint-row {
  display: grid;
  grid-template-columns: 200px 1fr 28px;
  gap: 8px;
  align-items: center;
}
.endpoint-head {
  padding: 7px 12px;
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--chat-text-tertiary);
  background: var(--chat-bg-hover);
}
.endpoint-row {
  padding: 0 12px;
}
.endpoint-row + .endpoint-row {
  border-top: 1px solid var(--chat-border);
}
.endpoint-row:hover {
  background: var(--chat-bg-hover);
}
.endpoint-input {
  width: 100%;
  padding: 8px 0;
  border: none;
  border-bottom: 1px solid transparent;
  border-radius: 0;
  background: transparent;
  color: var(--chat-text-primary);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  outline: none;
  transition: border-color 150ms;
}
.endpoint-input:focus {
  border-bottom-color: var(--chat-accent);
}
.endpoint-input:disabled {
  opacity: 0.55;
}
.endpoint-input::placeholder {
  font-family: "Noto Sans SC", sans-serif;
  color: var(--chat-text-tertiary);
}
.endpoint-remove {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--chat-text-tertiary);
  font-size: 18px;
  line-height: 1;
  border-radius: var(--chat-radius-sm);
  cursor: pointer;
  opacity: 0;
  transition:
    opacity 150ms,
    background 150ms,
    color 150ms;
}
.endpoint-row:hover .endpoint-remove,
.endpoint-remove:focus-visible {
  opacity: 1;
}
.endpoint-remove:hover:not(:disabled) {
  background: color-mix(in srgb, var(--err) 12%, transparent);
  color: var(--err);
}
.endpoint-remove:disabled {
  cursor: not-allowed;
}
.endpoint-add {
  display: block;
  width: 100%;
  padding: 8px 12px;
  border: none;
  border-top: 1px dashed var(--chat-border);
  background: transparent;
  color: var(--chat-text-tertiary);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  transition:
    background 150ms,
    color 150ms;
}
.endpoint-add:hover:not(:disabled) {
  background: var(--chat-bg-hover);
  color: var(--chat-accent);
}
.endpoint-add:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
@media (max-width: 768px) {
  .endpoint-head {
    display: none;
  }
  .endpoint-row {
    grid-template-columns: 1fr 1fr 28px;
  }
}

/* ---- Confirm modal ---- */
.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.45);
}
.modal {
  width: min(460px, calc(100vw - 48px));
  padding: 20px;
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.18);
}
.modal-title {
  margin: 0 0 8px;
  font-size: 16px;
  color: var(--chat-text-primary);
}
.modal-message {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--chat-text-secondary);
  white-space: pre-wrap;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 18px;
}
.confirm-btn {
  background: var(--chat-accent);
  border-color: var(--chat-accent);
  color: var(--chat-accent-contrast);
}
.confirm-btn:hover:not(:disabled) {
  background: var(--chat-accent-hover);
}

/* ---- Narrow screens: nav collapses to a horizontal scroller ---- */
@media (max-width: 1080px) {
  .settings-body {
    flex-direction: column;
  }
  .settings-nav {
    position: static;
    width: 100%;
    flex-direction: row;
    align-items: center;
    overflow-x: auto;
    padding-bottom: 4px;
  }
  .nav-item {
    flex-shrink: 0;
  }
  .nav-sep {
    display: none;
  }
  .field-row {
    grid-template-columns: 1fr;
    gap: 10px;
  }
  .field-control {
    align-self: stretch;
  }
}
</style>
