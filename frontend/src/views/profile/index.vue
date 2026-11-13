<template>
  <section class="page" data-module="partition-profile">
    <header class="page-head">
      <div>
        <h2>分区热度画像</h2>
        <p class="page-desc">
          依据采样时间、访问来源与分区热度生成按时间分组的画像，标注持续高访问、短时峰值、长期无访问分区，
          并把可执行的分层调整建议提交到数据分层定义；历史批次按原口径保留，当前画像与目标分层全程可追溯。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openReport">上报分区采样</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in summaryCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>画像分类</span>
        <select v-model="categoryFilter">
          <option value="">全部分类</option>
          <option v-for="name in categories" :key="name" :value="name">{{ name }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>表/分区检索</span>
        <input v-model="keyword" placeholder="按表名称或分区名称检索" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <h3 class="block-title">当前分区画像（持续高访问与短时峰值分区优先展示）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in profileColumns" :key="column">{{ column }}</th>
          <th>画像分类</th>
          <th>来源批次</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in profiles" :key="`${row.表名称}/${row.分区名称}`">
          <td>{{ row.表名称 }}</td>
          <td>{{ row.分区名称 }}</td>
          <td>{{ row.总查询次数 }}</td>
          <td>{{ row.分区热度 }}</td>
          <td>{{ formatSources(row.来源明细) }}</td>
          <td>
            <span class="tag" :class="tagClass(row.画像分类)">{{ row.画像分类 }}</span>
            <p class="cell-note">{{ row.分类依据 }}</p>
          </td>
          <td>
            <span class="batch-pill">{{ row.批次编号 }}</span>
            <p class="cell-note">{{ row.规则版本 }} · {{ row.收口时间 }}</p>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">分区明细</button>
          </td>
        </tr>
        <tr v-if="!profiles.length">
          <td :colspan="profileColumns.length + 3" class="empty-state">暂无符合条件的分区画像</td>
        </tr>
      </tbody>
    </table>

    <h3 class="block-title">按采样窗口分组的画像视图</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>批次编号</th>
          <th>采样窗口</th>
          <th>规则版本</th>
          <th>批次状态</th>
          <th>收口时间</th>
          <th>分区数</th>
          <th>持续高访问</th>
          <th>短时峰值</th>
          <th>长期无访问</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="batch in timeline" :key="batch.批次编号">
          <td><span class="batch-pill">{{ batch.批次编号 }}</span></td>
          <td>{{ batch.采样窗口开始 }} ~ {{ batch.采样窗口结束 }}</td>
          <td>{{ batch.规则版本 }}</td>
          <td>
            <span class="tag" :class="batch.状态 === '已收口' ? 'tag-done' : 'tag-open'">{{ batch.状态 }}</span>
          </td>
          <td>{{ batch.收口时间 || '—' }}</td>
          <td>{{ batch.分区数 }}</td>
          <td><strong>{{ batch.分类统计['持续高访问'] }}</strong></td>
          <td><strong>{{ batch.分类统计['短时峰值'] }}</strong></td>
          <td><strong>{{ batch.分类统计['长期无访问'] }}</strong></td>
        </tr>
      </tbody>
    </table>

    <h3 class="block-title">可执行的分层调整建议（提交后进入数据分层定义）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>建议编号</th>
          <th>表/分区</th>
          <th>画像分类</th>
          <th>建议动作</th>
          <th>目标分层</th>
          <th>来源批次</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="suggestion in suggestions" :key="String(suggestion.id)">
          <td>{{ suggestion.建议编号 }}</td>
          <td>{{ suggestion.表名称 }} / {{ suggestion.分区名称 }}</td>
          <td><span class="tag" :class="tagClass(suggestion.画像分类)">{{ suggestion.画像分类 }}</span></td>
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
              提交分层定义
            </button>
            <span v-else class="cell-note">已流转</span>
          </td>
        </tr>
        <tr v-if="!suggestions.length">
          <td :colspan="8" class="empty-state">暂无可展示的分层调整建议</td>
        </tr>
      </tbody>
    </table>

    <div v-if="detail" class="modal-mask" @click.self="detail = null">
      <div class="modal">
        <header class="modal-head">
          <h3>分区明细 · {{ detail.当前画像.表名称 }} / {{ detail.当前画像.分区名称 }}</h3>
          <button class="btn ghost" type="button" @click="detail = null">关闭</button>
        </header>
        <p class="cell-note">
          当前画像来自批次 <strong>{{ detail.当前画像.批次编号 }}</strong>
          （{{ detail.当前画像.规则版本 }}，{{ detail.当前画像.收口时间 }}），
          冲突裁决：{{ detail.当前画像.冲突裁决 }}
        </p>
        <h4 class="block-title">历史批次快照（旧批次按原口径保留）</h4>
        <table class="data-table inner">
          <thead>
            <tr><th>批次编号</th><th>规则版本</th><th>分区热度</th><th>画像分类</th><th>收口时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="snap in detail.历史快照" :key="String(snap.id)">
              <td>{{ snap.批次编号 }}</td>
              <td>{{ snap.规则版本 }}</td>
              <td>{{ snap.分区热度 }}</td>
              <td><span class="tag" :class="tagClass(snap.画像分类)">{{ snap.画像分类 }}</span></td>
              <td>{{ snap.收口时间 }}</td>
            </tr>
          </tbody>
        </table>
        <h4 class="block-title">采样记录</h4>
        <table class="data-table inner">
          <thead>
            <tr><th>批次编号</th><th>采样任务</th><th>访问来源</th><th>查询次数</th><th>采样时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="obs in detail.采样记录" :key="String(obs.id)">
              <td>{{ obs.批次编号 }}</td>
              <td>{{ obs.采样任务 }}</td>
              <td>{{ obs.访问来源 }}</td>
              <td>{{ obs.查询次数 }}</td>
              <td>{{ obs.采样时间 }}</td>
            </tr>
          </tbody>
        </table>
        <h4 class="block-title">分层建议链路</h4>
        <table class="data-table inner">
          <thead>
            <tr><th>建议编号</th><th>来源批次</th><th>目标分层</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="sug in detail.分层建议" :key="String(sug.id)">
              <td>{{ sug.建议编号 }}</td>
              <td>{{ sug.来源批次编号 }}</td>
              <td>{{ sug.建议目标分层 }}</td>
              <td>{{ sug.状态 }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="reportForm.open" class="modal-mask" @click.self="reportForm.open = false">
      <div class="modal">
        <header class="modal-head">
          <h3>上报分区采样（并发/重复仅首个成功）</h3>
          <button class="btn ghost" type="button" @click="reportForm.open = false">关闭</button>
        </header>
        <div class="form-grid">
          <label class="filter-item">
            <span>采样批次id</span>
            <input v-model="reportForm.values.采样批次id" placeholder="采样中批次的 id" />
          </label>
          <label class="filter-item">
            <span>表名称</span>
            <input v-model="reportForm.values.表名称" placeholder="例如 dwd_order_detail" />
          </label>
          <label class="filter-item">
            <span>分区名称</span>
            <input v-model="reportForm.values.分区名称" placeholder="例如 dt=2026-09-15" />
          </label>
          <label class="filter-item">
            <span>访问来源</span>
            <input v-model="reportForm.values.访问来源" placeholder="在线查询 / 离线任务 / 对外服务" />
          </label>
          <label class="filter-item">
            <span>查询次数</span>
            <input v-model="reportForm.values.查询次数" placeholder="非负整数" />
          </label>
          <label class="filter-item">
            <span>采样任务</span>
            <input v-model="reportForm.values.采样任务" placeholder="任务标识，用于并发冲突提示" />
          </label>
        </div>
        <footer class="modal-foot">
          <button class="btn primary" type="button" @click="submitReport">提交采样上报</button>
        </footer>
      </div>
    </div>

    <footer class="page-foot">
      <span v-if="message" :class="messageOk ? '' : 'error-text'">{{ message }}</span>
      <span>采样窗口按自然月批次划分 · 峰值=热度≥100且达历史均值2倍 · 来源权重 在线1.0/离线0.6/对外1.2（v2）</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/partition-profile'
const categories = ['持续高访问', '短时峰值', '长期无访问', '平稳波动']
const profileColumns = ['表名称', '分区名称', '总查询次数', '分区热度', '来源明细']

type ProfileRow = Record<string, any>
type SuggestionRow = Record<string, any>
type TimelineBatch = Record<string, any>

const profiles = ref<ProfileRow[]>([])
const timeline = ref<TimelineBatch[]>([])
const suggestions = ref<SuggestionRow[]>([])
const summary = ref<Record<string, any>>({})
const detail = ref<Record<string, any> | null>(null)
const categoryFilter = ref('')
const keyword = ref('')
const message = ref('')
const messageOk = ref(true)

const reportForm = reactive<{ open: boolean; values: Record<string, string> }>({
  open: false,
  values: {
    采样批次id: '',
    表名称: '',
    分区名称: '',
    访问来源: '在线查询',
    查询次数: '',
    采样任务: '',
  },
})

const summaryCards = computed(() => {
  const stats = summary.value.分类统计 ?? {}
  return [
    { label: '画像分区总数', value: summary.value.画像分区总数 ?? 0 },
    { label: '持续高访问', value: stats['持续高访问'] ?? 0 },
    { label: '短时峰值', value: stats['短时峰值'] ?? 0 },
    { label: '长期无访问', value: stats['长期无访问'] ?? 0 },
    { label: '待处理分层建议', value: summary.value.待处理分层建议 ?? 0 },
    { label: '已收口/采样中批次', value: `${summary.value.已收口批次数 ?? 0} / ${summary.value.采样中批次数 ?? 0}` },
  ]
})

function tagClass(category: string): string {
  if (category === '持续高访问') return 'tag-hot'
  if (category === '短时峰值') return 'tag-spike'
  if (category === '长期无访问') return 'tag-idle'
  return 'tag-normal'
}

function formatSources(sources: Record<string, number> | undefined): string {
  if (!sources) return '—'
  return Object.entries(sources).map(([name, count]) => `${name}×${count}`).join('，')
}

function resetFilters() {
  categoryFilter.value = ''
  keyword.value = ''
  void reload()
}

function openReport() {
  reportForm.open = true
  message.value = ''
}

async function submitReport() {
  message.value = ''
  try {
    const response = await request(`${ENDPOINT}/observations`, {
      method: 'POST',
      body: JSON.stringify({ values: reportForm.values }),
    })
    const payload = await response.json()
    messageOk.value = Boolean(payload.ok)
    message.value = payload.message
    if (payload.ok) {
      reportForm.open = false
      await reload()
    }
  } catch (error) {
    messageOk.value = false
    message.value = error instanceof Error ? error.message : '采样上报失败'
  }
}

async function openDetail(row: ProfileRow) {
  message.value = ''
  try {
    const response = await request(
      `${ENDPOINT}/partitions/${encodeURIComponent(row.表名称)}/${encodeURIComponent(row.分区名称)}`,
    )
    const payload = await response.json()
    detail.value = payload.ok ? payload.detail : null
    if (!payload.ok) message.value = payload.message
  } catch (error) {
    message.value = error instanceof Error ? error.message : '分区明细读取失败'
  }
}

async function submitSuggestion(suggestion: SuggestionRow) {
  message.value = ''
  try {
    const response = await request(`${ENDPOINT}/suggestions/${suggestion.id}/submit`, { method: 'POST' })
    const payload = await response.json()
    messageOk.value = Boolean(payload.ok)
    message.value = payload.message
    await reload()
  } catch (error) {
    messageOk.value = false
    message.value = error instanceof Error ? error.message : '建议提交失败'
  }
}

async function reload() {
  message.value = ''
  const query = new URLSearchParams()
  if (categoryFilter.value) query.set('category', categoryFilter.value)
  if (keyword.value) query.set('keyword', keyword.value)
  try {
    const [profileResp, timelineResp, suggestionResp, summaryResp] = await Promise.all([
      request(`${ENDPOINT}/profiles?${query.toString()}`),
      request(`${ENDPOINT}/timeline`),
      request(`${ENDPOINT}/suggestions`),
      request(`${ENDPOINT}/summary`),
    ])
    const [profilePayload, timelinePayload, suggestionPayload, summaryPayload] = await Promise.all([
      profileResp.json(),
      timelineResp.json(),
      suggestionResp.json(),
      summaryResp.json(),
    ])
    profiles.value = profilePayload.items ?? []
    timeline.value = timelinePayload.items ?? []
    suggestions.value = suggestionPayload.items ?? []
    summary.value = summaryPayload ?? {}
  } catch (error) {
    messageOk.value = false
    message.value = error instanceof Error ? error.message : '分区热度画像读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.block-title { margin: 18px 0 8px; font-size: 14px; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; }
.tag-hot { background: #fee2e2; color: #b42318; }
.tag-spike { background: #fef3c7; color: #b45309; }
.tag-idle { background: #e2e8f0; color: #475569; }
.tag-normal { background: #dbeafe; color: #1d4ed8; }
.tag-done { background: #dcfce7; color: #166534; }
.tag-open { background: #fef3c7; color: #b45309; }
.batch-pill { display: inline-block; background: #eef2ff; color: #3730a3; border-radius: 6px; padding: 1px 7px; font-size: 12px; }
.cell-note { margin: 4px 0 0; color: var(--muted); font-size: 12px; max-width: 260px; }
.modal-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: flex; align-items: center; justify-content: center; z-index: 20; }
.modal { background: #fff; border-radius: 10px; width: min(920px, 92vw); max-height: 86vh; overflow: auto; padding: 18px 20px; }
.modal-head { display: flex; justify-content: space-between; align-items: center; }
.data-table.inner { margin-bottom: 8px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 12px 0; }
.modal-foot { display: flex; justify-content: flex-end; }
</style>
