import { lazy, Suspense, useState } from 'react'
import { Alert, Space, Spin, Switch, Tag } from 'antd'
import { workflowWrite, type SessionWorkflow } from '../../api/sessionWorkflow'
import { notifyWorkflowChange } from '../../store/useWorkflowStore'

const FactsPanel = lazy(() => import('./FactsPanel'))

export default function SessionPersonnel({ session, onSaved }: { session: SessionWorkflow; onSaved: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function setIdentity(enabled: boolean) {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      await workflowWrite(session.session_id, 'identity-setting', { expected_version: session.version, enabled }, 'PUT')
      onSaved()
      notifyWorkflowChange()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '设置未保存，请刷新核对')
    } finally { setBusy(false) }
  }
  return <div className="sv-page session-personnel">
    <Space wrap>
      <strong>人员身份核查</strong>
      <Switch aria-label="启用本场人员身份核查" checked={session.identity_verification_enabled}
        disabled={session.status !== 'draft' || busy} loading={busy} onChange={value => void setIdentity(value)} />
      <Tag color={session.identity_verification_enabled ? 'blue' : undefined}>{session.identity_verification_enabled ? '已启用' : '未启用'}</Tag>
    </Space>
    <p className="hj-help">{session.identity_verification_enabled
      ? "请核对有效名单和身份依据，此选项开始监管后不可修改。"
      : "基础行为监管无需人员名单。启用身份核查后，需准备名单和核验依据，已有记录保留。"}</p>
    {error && <Alert type="error" showIcon message={error} />}
    {session.room_id && <Suspense fallback={<Spin />}><FactsPanel sessionId={session.session_id} roomId={session.room_id}
      identityEnabled={session.identity_verification_enabled} readOnly={session.status === 'closed'} /></Suspense>}
  </div>
}
