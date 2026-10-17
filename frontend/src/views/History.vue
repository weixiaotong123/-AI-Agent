<script setup lang="ts">
import { message } from "ant-design-vue";
import { onMounted, ref, watch } from "vue";

import { deleteTrip, getTripDetail, listTrips } from "../services/api";
import type { Itinerary, TripSummaryItem } from "../types";

const props = defineProps<{
  active: boolean;
}>();

const emit = defineEmits<{
  openTrip: [itinerary: Itinerary];
}>();

const loading = ref(false);
const items = ref<TripSummaryItem[]>([]);
const deletingTripId = ref("");

async function loadTrips() {
  loading.value = true;
  try {
    const response = await listTrips();
    items.value = response.items;
  } catch (error) {
    console.error(error);
    message.error("历史列表加载失败。");
  } finally {
    loading.value = false;
  }
}

async function openTrip(tripId: string) {
  try {
    const response = await getTripDetail(tripId);
    emit("openTrip", response.itinerary);
    message.success("已加载已保存行程。");
  } catch (error) {
    console.error(error);
    message.error("读取行程详情失败。");
  }
}

async function removeTrip(tripId: string) {
  const confirmed = window.confirm("确定要删除这条已保存行程吗？删除后无法恢复。");
  if (!confirmed) return;

  deletingTripId.value = tripId;
  try {
    await deleteTrip(tripId);
    items.value = items.value.filter((item) => item.trip_id !== tripId);
    message.success("行程已删除。");
  } catch (error) {
    console.error(error);
    message.error("删除行程失败。");
  } finally {
    deletingTripId.value = "";
  }
}

onMounted(() => {
  if (props.active) void loadTrips();
});

watch(() => props.active, (active) => {
  if (active) void loadTrips();
});
</script>

<template>
  <section class="history-page">
    <!-- 头部 -->
    <div class="ios-card history-header">
      <div>
        <h2 class="history-header__title">历史行程</h2>
        <p class="history-header__desc">已保存到数据库的行程记录</p>
      </div>
      <button class="ios-btn ios-btn--primary ios-btn--sm" @click="loadTrips">刷新</button>
    </div>

    <!-- 状态 -->
    <div v-if="loading" class="ios-card ios-empty">正在加载...</div>
    <div v-else-if="items.length === 0" class="ios-card ios-empty">还没有已保存的行程</div>

    <!-- 列表 -->
    <div v-else class="history-grid">
      <div v-for="item in items" :key="item.trip_id" class="ios-card history-card">
        <div class="history-card__dest">{{ item.destination }}</div>
        <div class="history-card__id">{{ item.trip_id }}</div>
        <p class="history-card__summary">{{ item.summary }}</p>
        <div class="history-card__time">{{ item.updated_at || "未记录" }}</div>
        <div class="history-card__actions">
          <button class="ios-btn ios-btn--primary ios-btn--sm" @click="openTrip(item.trip_id)">查看详情</button>
          <button class="ios-btn ios-btn--danger ios-btn--sm" :disabled="deletingTripId === item.trip_id" @click="removeTrip(item.trip_id)">
            {{ deletingTripId === item.trip_id ? "删除中..." : "删除" }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.history-page {
  display: grid;
  gap: 12px;
}

/* 头部 */
.history-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.history-header__title {
  margin: 0 0 4px;
  font-size: 22px;
  font-weight: 700;
  color: #1C1C1E;
}

.history-header__desc {
  margin: 0;
  font-size: 14px;
  color: #8E8E93;
}

/* 卡片 */
.ios-card {
  padding: 20px;
  border-radius: 12px;
  background: #FFFFFF;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}

.ios-empty {
  text-align: center;
  color: #8E8E93;
  font-size: 14px;
  padding: 40px 20px;
}

/* 按钮 */
.ios-btn {
  border: none;
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;
}

.ios-btn:active { transform: scale(0.97); }
.ios-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.ios-btn--primary { background: #007AFF; color: #FFFFFF; }
.ios-btn--danger { background: #FF3B30; color: #FFFFFF; }
.ios-btn--sm { padding: 6px 14px; font-size: 13px; }

/* 列表 */
.history-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
}

.history-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.history-card__dest {
  font-size: 22px;
  font-weight: 700;
  color: #1C1C1E;
}

.history-card__id {
  font-size: 12px;
  color: #8E8E93;
  word-break: break-all;
}

.history-card__summary {
  margin: 0;
  font-size: 14px;
  color: #3C3C43;
  line-height: 1.6;
}

.history-card__time {
  font-size: 12px;
  color: #8E8E93;
}

.history-card__actions {
  display: flex;
  gap: 8px;
  margin-top: auto;
  padding-top: 12px;
  border-top: 0.5px solid rgba(0, 0, 0, 0.06);
}

@media (max-width: 768px) {
  .history-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }
}
</style>
