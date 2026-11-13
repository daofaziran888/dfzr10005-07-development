<template>
  <section class="page" data-module="tier">
    <header class="page-head">
      <div>
        <h2>数据分层规划管理</h2>
        <p class="page-desc">维护数据分层的粒度定义、存储选型与访问模式规划，为数据分层规划提供可审计的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记分层定义</button>
        <button class="btn" type="button" @click="exportRows">导出数据分层规划清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <section class="suggestion-panel">
      <header class="summary-head">
        <h3 class="block-title">画像驱动的分层调整建议（显示建议来源批次）</h3>
        <RouterLink class="link" to="/partition-profile">前往分区热度画像</RouterLink>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>建议编号</th>
            <th>表/分区</th>
            <th>画像分类</th>
            <th>建议动作</th>
            <th>目标分层</th>
            <th>建议来源批次</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="suggestion in profileSuggestions" :key="String(suggestion.id)">
            <td>{{ suggestion.建议编号 }}</td>
            <td>{{ suggestion.表名称 }} / {{ suggestion.分区名称 }}</td>
            <td>{{ suggestion.画像分类 }}</td>
            <td>{{ suggestion.建议动作 }}</td>
            <td>{{ suggestion.建议目标分层 }}</td>
            <td><span class="batch-pill">{{ suggestion.来源批次编号 }}</span></td>
            <td>{{ suggestion.状态 }}</td>
            <td class="row-actions">
              <button
                v-if="suggestion.状态 === '待处理'"
                class="link"
                type="button"
                @click="submitSuggestion(suggestion)"
              >
                采纳并生成分层定义
              </button>
              <span v-else class="cell-note">已流转</span>
            </td>
          </tr>
          <tr v-if="!profileSuggestions.length">
            <td :colspan="8" class="empty-state">暂无画像驱动的分层调整建议</td>
          </tr>
        </tbody>
      </table>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>建议来源批次</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span v-if="row['建议来源批次']" class="batch-pill">{{ row['建议来源批次'] }}</span>
            <span v-else class="cell-note">人工定义</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无数据分层规划数据，可先登记分层定义</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据分层规划记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/tier'
const columns = ["分层编号", "分层名称", "层级别名", "数据粒度", "存储格式", "压缩策略", "访问模式", "分层状态"]
const actions = ["定义分层", "废弃分层", "归档分层"]
const statuses = ["草稿", "已定义", "生效中", "已废弃"]
const stats = [{"label": "分层定义总数", "value": 0}, {"label": "已定义分层", "value": 0}, {"label": "生效中分层", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const profileSuggestions = ref<Row[]>([])

async function loadSuggestions() {
  try {
    const response = await request('/api/tier/profile-suggestions')
    if (!response.ok) {
      throw new Error('画像分层建议读取失败')
    }
    const payload = await response.json()
    profileSuggestions.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '画像分层建议读取失败'
  }
}

async function submitSuggestion(suggestion: Row) {
  errorMessage.value = ''
  try {
    const response = await request(
      `/api/partition-profile/suggestions/${suggestion.id}/submit`,
      { method: 'POST' },
    )
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message)
    }
    await Promise.all([reload(), loadSuggestions()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '建议提交失败'
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '分层定义登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据分层规划动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据分层规划操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('分层定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据分层规划列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadSuggestions()
})
</script>

<style scoped>
.suggestion-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 14px; }
.summary-head { display: flex; justify-content: space-between; align-items: center; }
.block-title { margin: 0 0 8px; font-size: 14px; }
.batch-pill { display: inline-block; background: #eef2ff; color: #3730a3; border-radius: 6px; padding: 1px 7px; font-size: 12px; }
.cell-note { color: var(--muted); font-size: 12px; }
</style>
