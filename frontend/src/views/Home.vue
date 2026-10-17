<script setup lang="ts">
import axios from "axios";
import { computed, reactive, ref, watch } from "vue";
import { message } from "ant-design-vue";

import { generateTrip } from "../services/api";
import type {
  Itinerary,
  TripRequestPayload,
} from "../types";

const emit = defineEmits<{
  generated: [itinerary: Itinerary];
}>();

const preferenceOptions = [
  "自然风景",
  "拍照",
  "美食",
  "古镇",
  "休闲",
];

const dietaryOptions = [
  "少辣",
  "不吃香菜",
  "不吃葱",
];

function formatDate(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

const today = new Date();
const todayPlus2 = new Date(today);
todayPlus2.setDate(todayPlus2.getDate() + 2);

const formState = reactive({
  destination: "大理",
  startDate: formatDate(today),
  endDate: formatDate(todayPlus2),
  travelers: 2,
  budget: 3200,
  hotelLevel: "舒适型",
  pace: "轻松",
  preferences: ["自然风景", "拍照", "美食"],
  dietaryPreferences: ["少辣"],
  notes: "不想太早起床，希望安排一个适合看日落的地点。",
});

const isSubmitting = ref(false);
const destinationInput = ref<HTMLInputElement | null>(null);

interface CandidateGap {
  category: string;
  count: number;
  minimum: number;
  shortage: number;
}

interface GenerationError {
  code: string | null;
  message: string;
  guidance: string;
  candidateGaps: CandidateGap[];
  provinceSuggestion: string | null;
}

const generationError = ref<GenerationError | null>(null);

watch(
  () => formState.destination,
  () => {
    generationError.value = null;
  },
);

const dayCount = computed(() => {
  const start = new Date(formState.startDate);
  const end = new Date(formState.endDate);
  const diff = end.getTime() - start.getTime();
  return Number.isNaN(diff) ? 0 : Math.max(Math.floor(diff / 86400000) + 1, 0);
});

function togglePreference(list: string[], value: string) {
  const idx = list.indexOf(value);
  if (idx >= 0) {
    list.splice(idx, 1);
  } else {
    list.push(value);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function parseGenerationError(data: unknown): GenerationError {
  const rawDetail = isRecord(data) ? data.detail : null;
  const detail = isRecord(rawDetail) ? rawDetail : null;
  const code = typeof detail?.code === "string" ? detail.code : null;
  const backendMessage =
    typeof detail?.message === "string"
      ? detail.message
      : typeof rawDetail === "string"
        ? rawDetail
        : null;
  const counts = isRecord(detail?.candidate_counts) ? detail.candidate_counts : {};
  const minimums = isRecord(detail?.minimum_counts) ? detail.minimum_counts : {};
  const shortages = isRecord(detail?.shortages) ? detail.shortages : {};
  const categoryLabels: Record<string, string> = {
    spot: "景点",
    meal: "餐饮",
    hotel: "住宿",
  };
  const candidateGaps = Object.entries(shortages).flatMap(([category, value]) => {
    const shortage = typeof value === "number" ? value : 0;
    const count = counts[category];
    const minimum = minimums[category];
    if (
      shortage <= 0 ||
      typeof count !== "number" ||
      typeof minimum !== "number"
    ) {
      return [];
    }
    return [{
      category: categoryLabels[category] ?? category,
      count,
      minimum,
      shortage,
    }];
  });
  const destination =
    typeof detail?.destination === "string" ? detail.destination : "";
  const suggestedCity = backendMessage?.match(/例如输入“([^”]+)”/)?.[1] ?? null;

  let guidance = "请检查填写内容后重试，或调整目的地与条件。";
  if (code === "invalid_destination" || code === "insufficient_city_data") {
    guidance = "系统没有确认这个目的地，请核对城市名称；如有重名，请补充省份。系统不会自动替你猜测城市。";
  } else if (code === "unsupported_destination_scope") {
    guidance = "目前仅支持按单个城市规划，请改填具体城市。";
  } else if (code === "insufficient_candidate_data") {
    guidance = "当前城市可核实的地点候选没有达到最低要求。可换一个目的地，或稍后重试；系统不会用虚构地点补齐。";
  } else if (code === "dietary_candidate_unavailable") {
    guidance = "当前城市没有足够符合饮食限制的餐饮候选。可放宽饮食限制后重试，或更换目的地。";
  } else if (code === "candidate_collection_unavailable") {
    guidance = "地点数据服务暂时不可用，可以稍后按原条件重试。";
  }

  return {
    code,
    message: backendMessage ?? "行程生成失败，请调整条件后重试。",
    guidance,
    candidateGaps,
    provinceSuggestion:
      code === "unsupported_destination_scope" && destination
        ? suggestedCity
        : null,
  };
}

function focusDestination() {
  destinationInput.value?.focus();
  destinationInput.value?.select();
}

async function handleSubmit() {
  const payload: TripRequestPayload = {
    destination: formState.destination,
    start_date: formState.startDate,
    end_date: formState.endDate,
    travelers: formState.travelers,
    budget: formState.budget,
    preferences: formState.preferences,
    pace: formState.pace,
    dietary_preferences: formState.dietaryPreferences,
    hotel_level: formState.hotelLevel,
    special_notes: formState.notes,
  };

  isSubmitting.value = true;
  generationError.value = null;
  try {
    const itinerary = await generateTrip(payload);
    message.success("行程生成成功，已切换到结果页。");
    emit("generated", itinerary);
  } catch (error) {
    console.error(error);
    if (axios.isAxiosError(error)) {
      if (error.code === "ECONNABORTED") {
        generationError.value = {
          code: null,
          message: "行程生成超时，请稍后再试。",
          guidance: "请求可能仍在处理中；确认服务状态后，可以按原条件重新提交。",
          candidateGaps: [],
          provinceSuggestion: null,
        };
      } else if (error.response) {
        generationError.value = parseGenerationError(error.response.data);
        const responseDetail = isRecord(error.response.data)
          ? error.response.data.detail
          : null;
        if (!isRecord(responseDetail) && typeof responseDetail !== "string") {
          generationError.value.message = `行程生成失败：后端返回 ${error.response.status}。`;
        }
      } else {
        generationError.value = {
          code: null,
          message: "行程生成失败，暂时无法连接后端。",
          guidance: "请检查服务是否启动以及前后端连接后重试。",
          candidateGaps: [],
          provinceSuggestion: null,
        };
      }
    } else {
      generationError.value = {
        code: null,
        message: "行程生成失败。",
        guidance: "请检查后端地址或服务状态后重试。",
        candidateGaps: [],
        provinceSuggestion: null,
      };
    }
  } finally {
    isSubmitting.value = false;
  }
}
</script>

<template>
  <section class="home-page">
    <!-- 目的地与日期 -->
    <div class="ios-card">
      <div class="ios-card__header">
        <span class="ios-card__icon">📍</span>
        <span class="ios-card__title">目的地与日期</span>
      </div>

      <div class="ios-form-row">
        <div class="ios-field ios-field--full">
          <label class="ios-label">目的地城市</label>
          <input
            ref="destinationInput"
            v-model="formState.destination"
            class="ios-input"
            placeholder="请输入具体城市，如“西宁”"
          />
        </div>
      </div>

      <div class="ios-form-row ios-form-row--3col">
        <div class="ios-field">
          <label class="ios-label">开始日期</label>
          <input v-model="formState.startDate" class="ios-input" />
        </div>
        <div class="ios-field">
          <label class="ios-label">结束日期</label>
          <input v-model="formState.endDate" class="ios-input" />
        </div>
        <div class="ios-field">
          <label class="ios-label">人数</label>
          <input v-model.number="formState.travelers" type="number" class="ios-input" min="1" />
        </div>
      </div>

      <div class="ios-info-row">
        <span class="ios-info-label">旅行天数</span>
        <span class="ios-badge">{{ dayCount }} 天</span>
      </div>
    </div>

    <!-- 偏好设置 -->
    <div class="ios-card">
      <div class="ios-card__header">
        <span class="ios-card__icon">⚙️</span>
        <span class="ios-card__title">偏好设置</span>
      </div>

      <div class="ios-form-row ios-form-row--3col">
        <div class="ios-field">
          <label class="ios-label">节奏偏好</label>
          <select v-model="formState.pace" class="ios-select">
            <option value="轻松">轻松</option>
            <option value="适中">适中</option>
            <option value="紧凑">紧凑</option>
          </select>
        </div>
        <div class="ios-field">
          <label class="ios-label">住宿偏好</label>
          <select v-model="formState.hotelLevel" class="ios-select">
            <option value="舒适型">舒适型</option>
            <option value="高档型">高档型</option>
            <option value="经济型">经济型</option>
          </select>
        </div>
        <div class="ios-field">
          <label class="ios-label">总预算（全体出行人，元）</label>
          <input v-model.number="formState.budget" type="number" class="ios-input" min="0" />
        </div>
      </div>

      <div class="ios-field" style="margin-top: 16px">
        <label class="ios-label">旅行偏好</label>
        <div class="ios-chips">
          <button
            v-for="opt in preferenceOptions"
            :key="opt"
            :class="['ios-chip', { 'ios-chip--active': formState.preferences.includes(opt) }]"
            @click="togglePreference(formState.preferences, opt)"
          >
            {{ opt }}
          </button>
        </div>
      </div>

      <div class="ios-field" style="margin-top: 16px">
        <label class="ios-label">饮食偏好</label>
        <div class="ios-chips">
          <button
            v-for="opt in dietaryOptions"
            :key="opt"
            :class="['ios-chip', { 'ios-chip--active': formState.dietaryPreferences.includes(opt) }]"
            @click="togglePreference(formState.dietaryPreferences, opt)"
          >
            {{ opt }}
          </button>
        </div>
      </div>
    </div>

    <!-- 额外要求 -->
    <div class="ios-card">
      <div class="ios-card__header">
        <span class="ios-card__icon">💬</span>
        <span class="ios-card__title">额外要求</span>
      </div>
      <textarea
        v-model="formState.notes"
        class="ios-textarea"
        rows="3"
        placeholder="输入想要保留的偏好、节奏和备注"
      />
    </div>

    <div v-if="generationError" class="generation-error" role="alert" aria-live="polite">
      <strong class="generation-error__title">{{ generationError.message }}</strong>
      <p>{{ generationError.guidance }}</p>
      <ul v-if="generationError.candidateGaps.length" class="generation-error__gaps">
        <li v-for="gap in generationError.candidateGaps" :key="gap.category">
          {{ gap.category }}：已找到 {{ gap.count }} 个，至少需要 {{ gap.minimum }} 个，还缺 {{ gap.shortage }} 个
        </li>
      </ul>
      <div class="generation-error__actions">
        <button
          v-if="generationError.provinceSuggestion"
          class="ios-button ios-button--secondary"
          :disabled="isSubmitting"
          @click="formState.destination = generationError?.provinceSuggestion ?? formState.destination"
        >
          使用示例城市：{{ generationError.provinceSuggestion }}
        </button>
        <button class="ios-button ios-button--secondary" :disabled="isSubmitting" @click="focusDestination">
          修改目的地
        </button>
        <button class="ios-button ios-button--primary" :disabled="isSubmitting" @click="handleSubmit">
          按当前条件重试
        </button>
      </div>
    </div>

    <!-- 提交 -->
    <div class="submit-area">
      <button
        class="ios-button ios-button--primary"
        :disabled="isSubmitting"
        @click="handleSubmit"
      >
        {{ isSubmitting ? "正在生成中..." : "开始规划" }}
      </button>
      <p class="submit-hint">生成成功后会直接展示真实行程</p>
    </div>
  </section>
</template>

<style scoped>
.home-page {
  display: grid;
  gap: 12px;
}

/* iOS 卡片 */
.ios-card {
  padding: 20px;
  border-radius: 12px;
  background: #FFFFFF;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}

.ios-card__header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 0.5px solid rgba(0, 0, 0, 0.06);
}

.ios-card__icon {
  font-size: 17px;
}

.ios-card__title {
  font-size: 15px;
  font-weight: 600;
  color: #1C1C1E;
}

/* iOS 表单 */
.ios-form-row {
  display: grid;
  gap: 12px;
}

.ios-form-row--3col {
  grid-template-columns: repeat(3, 1fr);
}

.ios-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.ios-field--full {
  grid-column: 1 / -1;
}

.ios-label {
  font-size: 13px;
  font-weight: 500;
  color: #8E8E93;
}

.ios-input,
.ios-select {
  height: 36px;
  padding: 0 12px;
  border: 1px solid #D1D1D6;
  border-radius: 8px;
  background: #FFFFFF;
  font-size: 15px;
  color: #1C1C1E;
  outline: none;
  transition: border-color 0.2s ease;
}

.ios-input:focus,
.ios-select:focus {
  border-color: #007AFF;
}

.ios-textarea {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid #D1D1D6;
  border-radius: 8px;
  background: #FFFFFF;
  font-size: 15px;
  color: #1C1C1E;
  outline: none;
  resize: vertical;
  transition: border-color 0.2s ease;
  font-family: inherit;
}

.ios-textarea:focus {
  border-color: #007AFF;
}

/* iOS 信息行 */
.ios-info-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 0.5px solid rgba(0, 0, 0, 0.06);
}

.ios-info-label {
  font-size: 13px;
  color: #8E8E93;
}

.ios-badge {
  display: inline-flex;
  align-items: center;
  padding: 4px 12px;
  border-radius: 20px;
  background: #007AFF;
  color: #FFFFFF;
  font-size: 13px;
  font-weight: 600;
}

/* iOS Chip 标签 */
.ios-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.ios-chip {
  border: 1px solid #D1D1D6;
  border-radius: 20px;
  padding: 6px 14px;
  background: #FFFFFF;
  font-size: 13px;
  color: #1C1C1E;
  cursor: pointer;
  transition: all 0.2s ease;
}

.ios-chip:active {
  transform: scale(0.97);
}

.ios-chip--active {
  border-color: #007AFF;
  background: #007AFF;
  color: #FFFFFF;
}

/* iOS 按钮 */
.ios-button {
  border: none;
  border-radius: 12px;
  padding: 14px 32px;
  font-size: 17px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s ease;
}

.ios-button:active {
  transform: scale(0.97);
}

.ios-button--primary {
  background: #007AFF;
  color: #FFFFFF;
}

.ios-button--primary:disabled {
  opacity: 0.5;
  cursor: wait;
}

.ios-button--secondary {
  border: 1px solid #D1D1D6;
  background: #FFFFFF;
  color: #1C1C1E;
}

.ios-button--secondary:disabled {
  opacity: 0.5;
  cursor: wait;
}

.generation-error {
  padding: 16px 20px;
  border: 1px solid #F2B8B5;
  border-radius: 12px;
  background: #FFF4F2;
  color: #5C2522;
}

.generation-error__title {
  display: block;
  font-size: 15px;
}

.generation-error p {
  margin: 8px 0;
  line-height: 1.6;
}

.generation-error__gaps {
  margin: 8px 0;
  padding-left: 20px;
  line-height: 1.7;
}

.generation-error__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

/* 提交区 */
.submit-area {
  text-align: center;
  padding: 8px 0;
}

.submit-hint {
  margin-top: 10px;
  font-size: 13px;
  color: #8E8E93;
}

@media (max-width: 768px) {
  .ios-form-row--3col {
    grid-template-columns: 1fr;
  }
}
</style>
