import { describePhaseStatus, phaseTitle } from '../../api/stages'
import type { StageConfig, StageControl } from '../../api/stages'
import '../../phase-badge.css'

interface Props {
  config: StageConfig | null
  control: StageControl | null
  loading?: boolean
  error?: string | null
  needsConfiguration?: boolean
}
export default function PhaseBadge({ config, control, loading = false, error, needsConfiguration = false }: Props) {
  const status = loading ? { label: '正在刷新', tone: 'neutral' }
    : error ? { label: '状态获取失败', tone: 'danger' }
    : needsConfiguration ? { label: '阶段待配置', tone: 'warning' }
    : describePhaseStatus(config, control)
  return <div className="phase-badge" role="status" aria-live="polite" aria-busy={loading}>
    <span className="phase-badge-title">{config ? phaseTitle(config.phase_id) : '评标阶段'}</span>
    {config && <span className="phase-badge-version">v{config.phase_version}</span>}
    <span className={`phase-badge-state phase-badge-state--${status.tone}`}>{status.label}</span>
    {error && <span className="phase-badge-error">{error}</span>}
  </div>
}
