<template>
  <div class="settings-page">
    <header class="settings-header">
      <h1>系统设置</h1>
      <div class="settings-meta">
        <span class="badge" :class="`mode-${view.mode}`">
          {{ view.mode === "db" ? "数据库配置" : "env-only（未接数据库）" }}
        </span>
        <span v-if="view.version !== null" class="badge">版本 {{ view.version }}</span>
        <span v-if="view.unreadable.length" class="badge warn">
          {{ view.unreadable.length }} 项密文不可读（加密密钥不匹配）
        </span>
      </div>
    </header>

    <p v-if="loading" class="settings-notice">加载中…</p>
    <p v-else-if="loadError" class="settings-notice">加载失败：{{ loadError }}</p>

    <p v-if="view.mode === 'env'" class="settings-notice">
      当前为 env-only 降级模式：设置不可编辑，需配置 MYSQL_URL 并重启后使用。
    </p>

    <nav class="settings-tabs">
      <button
        v-for="cat in view.categories"
        :key="cat.key"
        class="settings-tab"
        :class="{ active: cat.key === activeCategory }"
        @click="activeCategory = cat.key"
      >
        {{ cat.label }}
      </button>
    </nav>

    <section v-if="activeFields.length" class="settings-form">
      <div
        v-for="field in activeFields"
        :key="field.name"
        class="settings-field"
        :class="{ error: formErrors[field.name] }"
      >
        <label class="field-label">
          <span class="field-name">{{ field.name }}</span>
          <span class="field-badges">
            <span class="badge" :class="`source-${field.source}`">{{ sourceLabel(field.source) }}</span>
            <span class="badge" :class="`effect-${field.effect}`">{{ effectLabel(field.effect) }}</span>
          </span>
        </label>
        <p v-if="field.description" class="field-desc">{{ field.description }}</p>

        <input
          v-if="field.is_secret"
          v-model="formState[activeCategory][field.name]"
          class="field-input"
          type="password"
          autocomplete="new-password"
          :placeholder="secretPlaceholder(field)"
          :disabled="!editable"
        />
        <input
          v-else-if="field.type === 'bool'"
          v-model="formState[activeCategory][field.name]"
          class="field-checkbox"
          type="checkbox"
          :disabled="!editable"
        />
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

        <label v-if="field.source === 'db'" class="field-clear">
          <input v-model="cleared[activeCategory][field.name]" type="checkbox" :disabled="!editable" />
          清除（回退 env/默认）
        </label>
        <p v-if="formErrors[field.name]" class="field-error">{{ formErrors[field.name] }}</p>
      </div>

      <footer class="settings-actions">
        <button class="btn primary" :disabled="!editable || saving" @click="save">
          {{ saving ? "保存中…" : "保存本组" }}
        </button>
        <button
          v-if="activeCategory === 'model'"
          class="btn"
          :disabled="testing"
          @click="testLlm"
        >
          {{ testing ? "测试中…" : "测试 LLM 连接" }}
        </button>
        <span v-if="llmTest" class="llm-test" :class="llmTest.ok ? 'ok' : 'fail'">
          {{ llmTest.ok ? `连通（${llmTest.model}，${llmTest.reply}）` : `失败：${llmTest.error}` }}
        </span>
      </footer>

      <p v-if="saveBanner" class="save-banner" :class="{ restart: saveBanner.restart.length }">
        已保存 {{ saveBanner.applied.length }} 项
        <template v-if="saveBanner.cleared.length">，清除 {{ saveBanner.cleared.length }} 项</template>
        <template v-if="saveBanner.restart.length">
          ；{{ saveBanner.restart.join("、") }} 需重启容器后生效
        </template>
      </p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";

import { api, type SettingsView } from "../api/client";
import {
  buildUpdateBody,
  initFormState,
  secretPlaceholder,
  type FormState,
  type SettingsField,
} from "../utils/settingsForm";

const loading = ref(true);
const loadError = ref("");
const view = ref<SettingsView>({
  mode: "env",
  version: null,
  unreadable: [],
  categories: [],
});
const activeCategory = ref("");
const formState = reactive<Record<string, FormState>>({});
const cleared = reactive<Record<string, Record<string, boolean>>>({});
const formErrors = reactive<Record<string, string>>({});
const saving = ref(false);
const testing = ref(false);
const llmTest = ref<{ ok: boolean; model?: string; reply?: string; error?: string } | null>(null);
const saveBanner = ref<{ applied: string[]; cleared: string[]; restart: string[] } | null>(null);

