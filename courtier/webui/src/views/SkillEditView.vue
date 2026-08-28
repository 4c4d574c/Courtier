<template>
  <div class="ext-page">
    <div class="ext-header">
      <div>
        <router-link to="/admin/skills" class="ext-back">← 返回技能列表</router-link>
        <h1 class="ext-title">查看 / 编辑技能：{{ name }}</h1>
        <p class="ext-subtitle">{{ domain }} domain 包 · 保存后立即生效（新会话按最新技能文件执行）</p>
      </div>
      <div class="ext-header-actions">
        <button class="ext-btn" type="button" @click="cancel">取消</button>
        <button
          class="ext-btn ext-btn--primary"
          type="button"
          :disabled="loading || saving"
          @click="submit"
        >
          {{ saving ? "保存中..." : "保存" }}
        </button>
      </div>
    </div>

    <p v-if="loadError" class="ext-error">{{ loadError }}</p>
    <p v-else-if="loading" class="ext-dim">加载中...</p>
    <div v-else class="ext-form ext-edit-form">
      <label class="ext-field">
        <span>名称（不可修改）</span>
        <input :value="form.name" disabled />
      </label>
      <label class="ext-field">
        <span>中文名</span>
        <input v-model.trim="form.display_name" placeholder="如：周报生成" />
      </label>
      <label class="ext-field">
        <span>描述</span>
        <input v-model.trim="form.description" placeholder="这个技能做什么" />
      </label>
      <label class="ext-field">
        <span>执行策略（mode）</span>
        <select v-model="form.mode">
          <option value="">未指定</option>
          <option value="auto">auto（由模型选择）</option>
          <option value="sequential">sequential（顺序执行）</option>
          <option value="parallel">parallel（并行执行）</option>
        </select>
      </label>
      <label class="ext-field">
        <span>默认执行模式</span>
        <select v-model="form.default_mode">
          <option value="">由模型选择</option>
          <option value="subagent">subagent（独立子代理）</option>
          <option value="inline">inline（主代理内联执行）</option>
        </select>
      </label>
      <div class="ext-field">
        <span>可用工具（勾选）</span>
        <div class="ext-checklist">
          <label v-for="t in availableTools" :key="t" class="ext-check">
            <input type="checkbox" :value="t" v-model="form.tools" /> {{ t }}
          </label>
          <p v-if="!availableTools.length" class="ext-dim">暂无可选工具</p>
        </div>
      </div>
      <div class="ext-field">
        <span>子技能（勾选）</span>
        <div class="ext-checklist">
          <template v-for="d in skillDomains" :key="d.name">
            <label
              v-for="s in d.items.filter((x) => x.name !== name)"
              :key="s.name"
              class="ext-check"
            >
              <input type="checkbox" :value="s.name" v-model="form.skills" />
              {{ s.displayName }}（{{ s.name }}）
            </label>
          </template>
        </div>
      </div>
      <label class="ext-field">
        <span>标签（逗号分隔）</span>
        <input v-model.trim="form.tagsText" placeholder="如：报告,周报" />
      </label>
      <label class="ext-field ext-field--wide">
        <span>工作流指令（Markdown，将作为技能的 system prompt）*</span>
        <textarea v-model="form.system_prompt" rows="22"></textarea>
      </label>
      <p v-if="saveError" class="ext-error">{{ saveError }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, type DomainSkills, type PluginInfo } from "../api/client";

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
      tags: form.tagsText
        .split(/[,，]/)
        .map((t) => t.trim())
        .filter(Boolean),
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

onMounted(load);
</script>

<style scoped src="../styles/extension.css"></style>

<style scoped>
.ext-back {
  display: inline-block;
  margin-bottom: 6px;
  font-size: 13px;
  color: var(--chat-text-tertiary);
  text-decoration: none;
}

.ext-back:hover {
  color: var(--chat-text-secondary);
}

.ext-header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* Full-page form: two comfortable columns on wide screens, one on narrow.
   The workflow-prompt field spans the full width. */
.ext-edit-form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 0 24px;
  align-items: start;
}

.ext-edit-form .ext-field--wide {
  grid-column: 1 / -1;
}
</style>
