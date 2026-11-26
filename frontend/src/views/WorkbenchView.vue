<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <RouterLink
          v-if="backToExtractLink"
          class="btn primary"
          data-testid="back-to-extract"
          :to="backToExtractLink"
        >
          返回原作业继续（{{ route.query.batch || route.query.continueJobId }}）
        </RouterLink>
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <CleanseBatchBoard
      v-if="meta.key === 'std_cleanse' || meta.key === 'cleanse_batch'"
      :refresh-tick="batchTick"
      @view-record="openRecord"
      @changed="batchTick += 1"
    />

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span>{{ row.status }}</span>
            <small v-if="row.清洗状态 && row.清洗状态 !== row.status" class="cell-substatus" data-testid="row-cleanse-status">
              清洗：{{ row.清洗状态 }}
            </small>
            <p v-if="row.workflow?.暂停原因" class="row-pause-reason" :data-testid="`row-pause-${row.id}`">
              {{ row.workflow.暂停原因 }}
            </p>
            <RouterLink
              v-if="meta.key === 'record_extract' && row.实体编码"
              class="link row-rules-link"
              :data-testid="`row-rules-${row.id}`"
              :to="rulesLinkForRow(row)"
            >
              查看{{ row.实体编码 }}的清洗规则
            </RouterLink>
          </td>
          <td class="row-actions">
            <template v-if="meta.key === 'record_extract'">
              <button
                class="link strong-link"
                type="button"
                :data-testid="`action-${row.id}-派发清洗`"
                @click="quickDispatch(row)"
              >
                <span :data-testid="`dispatch-${row.id}`">派发清洗</span>
              </button>
              <button
                class="link strong-link"
                type="button"
                :data-testid="`action-${row.id}-继续办理`"
                @click="openWorkflow(row)"
              >
                <span :data-testid="`continue-${row.id}`">继续办理</span>
              </button>
              <button
                v-if="row.清洗记录号"
                class="link strong-link"
                type="button"
                :data-testid="`record-${row.id}`"
                @click="openRowRecord(row)"
              >
                清洗记录
              </button>
            </template>
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <label v-if="meta.key === 'record_extract'" class="create-field">
          <span>批次号（可选；不填则使用作业名称）</span>
          <input v-model="createValues['批次号']" data-testid="create-批次号" placeholder="例如 R1" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <ExtractWorkflowModal
      v-if="workflowJob"
      :job="workflowJob"
      :open="workflowOpen"
      :auto-dispatch="workflowAutoDispatch"
      @close="workflowOpen = false"
      @changed="reload"
      @view-record="openRecord"
    />

    <CleanseRecordModal
      :open="recordOpen"
      :record="activeRecord"
      :record-no="activeRecordNo"
      @close="recordOpen = false"
    />

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import CleanseBatchBoard from '@/features/CleanseBatchBoard.vue'
import CleanseRecordModal from '@/features/CleanseRecordModal.vue'
import ExtractWorkflowModal from '@/features/ExtractWorkflowModal.vue'
import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import { fetchJson } from '@/api/client'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const route = useRoute()
const meta = computed(() => moduleMeta(props.moduleKey))
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>(initialFilters())
const filterFields = computed(() => meta.value.fields.slice(0, 3))
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const batchTick = ref(0)

const workflowJob = ref<EntryRow | null>(null)
const workflowOpen = ref(false)
const workflowAutoDispatch = ref(false)
const recordOpen = ref(false)
const activeRecord = ref<Record<string, any> | null>(null)
const activeRecordNo = ref<string | number | null>(null)

