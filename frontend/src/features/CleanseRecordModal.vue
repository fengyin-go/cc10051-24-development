<template>
  <div v-if="open" class="modal-backdrop" data-testid="cleanse-record-modal" @click.self="$emit('close')">
    <section class="modal workflow-modal" role="dialog" aria-modal="true" aria-label="清洗记录">
      <header class="workflow-head">
        <div>
          <h3>清洗记录{{ record?.清洗记录号 ? ` · ${record.清洗记录号}` : '' }}</h3>
          <p class="page-desc">同一抽取批次只保留一笔完整清洗确认，迟到结果幂等回填。</p>
        </div>
        <button class="btn ghost" type="button" data-testid="close-record" @click="$emit('close')">关闭</button>
      </header>

      <p v-if="loading" class="workflow-muted">正在读取服务端清洗记录…</p>
      <p v-else-if="error" class="error-text" data-testid="record-error">{{ error }}</p>
      <template v-else-if="record">
        <div class="workflow-badge-row">
          <span class="status-pill" :data-status="String(record.status)" data-testid="cleanse-record-status">
            {{ record.status }}
          </span>
          <strong v-if="record.完整清洗确认" data-testid="complete-confirmation">完整清洗确认</strong>
        </div>
        <dl class="workflow-grid" data-testid="cleanse-record-detail">
          <div><dt>清洗记录号</dt><dd>{{ record.清洗记录号 || '—' }}</dd></div>
          <div><dt>原批次号</dt><dd data-testid="record-batch-no">{{ record.批次号 || '—' }}</dd></div>
          <div><dt>来源系统</dt><dd>{{ record.源系统 || '—' }}</dd></div>
          <div><dt>实体编码</dt><dd>{{ record.实体编码 || '—' }}</dd></div>
          <div><dt>使用规则</dt><dd>{{ record.规则名称 || record.规则 || '—' }}<small v-if="record.规则ID">（#{{ record.规则ID }}）</small></dd></div>
          <div><dt>登记行数</dt><dd data-testid="record-row-count">{{ record.行数 ?? record.row_count ?? 0 }}</dd></div>
          <div><dt>清洗后行数</dt><dd>{{ record.清洗后行数 ?? record.cleaned_count ?? '—' }}</dd></div>
          <div><dt>完成时间</dt><dd>{{ formatTime(record.completed_at || record.updated_at) }}</dd></div>
        </dl>
        <div class="workflow-result" data-testid="record-result">
          <strong>结果与可继续办理</strong>
          <p>{{ record.结果 || '—' }}</p>
          <pre v-if="continuationText" data-testid="record-continuation">{{ continuationText }}</pre>
          <RouterLink
            v-if="record.实体编码"
            class="btn"
            :to="`/dup_detect?entity=${encodeURIComponent(String(record.实体编码))}&batch=${encodeURIComponent(String(record.批次号 || ''))}`"
          >
            前往重复检测器继续
          </RouterLink>
        </div>
      </template>
      <p v-else class="empty-state">暂未读取到清洗记录。</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { cleanseWorkflowApi, type WorkflowDict } from '@/api/workflow'

const props = defineProps<{
  open: boolean
  record?: WorkflowDict | null
  recordNo?: string | number | null
}>()

defineEmits<{ close: [] }>()

const remoteRecord = ref<WorkflowDict | null>(null)
const loading = ref(false)
const error = ref('')

const record = computed<WorkflowDict | null>(() => props.record ?? remoteRecord.value)
const continuationText = computed(() => {
  const value = record.value?.可继续结果 ?? record.value?.continuation
  if (!value) return ''
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2)
})

function formatTime(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

async function load(recordNo: string | number) {
  loading.value = true
  error.value = ''
  try {
    const result = await cleanseWorkflowApi.record(String(recordNo))
    if (!result.ok) throw new Error(result.message || '清洗记录读取失败')
    remoteRecord.value = result.record
  } catch (err) {
    error.value = err instanceof Error ? err.message : '清洗记录读取失败'
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.open, props.recordNo] as const,
  ([open, recordNo]) => {
    if (!open || props.record) return
    if (recordNo) void load(recordNo)
  },
  { immediate: true },
)
</script>
