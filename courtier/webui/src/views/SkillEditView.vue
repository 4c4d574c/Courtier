<template>
  <div class="se-page">
    <div class="se-topbar">
      <router-link to="/admin/skills" class="se-back"><AppIcon name="arrow-left" :size="14" />技能列表</router-link>
      <div class="se-actions">
        <button class="ext-btn" type="button" :disabled="saving" @click="cancel">取消</button>
        <button
          class="ext-btn ext-btn--primary"
          type="button"
          :disabled="loading || saving"
          @click="submit"
        >
          {{ saving ? "保存中..." : "保存更改" }}
        </button>
      </div>
    </div>

    <p v-if="loadError" class="ext-error">{{ loadError }}</p>
    <p v-else-if="loading" class="se-loading">加载中...</p>

    <template v-else>
      <header class="se-head">
        <h1 class="se-title">{{ form.display_name || name }}</h1>
        <div class="se-head-meta">
          <code class="se-handle">{{ name }}</code>
          <span class="ext-badge ext-badge--source">{{ domain }}</span>
          <span class="ext-badge ext-badge--dim">{{ form.mode || "auto" }}</span>
          <span v-if="form.default_mode" class="ext-badge ext-badge--dim">
            默认 {{ form.default_mode }}
          </span>
          <span
            v-for="tag in tagsPreview"
            :key="tag"
            class="ext-badge ext-badge--tag-like"
            >#{{ tag }}</span
          >
        </div>
        <p v-if="form.description" class="se-desc">{{ form.description }}</p>
      </header>

      <div class="se-layout">
        <!-- Workflow prompt: the main event, fills the viewport height. -->
        <section class="se-card se-editor-card">
          <div class="se-card-head">
            <h2 class="se-card-title">工作流指令</h2>
            <span class="se-card-hint">
              Markdown · 作为该技能的 system prompt · {{ form.system_prompt.length }} 字符
            </span>
          </div>
          <textarea
            v-model="form.system_prompt"
            class="se-editor"
            spellcheck="false"
            placeholder="# 目标&#10;描述这个技能要完成的任务、执行步骤和输出格式要求。"
          ></textarea>
          <p v-if="saveError" class="ext-error se-save-error">{{ saveError }}</p>
        </section>

        <aside class="se-side">
          <section class="se-card">
            <h2 class="se-card-title">基本信息</h2>
            <label class="se-field">
              <span>中文名</span>
              <input v-model.trim="form.display_name" placeholder="如：周报生成" />
            </label>
            <label class="se-field">
              <span>描述</span>
              <input v-model.trim="form.description" placeholder="这个技能做什么" />
            </label>
            <div class="se-field-row">
              <label class="se-field">
                <span>执行策略</span>
                <select v-model="form.mode">
                  <option value="">未指定</option>
                  <option value="auto">auto</option>
                  <option value="sequential">sequential</option>
                  <option value="parallel">parallel</option>
                </select>
              </label>
              <label class="se-field">
                <span>默认模式</span>
                <select v-model="form.default_mode">
                  <option value="">由模型选择</option>
                  <option value="subagent">subagent</option>
                  <option value="inline">inline</option>
                </select>
              </label>
            </div>
            <label class="se-field">
              <span>标签（逗号分隔）</span>
              <input v-model.trim="form.tagsText" placeholder="如：报告,周报" />
            </label>
          </section>

          <section class="se-card">
            <div class="se-card-head">
              <h2 class="se-card-title">可用工具</h2>
              <span class="se-card-hint">{{ form.tools.length }}/{{ availableTools.length }}</span>
            </div>
            <div class="se-chipbox">
              <button
                v-for="t in availableTools"
                :key="t"
                type="button"
                class="se-chip"
                :class="{ 'se-chip--on': form.tools.includes(t) }"
                @click="toggleIn(form.tools, t)"
              >
                {{ t }}
              </button>
              <p v-if="!availableTools.length" class="ext-dim">暂无可选工具</p>
            </div>
          </section>

          <section class="se-card">
            <div class="se-card-head">
              <h2 class="se-card-title">子技能</h2>
              <span class="se-card-hint">{{ form.skills.length }}/{{ skillChoices.length }}</span>
            </div>
            <div class="se-chipbox">
              <button
                v-for="s in skillChoices"
                :key="s.name"
                type="button"
                class="se-chip"
                :class="{ 'se-chip--on': form.skills.includes(s.name) }"
                @click="toggleIn(form.skills, s.name)"
              >
                {{ s.displayName }}
              </button>
              <p v-if="!skillChoices.length" class="ext-dim">暂无其他技能</p>
            </div>
          </section>
        </aside>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, type DomainSkills, type PluginInfo, type SkillInfo } from "../api/client";

