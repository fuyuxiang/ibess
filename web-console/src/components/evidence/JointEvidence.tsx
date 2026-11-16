import { Empty, Tag } from 'antd'
import type { EventDetail } from '../../api/client'
import { formatDateTime } from '../../utils/ui'

/**
 * 音视频联合核查：把同一事件时刻前后的"录像片段"和"现场拾音"放在一起看。
 *
 * 说明：这个文件之前被 .gitignore 的 `evidence/` 规则误伤，没有随提交入库，
 * 这里按 SupervisionDetail 的调用约定（detail + reload）补齐实现。
 */
export default function JointEvidence({ detail }: { detail: EventDetail; reload: () => void }) {
  const videos = detail.evidences.filter((item) => item.evidence_type === 'video')
  const context = detail.context_evidence
  const audios = context?.audio || []
  const range = context?.range

  if (!videos.length && !audios.length) {
    return <Empty description="尚未关联音视频证据 —— 事件落库后会自动截取前后片段" />
  }

  return <div className="je-wrap">
    <div className="je-meta">
      <span><em>事件时刻</em>{formatDateTime(detail.occurred_at)}</span>
      {range?.started_at && <span><em>取证窗口</em>{formatDateTime(range.started_at)} — {formatDateTime(range.ended_at)}</span>}
      <span><em>机位</em>{detail.camera_id || '—'}</span>
      <span><em>片段</em><b>{videos.length}</b> 段 · 录音 <b>{audios.length}</b> 条</span>
    </div>

    <div className="je-grid">
      <section className="je-pane">
        <header><Tag color="blue">视频</Tag><span>事件前后 45 秒</span></header>
        {videos.length
          ? videos.map((item) => <div className="je-video" key={item.id}>
              <video src={item.url} controls preload="metadata" />
              <small>{formatDateTime(item.captured_at)} · {item.object_key}</small>
            </div>)
          : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本事件暂无录像片段" />}
      </section>

      <section className="je-pane">
        <header><Tag color="green">音频</Tag><span>现场拾音 · 与事件同时段</span></header>
        {audios.length
          ? audios.map((item) => <div className="je-audio" key={item.recording_id}>
              <strong>{item.title}</strong>
              <small>
                {formatDateTime(item.capture_started_at)}
                {item.duration_ms ? ` · 时长 ${(item.duration_ms / 1000).toFixed(0)}s` : ''}
                {item.note ? ` · ${item.note}` : ''}
              </small>
              <audio src={item.playback_url} controls preload="none" />
              {item.segments?.length
                ? <div className="je-transcript">
                    {item.segments.map((seg) => <div key={seg.segment_id}>
                      <em>{seg.speaker || '未知'} {(seg.start_ms / 1000).toFixed(1)}s</em>
                      <span>{seg.text}</span>
                    </div>)}
                  </div>
                : null}
            </div>)
          : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本事件暂无关联录音" />}
      </section>
    </div>

    {context?.annotations?.length ? <div className="je-notes">
      <header>人工校订记录</header>
      {context.annotations.slice(0, 6).map((item) => <div key={item.annotation_id}>
        <Tag>{item.kind}</Tag>
        <span>{item.original?.speaker || '未知说话人'}：{item.original?.text}</span>
        <small>{item.actor} · {formatDateTime(item.at)}</small>
      </div>)}
    </div> : null}
  </div>
}
