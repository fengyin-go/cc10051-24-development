<template>
  <div v-if="open" class="modal-backdrop" data-testid="extract-workflow-modal" @click.self="$emit('close')">
    <section class="modal workflow-modal" role="dialog" aria-modal="true" :data-batch-no="batchNo">
      <header class="workflow-head">
        <div>
          <h3>派发和续办 · <span data-testid="workflow-batch-no">{{ batchNo }}</span></h3>
          <p class="page-desc">服务端核对来源与实体后判断规则能否受理；重复派发和迟到结果都按原批次号幂等处理。</p>
        </div>
        <button class="btn ghost" type="button" data-testid="close-workflow" @click="$emit('close')">关闭</button>
      </header>

      <div class="workflow-summary" data-testid="workflow-summary">
        <span><b>作业：</b>{{ job.作业名称 }}</span>
        <span><b>来源：</b><span data-testid="workflow-source">{{ source }}</span></span>
        <span><b>实体：</b><span data-testid="workflow-entity">{{ entity }}</span></span>
        <span><b>抽取状态：</b>{{ job.status }}</span>
        <span v-if="batch"><b>清洗状态：</b><span data-testid="workflow-status">{{ batch.status }}</span></span>
      </div>

      <p v-if="loading" class="workflow-muted">正在读取服务端状态…</p>
      <p v-if="message" :class="hasError ? 'error-text' : 'workflow-success'" data-testid="workflow-message">{{ message }}</p>

      <div v-if="job.status !== '已完成'" class="workflow-card" data-testid="complete-extract-card">
        <h4>先完成抽取并登记实际行</h4>
        <div class="workflow-inline">
          <label>
            抽取行数
            <input v-model.number="completeCount" type="number" min="0" data-testid="complete-row-count" />
          </label>
          <button class="btn primary" type="button" :disabled="busy" data-testid="complete-extract" @click="completeExtraction">
            完成抽取并派发
          </button>
        </div>
      </div>

      <div v-if="batch" class="workflow-card" :data-status="batch.status">
        <div class="workflow-badge-row">
          <span class="status-pill" :data-status="String(batch.status)">{{ batch.status }}</span>
          <span>批次号保持为 <strong>{{ batch.批次号 }}</strong></span>
          <span v-if="batch.规则名称">确定规则：<strong data-testid="matched-rule-name">{{ batch.规则名称 }}</strong></span>
        </div>

        <p v-if="batch.暂停原因" class="pause-reason" data-testid="pause-reason">
          {{ batch.暂停原因 }}
        </p>
        <p v-if="batch.失败原因" class="error-text" data-testid="failure-reason-text">失败原因：{{ batch.失败原因 }}</p>

        <div v-if="record?.status === '已完成' || batch.status === '已完成'" class="workflow-confirmation" data-testid="workflow-complete-summary">
          <strong>已有一笔完整清洗确认：{{ record?.清洗记录号 ?? batch.清洗记录号 }}</strong>
          <span>规则 {{ record?.规则名称 ?? batch.规则名称 }}，{{ record?.行数 ?? batch.行数 }} 行；迟到或重复请求不会再生成第二笔。</span>
        </div>

        <div class="workflow-actions">
          <RouterLink
            class="btn"
            data-testid="view-entity-rules"
            :to="rulesLink"
          >
            查看{{ entity }}的清洗规则
          </RouterLink>
          <button class="btn" type="button" :disabled="busy" data-testid="refresh-workflow" @click="refresh()">
            刷新服务端状态
          </button>
          <button class="btn" type="button" :disabled="busy" data-testid="dispatch-batch" @click="dispatch">
            核对来源/实体并重新派发
          </button>

          <button
            v-if="batch.status === '等待清洗' && batch.claimable"
            class="btn primary"
            type="button"
            :disabled="busy"
            data-testid="claim-batch"
            @click="claimAndCleanse"
          >
            正式认领（领用）并清洗
          </button>
          <button
            v-else-if="batch.status === '清洗中'"
            class="btn primary"
            type="button"
            :disabled="busy"
            data-testid="claim-batch"
            @click="claimAndCleanse"
          >
            重试领用/查询处理结果
          </button>
          <button
            v-if="batch.recoverable || batch.status === '清洗失败'"
            class="btn primary"
            type="button"
            :disabled="busy"
            data-testid="recover-batch"
            @click="recover"
          >
            恢复报错前的批次
          </button>
          <button
            v-if="record || batch.清洗记录号"
            class="btn primary"
            type="button"
            data-testid="view-cleanse-record"
            @click="viewRecord"
          >
            打开清洗记录
          </button>
        </div>

        <div v-if="batch.status === '等待清洗' && !batch.claimable" class="workflow-empty-inline" data-testid="missing-rule-tip">
          当前实体还没有可受理的已启用规则。可直接打开规则页启用；返回后仍办理原批次 {{ batch.批次号 }}。
        </div>

        <details class="worker-details">
          <summary>Worker 领用/失败回报</summary>
          <div class="workflow-inline">
            <label class="grow">
              失败原因
              <input v-model="failureReason" data-testid="failure-reason-input" placeholder="例如：规则表达式执行失败" />
            </label>
            <button class="btn" type="button" :disabled="busy" data-testid="report-failure" @click="reportFailure">
              登记执行失败
            </button>
          </div>
          <p class="workflow-muted">领用请求会按来源关联批次与实际抽取行；完成或失败均登记规则、行数和可继续结果。</p>
        </details>
      </div>

      <div v-else class="workflow-card" data-testid="no-batch-yet">
        <p>尚未登记等待清洗批次。完成抽取后点击“派发清洗”，服务端会判断是否有确定可用规则。</p>
        <button class="btn primary" type="button" :disabled="busy || job.status !== '已完成'" data-testid="dispatch-first" @click="dispatch">
          派发清洗并登记批次
        </button>
      </div>

      <div v-if="detail?.rows?.length" class="workflow-rows">
        <h4>关联到的实际抽取行（{{ detail.rows.length }}）</h4>
        <ul>
          <li v-for="row in detail.rows.slice(0, 5)" :key="String(row.id)" data-testid="extract-row-link">
            {{ row.记录键 }} · {{ row.源系统 }} · {{ row.实体编码 }}
          </li>
        </ul>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'