const stats = computed(() => [
  { label: meta.value.metrics[0], value: total.value },
  { label: meta.value.metrics[1], value: rows.value.filter((row) => row.status === meta.value.statuses[1]).length },
  {
    label: meta.value.metrics[2],
    value: rows.value.filter((row) => row.status === meta.value.statuses[meta.value.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.value.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

const backToExtractLink = computed(() => {
  if (meta.value.key !== 'std_cleanse' || route.query.from !== 'record_extract') return null
  const continueId = route.query.continueJobId ?? route.query.continue
  if (!continueId && !route.query.batch) return null
  const query: Record<string, string> = {}
  if (continueId) query.continueJobId = String(continueId)
  if (route.query.batch) query.batch = String(route.query.batch)
  return { path: '/record_extract', query }
})

function rulesLinkForRow(row: EntryRow) {
  const batch = String(row.清洗批次号 ?? row.workflow?.批次号 ?? row.批次号 ?? row.作业名称 ?? `R${row.id}`)
  return {
    path: '/std_cleanse',
    query: {
      entity: String(row.实体编码 ?? ''),
      from: 'record_extract',
      continueJobId: String(row.id),
      batch,
    },
  }
}

function initialFilters(): Record<string, string> {
  const result: Record<string, string> = {}
  const currentMeta = moduleMeta(props.moduleKey)
  for (const field of currentMeta.fields.slice(0, 3)) {
    const value = route.query[field]
    if (typeof value === 'string' && value) result[field] = value
  }
  const entity = route.query['实体编码'] ?? route.query.entity ?? route.query.entity_code
  if (typeof entity === 'string' && entity && currentMeta.fields.includes('实体编码')) {
    result['实体编码'] = entity
  }
  return result
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.value.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.value.fields.map((field) => [field, '']))
  createValues.value['批次号'] = ''
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const values = { ...createValues.value }
    // 空的扩展批次号不要覆盖“默认使用作业名称”的服务端逻辑。
    if (!values['批次号']) delete values['批次号']
    const result = await createEntry(meta.value.key, values)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

function openWorkflow(row: EntryRow, autoDispatch = false) {
  workflowJob.value = row
  workflowAutoDispatch.value = autoDispatch
  workflowOpen.value = true
}

function quickDispatch(row: EntryRow) {
  openWorkflow(row, true)
}

function openRecord(payload: { record?: Record<string, any> | null; recordNo?: string | number | null }) {
  activeRecord.value = payload.record ?? null
  activeRecordNo.value = payload.recordNo ?? null
  recordOpen.value = true
}

function openRowRecord(row: EntryRow) {
  openRecord({ recordNo: row.清洗记录号 })
}

async function maybeOpenContinuedJob() {
  if (meta.value.key !== 'record_extract' || workflowJob.value) return
  const continueRef = route.query.continueJobId ?? route.query.continue ?? route.query.batch
  if (!continueRef) return
  const refText = String(continueRef)

  if (/^\d+$/.test(refText)) {
    const id = Number(refText)
    const row = rows.value.find((item) => Number(item.id) === id)
    if (row) {
      openWorkflow(row)
      return
    }
    try {
      const entry = await fetchJson<EntryRow>(`/api/record_extract/${id}`)
      openWorkflow(entry)
      return
    } catch {
      // 继续尝试按批次号找回。
    }
  }

  try {
    const result = await fetchJson<{ ok: boolean; job?: EntryRow }>(
      `/api/record_extract/workflow?batch_no=${encodeURIComponent(refText)}`,
    )
    if (result.ok && result.job) openWorkflow(result.job)
  } catch {
    // 列表为空或作业不可读时保留在作业页，由用户手动点击“继续办理”。
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.value.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
    if (meta.value.key === 'std_cleanse') batchTick.value += 1
    if (meta.value.key === 'record_extract' && action === '重试抽取' && result.data?.batch) {
      const freshRow = rows.value.find((item) => Number(item.id) === Number(row.id)) ?? row
      openWorkflow(freshRow)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.value.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    await maybeOpenContinuedJob()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

watch(
  () => props.moduleKey,
  () => {
    filters.value = initialFilters()
    rows.value = []
    workflowJob.value = null
    workflowOpen.value = false
    void reload()
  },
)

onMounted(reload)
</script>
