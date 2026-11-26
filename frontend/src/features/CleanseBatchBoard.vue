<template>
  <section class="cleanse-board" data-testid="cleanse-batch-board">
    <div class="board-head">
      <div>
        <h3>待清洗批次 / 领用恢复台</h3>
        <p class="page-desc">标准化清洗按来源关联等待批次与实际抽取行；缺规则会说明暂停原因，失败批次可用原批次号找回。</p>
      </div>
      <button class="btn" type="button" data-testid="refresh-batches" @click="load">刷新批次</button>
    </div>

    <form class="workflow-inline recover-by-no" @submit.prevent="recoverByNo">
      <label class="grow">
        按批次号找回/恢复
        <input v-model="recoverNo" data-testid="recover-batch-no" placeholder="输入原批次号，例如 R1" />
      </label>
      <button class="btn primary" type="submit" :disabled="recovering" data-testid="recover-by-no">找回并恢复</button>
    </form>

    <div class="filter-bar compact">
      <label class="filter-item">
        <span>来源</span>
        <input v-model="sourceFilter" data-testid="batch-filter-source" placeholder="按来源检索" />
      </label>
      <label class="filter-item">
        <span>实体编码</span>
        <input v-model="entityFilter" data-testid="batch-filter-entity" placeholder="按实体检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter" data-testid="batch-filter-status">
          <option value="">全部状态</option>
          <option>等待清洗</option>
          <option>清洗中</option>
          <option>已完成</option>
          <option>清洗失败</option>
        </select>
      </label>
    </div>

    <div v-if="loading" class="workflow-muted">正在读取等待清洗批次…</div>
    <p v-if="message" :class="hasError ? 'error-text' : 'workflow-success'" data-testid="batch-action-message">{{ message }}</p>

    <div v-if="!loading && batches.length === 0" class="empty-state board-empty" data-testid="cleanse-batch-empty">
      <strong>暂无待清洗/等待清洗批次</strong>
      <p>从「记录抽取作业」完成数据并点击派发后，已抽取数据会登记到这里；如果对应实体没有已启用规则，会保留批次并说明暂停原因。</p>
    </div>

    <div v-else-if="filteredBatches.length === 0" class="empty-state" data-testid="cleanse-batch-filter-empty">
      没有符合当前筛选条件的批次；清空筛选可找回全部等待、失败和已完成批次。
    </div>

    <table v-else class="data-table batch-table" data-testid="cleanse-batch-table">
      <thead>
        <tr>
          <th>批次号</th>
          <th>来源</th>
          <th>实体</th>
          <th>行数</th>
          <th>状态/暂停原因</th>
          <th>确定规则</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="batch in filteredBatches"
          :key="String(batch.id)"
          :data-testid="`cleanse-batch-${batch.批次号}`"
          :data-status="String(batch.status)"
        >
          <td><strong>{{ batch.批次号 }}</strong></td>
          <td>{{ batch.源系统 }}</td>
          <td>{{ batch.实体编码 }}</td>
          <td data-testid="batch-row-count">{{ batch.行数 ?? batch.row_count ?? 0 }}</td>
          <td>
            <span class="status-pill" :data-status="String(batch.status)">{{ batch.status }}</span>
            <p v-if="batch.暂停原因" class="pause-reason" data-testid="batch-pause-reason">{{ batch.暂停原因 }}</p>
            <p v-if="batch.失败原因" class="error-text">{{ batch.失败原因 }}</p>
          </td>
          <td>{{ batch.规则名称 || '—' }}</td>
          <td class="row-actions">
            <button
              v-if="batch.status === '等待清洗' && batch.claimable"
              class="link"
              type="button"
              :data-testid="`claim-${batch.批次号}`"
              @click="execute(batch)"
            >
              领用并清洗
            </button>
            <button
              v-if="batch.status === '清洗失败' || batch.recoverable"
              class="link"
              type="button"
              :data-testid="`recover-${batch.批次号}`"
              @click="recover(batch)"
            >
              恢复
            </button>
            <button
              v-if="batch.清洗记录号"
              class="link"
              type="button"
              :data-testid="`record-${batch.批次号}`"
              @click="viewRecord(batch)"
            >
              查看清洗记录
            </button>
            <RouterLink
              v-if="batch.status === '等待清洗' && !batch.claimable"
              class="link"
              :to="rulesLink(batch)"
              :data-testid="`rules-${batch.批次号}`"
            >
              查看规则
            </RouterLink>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="records.length" class="record-list" data-testid="cleanse-record-list">
      <h4>最近清洗记录</h4>
      <ul>
        <li v-for="record in records.slice(0, 8)" :key="String(record.id)">
          <button class="link" type="button" :data-testid="`open-record-${record.清洗记录号}`" @click="$emit('viewRecord', { record })">
            {{ record.清洗记录号 }} · {{ record.批次号 }} · {{ record.status }} · {{ record.行数 }} 行
          </button>
        </li>
      </ul>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { cleanseWorkflowApi, type WorkflowDict } from '@/api/workflow'