import { cleanseWorkflowApi, extractWorkflowApi, type WorkflowDict } from '@/api/workflow'
import type { EntryRow } from '@/data/types'

const props = defineProps<{
  open: boolean
  job: EntryRow
  autoDispatch?: boolean
}>()

const emit = defineEmits<{
  close: []
  changed: []
  viewRecord: [payload: { record?: WorkflowDict | null; recordNo?: string | number | null }]
}>()

const detail = ref<WorkflowDict | null>(null)
const loading = ref(false)
const busy = ref(false)
const message = ref('')
const hasError = ref(false)
const completeCount = ref(3)
const failureReason = ref('规则表达式执行失败')
const autoDispatched = ref(false)

const batch = computed<WorkflowDict | null>(() => detail.value?.batch ?? (props.job.workflow as WorkflowDict | undefined) ?? null)
const record = computed<WorkflowDict | null>(() => detail.value?.record ?? null)
const source = computed(() => String(props.job.源系统 ?? ''))
const entity = computed(() => String(props.job.实体编码 ?? ''))
const batchNo = computed(() => String(
  batch.value?.批次号
  ?? props.job.清洗批次号
  ?? props.job.批次号
  ?? props.job.作业名称
  ?? `R${props.job.id}`,
))
const rulesLink = computed(() => ({
  path: '/std_cleanse',
  query: {
    entity: entity.value,
    from: 'record_extract',
    continueJobId: String(props.job.id),
    batch: batchNo.value,
  },
}))

function scopeBody(extra: WorkflowDict = {}): WorkflowDict {
  return {
    batch_no: batchNo.value,
    source: source.value,
    source_system: source.value,
    entity: entity.value,
    entity_code: entity.value,
    ...extra,
  }
}

