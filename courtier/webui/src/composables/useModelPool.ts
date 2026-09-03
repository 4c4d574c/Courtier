/**
 * Model pool state for the frontend selector (singleton).
 *
 * The pool is admin-configured and small; one module-level copy serves
 * the whole app.  Selection is sticky across sessions: restoring a
 * session preselects its last-used model (per-session provenance), and
 * every run sends the current selection as `modelId` — each run may
 * therefore use a different model.
 */

import { computed, ref } from "vue";

import { api } from "../api/client";

export interface ModelOption {
  id: string;
  name: string;
  /** Declared input modalities (vision/audio/video); empty = text-only. */
  modalities?: string[];
}

export interface ModelEndpointGroup {
  endpointId: string;
  endpointName: string;
  models: ModelOption[];
}

const endpoints = ref<ModelEndpointGroup[]>([]);
const defaultModelId = ref("");
const selectedModelId = ref("");
const loaded = ref(false);
const loading = ref(false);

function allModels(): ModelOption[] {
  return endpoints.value.flatMap((g) => g.models);
}

function currentModelName(): string {
  const found = allModels().find((m) => m.id === selectedModelId.value);
  return found?.name ?? "";
}

function currentModel(): ModelOption | undefined {
  return allModels().find((m) => m.id === selectedModelId.value);
}

/** Whether the currently selected model declares the given attachment kind's
 * modality. Scalar-fallback (no pool / unknown id) is text-only. */
function supportsKind(kind: string): boolean {
  const model = currentModel();
  if (!model) return false;
  const modality = kind === "image" ? "vision" : kind;
  return (model.modalities ?? []).includes(modality);
}

async function loadModels(force = false): Promise<void> {
  if (loading.value || (loaded.value && !force)) return;
  loading.value = true;
  try {
    const data = await api.getModels();
    endpoints.value = data.endpoints;
    defaultModelId.value = data.defaultModelId;
    // Drop a selection that no longer resolves (removed / disabled).
    if (
      selectedModelId.value &&
      !allModels().some((m) => m.id === selectedModelId.value)
    ) {
      selectedModelId.value = "";
    }
    if (!selectedModelId.value) selectedModelId.value = data.defaultModelId;
    loaded.value = true;
  } finally {
    loading.value = false;
  }
}

function selectModel(id: string): void {
  selectedModelId.value = id;
}

/** Preselect from a session's persisted last-used model (per-session provenance). */
function preselectModel(modelId: string | undefined | null): void {
  if (modelId && allModels().some((m) => m.id === modelId)) {
    selectedModelId.value = modelId;
  }
}

export function useModelPool() {
  return {
    endpoints,
    defaultModelId,
    selectedModelId,
    loaded,
    currentModelName,
    loadModels,
    selectModel,
    preselectModel,
    supportsKind,
    modelName: computed(() =>
      endpoints.value.length ? currentModelName() : "",
    ),
    selectedModelModalities: computed(() => currentModel()?.modalities ?? []),
  };
}