const route = useRoute();
const router = useRouter();

// Route params — the skill identity is the page's address, so a reload or a
// shared link lands on the same skill.
const name = String(route.params.name ?? "");
const domain = String(route.params.domain ?? "");

const loading = ref(false);
const loadError = ref("");
const saving = ref(false);
const saveError = ref("");

const plugins = ref<PluginInfo[]>([]);
const skillDomains = ref<DomainSkills[]>([]);

const form = reactive({
  name,
  display_name: "",
  description: "",
  mode: "",
  default_mode: "",
  tools: [] as string[],
  skills: [] as string[],
  tagsText: "",
  system_prompt: "",
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

// Every other skill, any domain — the sub-skill candidates.
const skillChoices = computed(() =>
  skillDomains.value
    .flatMap((d) => d.items)
    .filter((s: SkillInfo) => s.name !== name),
);

const tagsPreview = computed(() =>
  form.tagsText
    .split(/[,，]/)
    .map((t) => t.trim())
    .filter(Boolean),
);

function toggleIn(list: string[], value: string) {
  const idx = list.indexOf(value);
  if (idx >= 0) list.splice(idx, 1);
  else list.push(value);
}

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    // The sub-skill checklist needs the full skill list; plugins enumerate
    // the ACTIVE tools. Both are independent of the skill detail.
    const [detail, p, s] = await Promise.all([
      api.getSkill(name, domain),
      api.listPlugins(),
      api.listSkillsAdmin(),
    ]);
    plugins.value = p.items;
    skillDomains.value = s.domains;
    form.display_name = detail.displayName === name ? "" : detail.displayName;
    form.description = detail.description;
    form.mode = detail.mode === "auto" ? "auto" : detail.mode || "";
    form.default_mode = detail.defaultMode;
    form.tools = [...detail.tools];
    form.skills = [...detail.skills];
    form.tagsText = detail.tags.join(",");
    form.system_prompt = detail.systemPrompt;
  } catch (e: unknown) {
    loadError.value = e instanceof Error ? e.message : "加载技能详情失败";
  } finally {
    loading.value = false;
  }
}

async function submit() {
  saveError.value = "";
  if (!form.system_prompt.trim()) {
    saveError.value = "工作流指令不能为空";
    return;
  }
  saving.value = true;
  try {
    await api.updateSkill(name, {
      domain,
      display_name: form.display_name || undefined,
      description: form.description || undefined,
      mode: form.mode || undefined,
      default_mode: form.default_mode || undefined,
      tools: form.tools,
      skills: form.skills,
      tags: tagsPreview.value,
      system_prompt: form.system_prompt,
    });
    await router.push({ name: "AdminSkills" });
  } catch (e: unknown) {
    saveError.value = e instanceof Error ? e.message : "保存失败";
  } finally {
    saving.value = false;
  }
}

function cancel() {
  void router.push({ name: "AdminSkills" });
}

// Ctrl/Cmd+S saves, like any editor.
function onKeydown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if (!loading.value && !loadError.value && !saving.value) void submit();
  }
}

onMounted(() => {
  void load();
  window.addEventListener("keydown", onKeydown);
});

onUnmounted(() => {
  window.removeEventListener("keydown", onKeydown);
});
</script>

<style scoped>
.se-page {
  max-width: 1240px;
}

.se-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.se-back {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 13px;
  color: var(--chat-text-tertiary);
  text-decoration: none;
  transition: color 150ms;
}

