import { request } from './client'

export type WorkflowDict = Record<string, any>

async function postJson(path: string, body: WorkflowDict = {}): Promise<WorkflowDict> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return (await response.json().catch(() => ({ ok: false, message: '服务端返回无法解析' }))) as WorkflowDict
}

async function getJson(path: string): Promise<WorkflowDict> {
  const response = await request(path)
  return (await response.json().catch(() => ({ ok: false, message: '服务端返回无法解析' }))) as WorkflowDict
}

export const extractWorkflowApi = {
  detail: (id: number | string) => getJson(`/api/record_extract/${id}/workflow`),
  dispatch: (id: number | string, body: WorkflowDict = {}) =>
    postJson(`/api/record_extract/${id}/dispatch`, body),
  complete: (id: number | string, body: WorkflowDict = {}) =>
    postJson(`/api/record_extract/${id}/complete`, body),
  retry: (id: number | string, body: WorkflowDict = {}) =>
    postJson(`/api/record_extract/${id}/retry`, body),
}

export const cleanseWorkflowApi = {
  batches: (query: Record<string, string> = {}) => {
    const params = new URLSearchParams(query).toString()
    return getJson(`/api/std_cleanse/batches?${params}`)
  },
  batch: (batchNo: string) => getJson(`/api/std_cleanse/batches/${encodeURIComponent(batchNo)}`),
  claim: (body: WorkflowDict) => postJson('/api/std_cleanse/claim', body),
  execute: (body: WorkflowDict) => postJson('/api/std_cleanse/execute', body),
  complete: (body: WorkflowDict) => postJson('/api/std_cleanse/complete', body),
  fail: (body: WorkflowDict) => postJson('/api/std_cleanse/fail', body),
  recover: (body: WorkflowDict) => postJson('/api/std_cleanse/recover', body),
  records: () => getJson('/api/std_cleanse/records'),
  record: (recordNo: string) => getJson(`/api/std_cleanse/records/${encodeURIComponent(recordNo)}`),
}