const editable = computed(() => view.value.mode === "db");

const activeFields = computed<SettingsField[]>(
  () => view.value.categories.find((c) => c.key === activeCategory.value)?.fields ?? [],
);

function resetForms(data: SettingsView) {
  for (const cat of data.categories) {
    formState[cat.key] = initFormState(cat.fields);
    cleared[cat.key] = {};
  }
}

function sourceLabel(source: string): string {
  return { db: "数据库", env: "环境", default: "默认" }[source] ?? source;
}

function stringValue(name: string): string {
  const value = formState[activeCategory.value]?.[name];
  return typeof value === "string" ? value : String(value ?? "");
}

function setFieldValue(name: string, value: string) {
  const state = formState[activeCategory.value];
  if (state) state[name] = value;
}

function effectLabel(effect: string): string {
  return { hot: "即时生效", rebuild: "保存后重建", restart: "需重启" }[effect] ?? effect;
}

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    view.value = await api.getSettings();
    if (!activeCategory.value) {
      activeCategory.value = view.value.categories[0]?.key ?? "";
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
  if (Object.keys(errors).length) return;
  if (!Object.keys(body).length) {
    saveBanner.value = { applied: [], cleared: [], restart: [] };
    return;
  }
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

async function testLlm() {
  testing.value = true;
  llmTest.value = null;
  try {
    const overrides: Record<string, unknown> = {};
    for (const field of activeFields.value) {
      if (!field.name.startsWith("llm_")) continue;
      const value = formState["model"][field.name];
      if (typeof value === "string" && value !== "") overrides[field.name] = value;
    }
    llmTest.value = await api.testLlm(overrides);
  } catch (exc) {
    llmTest.value = { ok: false, error: String(exc) };
  } finally {
    testing.value = false;
  }
}

onMounted(load);
</script>

<style scoped src="../styles/admin.css"></style>
<style scoped>
.settings-page {
  max-width: 860px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.settings-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}
.settings-meta,
.field-badges {
  display: inline-flex;
  gap: 6px;
  align-items: center;
}
.badge {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 10px;
  background: rgba(127, 127, 127, 0.18);
}
.badge.mode-db,
.badge.source-db {
  background: rgba(46, 160, 67, 0.2);
}
.badge.warn {
  background: rgba(219, 88, 96, 0.25);
}
.badge.effect-restart {
  background: rgba(219, 88, 96, 0.18);
}
.badge.effect-rebuild {
  background: rgba(156, 79, 221, 0.18);
}
.settings-notice {
  color: #d08770;
}
.settings-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.settings-tab {
  border: 1px solid rgba(127, 127, 127, 0.3);
  background: transparent;
  color: inherit;
  padding: 6px 14px;
  border-radius: 8px;
  cursor: pointer;
}
.settings-tab.active {
  background: rgba(127, 127, 127, 0.2);
}
.settings-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.settings-field.error .field-input,
.settings-field.error .field-textarea {
  border-color: rgba(219, 88, 96, 0.8);
}
.field-label {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.field-name {
  font-weight: 600;
}
.field-desc {
  margin: 2px 0 6px;
  font-size: 12px;
  opacity: 0.7;
  white-space: pre-wrap;
}
.field-input {
  width: 100%;
  box-sizing: border-box;
  padding: 6px 10px;
  border-radius: 6px;
  border: 1px solid rgba(127, 127, 127, 0.35);
  background: transparent;
  color: inherit;
}
.field-textarea {
  min-height: 64px;
  font-family: monospace;
}
.field-clear {
  margin-top: 4px;
  font-size: 12px;
  opacity: 0.8;
}
.field-error {
  color: #e06c75;
  font-size: 12px;
  margin: 4px 0 0;
}
.settings-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}
.btn {
  padding: 6px 16px;
  border-radius: 8px;
  border: 1px solid rgba(127, 127, 127, 0.4);
  background: transparent;
  color: inherit;
  cursor: pointer;
}
.btn.primary {
  background: rgba(46, 160, 67, 0.3);
}
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.llm-test.ok {
  color: #98c379;
}
.llm-test.fail {
  color: #e06c75;
}
.save-banner {
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(46, 160, 67, 0.15);
}
.save-banner.restart {
  background: rgba(219, 88, 96, 0.15);
}
</style>