function applyResult(result: WorkflowDict) {
  detail.value = result
  message.value = String(result.message ?? '')
  hasError.value = result.ok === false
  emit('changed')
}

let pollTimer: ReturnType<typeof setInterval> | undefined

async function refresh(silent = false) {
  if (!silent) loading.value = true
  if (!silent) message.value = ''
  try {
    const result = await extractWorkflowApi.detail(props.job.id)
    if (result.ok === false) throw new Error(result.message || '工作流状态读取失败')
    detail.value = result
  } catch (error) {
    hasError.value = true
    message.value = error instanceof Error ? error.message : '工作流状态读取失败'
  } finally {
    loading.value = false
  }
}

async function dispatch() {
  busy.value = true
  message.value = ''
  try {
    const result = await extractWorkflowApi.dispatch(props.job.id, scopeBody())
    applyResult(result)
    await refresh()
  } catch (error) {
    hasError.value = true
    message.value = error instanceof Error ? error.message : '派发失败'
  } finally {
    busy.value = false
  }
}

async function completeExtraction() {
  busy.value = true
  message.value = ''
  try {
    const result = await extractWorkflowApi.complete(props.job.id, scopeBody({
      record_count: completeCount.value,
    }))
    applyResult(result)
    await refresh()
  } catch (error) {
    hasError.value = true
    message.value = error instanceof Error ? error.message : '完成抽取失败'
  } finally {
    busy.value = false
  }
}

async function claimAndCleanse() {
  busy.value = true
  message.value = ''
  try {
    const claimResult = await cleanseWorkflowApi.claim(scopeBody())
    if (!claimResult.ok) {
      applyResult(claimResult)
      return
    }
    if (claimResult.code === 'ALREADY_COMPLETED') {
      applyResult(claimResult)
      await refresh()
      return
    }

    // 浏览器侧也显式分两步：先领用并关联实际抽取行，再回报执行完成。
    const rows = Array.isArray(claimResult.rows) ? claimResult.rows : []
    const ruleId = claimResult.rule?.id ?? claimResult.batch?.规则ID ?? batch.value?.规则ID
    const completeResult = await cleanseWorkflowApi.complete(scopeBody({
      rule_id: ruleId,
      row_count: rows.length || claimResult.batch?.行数 || 0,
      cleaned_count: rows.length || claimResult.batch?.行数 || 0,
    }))
    applyResult(completeResult)
    await refresh()
  } catch (error) {
    hasError.value = true
    message.value = error instanceof Error ? error.message : '领用清洗失败'
  } finally {
    busy.value = false
  }
}

async function reportFailure() {
  busy.value = true
  message.value = ''
  try {
    const result = await cleanseWorkflowApi.fail(scopeBody({ reason: failureReason.value }))
    applyResult(result)
    await refresh()
  } catch (error) {
    hasError.value = true
    message.value = error instanceof Error ? error.message : '失败回报失败'
  } finally {
    busy.value = false
  }
}

async function recover() {
  busy.value = true
  message.value = ''
  try {
    const result = await cleanseWorkflowApi.recover(scopeBody())
    applyResult(result)
    await refresh()
  } catch (error) {
    hasError.value = true
    message.value = error instanceof Error ? error.message : '恢复失败'
  } finally {
    busy.value = false
  }
}

function viewRecord() {
  emit('viewRecord', {
    record: record.value,
    recordNo: batch.value?.清洗记录号 ?? props.job.清洗记录号,
  })
}

watch(
  () => props.open,
  async (open) => {
    if (pollTimer) clearInterval(pollTimer)
    pollTimer = undefined
    if (!open) return
    autoDispatched.value = false
    detail.value = props.job.workflow ? { batch: props.job.workflow } : null
    await refresh()
    if (props.autoDispatch && !autoDispatched.value) {
      autoDispatched.value = true
      await dispatch()
    }
    // 迟到的派发/完成结果可能由其他会话写入；打开期间静默跟随服务端状态。
    pollTimer = setInterval(() => void refresh(true), 1500)
  },
  { immediate: true },
)

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>