.se-back:hover {
  color: var(--chat-text-primary);
}

.se-actions {
  display: flex;
  gap: 8px;
}

.se-loading {
  padding: 32px 0;
  font-size: 14px;
  color: var(--chat-text-secondary);
}

.se-head {
  margin-bottom: 18px;
}

.se-title {
  font-family: "Noto Sans SC", sans-serif;
  font-size: 24px;
  font-weight: 600;
  color: var(--chat-text-primary);
  margin: 0 0 8px;
}

.se-head-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.se-handle {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: var(--chat-text-secondary);
  background: var(--chat-bg-hover);
  border-radius: 6px;
  padding: 2px 8px;
}

.ext-badge--tag-like {
  background: transparent;
  color: var(--chat-text-tertiary);
}

.se-desc {
  margin: 8px 0 0;
  font-size: 13px;
  color: var(--chat-text-secondary);
}

/* Editor left, metadata sidebar right; collapses to one column on narrow. */
.se-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 340px;
  gap: 16px;
  align-items: start;
}

@media (max-width: 1080px) {
  .se-layout {
    grid-template-columns: minmax(0, 1fr);
  }
}

.se-card {
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  box-shadow: var(--chat-shadow);
  padding: 14px 16px;
}

.se-card + .se-card {
  margin-top: 16px;
}

.se-card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}

.se-card-title {
  font-family: "Noto Sans SC", sans-serif;
  font-size: 14px;
  font-weight: 600;
  color: var(--chat-text-primary);
  margin: 0;
}

.se-card-hint {
  font-size: 12px;
  color: var(--chat-text-tertiary);
}

/* The editor stretches with the viewport instead of a fixed row count. */
.se-editor-card {
  display: flex;
  flex-direction: column;
}

.se-editor {
  flex: 1;
  min-height: clamp(420px, calc(100vh - 330px), 820px);
  padding: 12px 14px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-sm);
  background: var(--chat-bg-body);
  color: var(--chat-text-primary);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  line-height: 1.7;
  resize: vertical;
  outline: none;
  transition:
    border-color 150ms,
    box-shadow 150ms;
}

.se-editor:focus {
  border-color: var(--chat-accent);
  box-shadow: 0 0 0 2px var(--chat-accent-soft);
}

.se-save-error {
  margin: 8px 0 0;
}

.se-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-bottom: 12px;
}

.se-field:last-child {
  margin-bottom: 0;
}

.se-field > span {
  font-size: 12px;
  font-weight: 500;
  color: var(--chat-text-secondary);
}

.se-field input,
.se-field select {
  width: 100%;
  padding: 7px 10px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-sm);
  background: var(--chat-bg-body);
  color: var(--chat-text-primary);
  font-size: 13px;
  font-family: inherit;
  outline: none;
  transition:
    border-color 150ms,
    box-shadow 150ms;
}

.se-field input:focus,
.se-field select:focus {
  border-color: var(--chat-accent);
  box-shadow: 0 0 0 2px var(--chat-accent-soft);
}

.se-field-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

/* Selectable pills instead of raw checkbox lists. */
.se-chipbox {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  max-height: 200px;
  overflow-y: auto;
}

.se-chip {
  padding: 3px 11px;
  border: 1px solid var(--chat-border);
  border-radius: 999px;
  background: var(--chat-bg-body);
  color: var(--chat-text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition:
    color 120ms,
    border-color 120ms,
    background 120ms;
}

.se-chip:hover {
  border-color: var(--chat-text-tertiary);
  color: var(--chat-text-primary);
}

.se-chip--on {
  background: color-mix(in srgb, var(--chat-accent) 13%, transparent);
  border-color: var(--chat-accent);
  color: var(--chat-accent);
}

.se-chip--on::before {
  content: "";
  display: inline-block;
  width: 10px;
  height: 10px;
  margin-right: 4px;
  vertical-align: -1px;
  background: currentColor;
  -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='20 6 9 17 4 12'/%3E%3C/svg%3E") no-repeat center / contain;
  mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='20 6 9 17 4 12'/%3E%3C/svg%3E") no-repeat center / contain;
}
</style>