const props = defineProps<{ refreshTick?: number }>()
const emit = defineEmits<{
  changed: []
  viewRecord: [payload: { record?: WorkflowDict | null; recordNo?: string | number | null }]
}>()

const route = useRoute()
const batches = ref<WorkflowDict[]>([])
const records = ref<WorkflowDict[]>([])
const loading = ref(false)
const message = ref('')
const hasError = ref(false)
const sourceFilter = ref('')
const entityFilter = ref(String(route.query.entity ?? route.query.entity_code ?? route.query['实体编码'] ?? ''))
const statusFilter = ref('')
const recoverNo = ref('')
const recovering = ref(false)

const filteredBatches = computed(() => batches.value.filter((batch) => {
  if (sourceFilter.value && !String(batch.源系统 ?? '').includes(sourceFilter.value)) return false
  if (entityFilter.value && !String(batch.实体编码 ?? '').includes(entityFilter.value)) return false
  if (statusFilter.value && batch.status !== statusFilter.value) return false
  return true
}))

function rulesLink(batch: WorkflowDict) {
  return {
    path: '/std_cleanse',
    query: {
      entity: String(batch.实体编码 ?? ''),
      batch: String(batch.批次号 ?? ''),
    },
  }
}

async function load() {
  loading.value = true
  try {
    const [batchResult, recordResult] = await Promise.all([
      cleanseWorkflowApi.batches({ size: '200' }),
      cleanseWorkflowApi.records(),
    ])
    batches.value = batchResult.items ?? []
    records.value = recordResult.items ?? []
  } finally {
    loading.value = false
  }
}

async function execute(batch: WorkflowDict) {
  message.value = ''
  const scope = { batch_no: batch.批次号, source: batch.源系统, entity_code: batch.实体编码 }
  const claim = await cleanseWorkflowApi.claim(scope)
  if (!claim.ok) {
    message.value = claim.message ?? '领用失败'
    hasError.value = true
    await load()
    return
  }
  if (claim.code !== 'ALREADY_COMPLETED') {
    const rows = Array.isArray(claim.rows) ? claim.rows : []
    const completed = await cleanseWorkflowApi.complete({
      ...scope,
      rule_id: claim.rule?.id ?? batch.规则ID,
      row_count: rows.length || batch.行数 || 0,
      cleaned_count: rows.length || batch.行数 || 0,
    })
    message.value = completed.message ?? ''
    hasError.value = completed.ok === false
  } else {
    message.value = claim.message ?? ''
  }
  await load()
}

async function recover(batch: WorkflowDict) {
  message.value = ''
  const result = await cleanseWorkflowApi.recover({ batch_no: batch.批次号 })
  message.value = result.message ?? ''
  hasError.value = result.ok === false
  await load()
}

async function recoverByNo() {
  if (!recoverNo.value.trim()) return
  recovering.value = true
  message.value = ''
  try {
    const result = await cleanseWorkflowApi.recover({ batch_no: recoverNo.value.trim() })
    message.value = result.message ?? ''
    hasError.value = result.ok === false
    if (result.ok) recoverNo.value = ''
    await load()
  } finally {
    recovering.value = false
  }
}

function viewRecord(batch: WorkflowDict) {
  emit('viewRecord', { recordNo: batch.清洗记录号 })
}

watch(() => props.refreshTick, () => void load())
watch(
  () => route.query.entity,
  (value) => {
    entityFilter.value = String(value ?? '')
  },
)

void load()
defineExpose({ load })
</script>
