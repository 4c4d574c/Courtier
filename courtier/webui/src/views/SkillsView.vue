<template>
  <div class="ext-page">
    <div class="ext-header">
      <div>
        <h1 class="ext-title">技能</h1>
        <p class="ext-subtitle">管理 domain 包与技能工作流</p>
      </div>
      <button class="ext-refresh" type="button" :disabled="loading" @click="reload">
        {{ loading ? "刷新中..." : "↻ 刷新" }}
      </button>
    </div>

    <p v-if="errorMsg" class="ext-error">{{ errorMsg }}</p>

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

    <!-- ── 新建 domain 包对话框 ──────────────────────────────── -->    <div v-if="showCreateDomain" class="ext-modal-mask" @click.self="showCreateDomain = false">
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
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { api, type DomainSkills, type PluginInfo, type SkillInfo } from "../api/client";

const router = useRouter();

// Plugins are fetched only to enumerate ACTIVE tools for the skill forms.
const plugins = ref<PluginInfo[]>([]);
const skillDomains = ref<DomainSkills[]>([]);
const loading = ref(false);
const errorMsg = ref("");
const toggling = ref("");

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

// Skill view/edit lives on its own page (/admin/skills/:domain/:name).

const enabledDomains = computed(() =>
  skillDomains.value.filter((d) => d.enabled),
);

const availableTools = computed(() => {
  const names = new Set<string>();
  for (const p of plugins.value) {
    if (p.state === "ACTIVE") {
      for (const t of p.tools) names.add(t.name);
    }
  }
  return [...names].sort();
});

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

// Skill view/edit lives on its own page (/admin/skills/:domain/:name).
function openEdit(s: SkillInfo, domain: string) {
  void router.push({ name: "AdminSkillEdit", params: { domain, name: s.name } });
}

onMounted(reload);
</script>

<style scoped src="../styles/extension.css"></style>
