import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { CaretRightOutlined, LoadingOutlined, SoundOutlined, WarningFilled } from '@ant-design/icons'
import { fetchAudioDetail, fetchAudioPlayback, fetchAudioRecordings } from '../../api/audioRecordings'
import type { AudioDetail } from '../../api/audioRecordings'

export type TranscriptPanelProps = {
  /** 素材编号，对应录音标题前缀，例如 S06 / S07 */
  source: string
  title: string
  /** 命中即标红的风险表达词表（仅标注，不定性） */
  riskTerms: string[]
  riskLabel: string
  /** 在指定时间点插入一条场景分割线 */
  divider?: { atMs: number; text: string }
  note?: string
  className?: string
}

type Row = { key: string; speaker: string; start: number; end: number; text: string }

const normalize = (text: string) => text.replace(/[\s，。！？、,.!?；;：:…“”"'’‘（）()]/g, '')

function clock(ms: number) {
  const total = Math.max(0, Math.round(ms / 1000))
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
}

function groupRows(detail: AudioDetail): Row[] {
  const rows: Row[] = []
  for (const segment of detail.segments) {
    if (!normalize(segment.text)) continue // 纯标点的空段是识别噪声，不展示
    const speaker = segment.speaker?.trim() || '未定'
    const last = rows[rows.length - 1]
    // 同一说话人、间隔小于 1.2 秒时合并；只去掉分段处被强行加上的逗号，不改一个字
    if (last && last.speaker === speaker && segment.start_ms - last.end <= 1200 && last.text.length < 150) {
      last.text = last.text.replace(/[，、；：]$/, '') + segment.text.replace(/^[，、]/, '')
      last.end = segment.end_ms
    } else {
      rows.push({ key: segment.segment_id, speaker, start: segment.start_ms, end: segment.end_ms, text: segment.text })
    }
  }
  return rows
}

export default function TranscriptPanel({ source, title, riskTerms, riskLabel, divider, note, className }: TranscriptPanelProps) {
  const [detail, setDetail] = useState<AudioDetail | null>(null)
  const [phase, setPhase] = useState<'loading' | 'ready' | 'empty'>('loading')
  const [audioUrl, setAudioUrl] = useState<string | null>(null)
  const [audioBusy, setAudioBusy] = useState(false)
  const objectUrl = useRef<string | null>(null)

  useEffect(() => {
    let alive = true
    setPhase('loading')
    setDetail(null)
    fetchAudioRecordings()
      .then(({ items }) => {
        const hit = items.find(item => item.title.startsWith(source) && item.status === 'ready')
        if (!hit) {
          if (alive) setPhase('empty')
          return null
        }
        return fetchAudioDetail(hit.recording_id)
      })
      .then(data => {
        if (!alive || !data) return
        setDetail(data)
        setPhase(data.segments.length ? 'ready' : 'empty')
      })
      .catch(() => { if (alive) setPhase('empty') })
    return () => { alive = false }
  }, [source])

  useEffect(() => () => { if (objectUrl.current) URL.revokeObjectURL(objectUrl.current) }, [])

  const rows = useMemo(() => detail ? groupRows(detail) : [], [detail])
  const hits = useMemo(() => rows.map(row => riskTerms.filter(term => normalize(row.text).includes(normalize(term)))), [rows, riskTerms])
  const hitCount = hits.filter(list => list.length > 0).length
  const dividerIndex = divider ? rows.findIndex(row => row.start >= divider.atMs) : -1

  const play = async () => {
    if (!detail || audioBusy) return
    setAudioBusy(true)
    try {
      const blob = await fetchAudioPlayback(detail.recording_id)
      const url = URL.createObjectURL(blob)
      objectUrl.current = url
      setAudioUrl(url)
    } catch {
      setAudioUrl(null)
    } finally {
      setAudioBusy(false)
    }
  }

  const hitRows = rows.filter((_, index) => hits[index].length > 0)
  const hitDividerIndex = divider ? hitRows.findIndex(row => row.start >= divider.atMs) : -1
  const listRef = useRef<HTMLDivElement | null>(null)
  const focusRow = (key: string) => {
    const list = listRef.current
    const target = list?.querySelector<HTMLElement>(`[data-row="${key}"]`)
    if (!list || !target) return
    list.scrollTo({ top: Math.max(0, target.offsetTop - 8), behavior: 'smooth' })
  }

  const body = phase === 'loading'
    ? <div className="sf-transcript-loading"><LoadingOutlined />正在读取转写…</div>
    : phase === 'empty' || !detail
      ? <div className="sf-transcript-empty"><WarningFilled />未读取到 {source} 的录音转写，需管理员登录且录音处理完成。</div>
      : <div className={`sf-transcript-body${hitRows.length ? ' has-hits' : ''}`}>
        <div className="sf-utt-list" ref={listRef}>
          {rows.map((row, index) => {
            const matched = hits[index]
            return <Fragment key={row.key}>
              {index === dividerIndex && divider
                ? <div className="sf-utt-divider"><span>{divider.text}</span></div>
                : null}
              <div className={`sf-utt is-speaker-${row.speaker}${matched.length ? ' is-hit' : ''}`} data-row={row.key}>
                <span className="sf-utt-who">说话人 {row.speaker}</span>
                <span className="sf-utt-time">{clock(row.start)}</span>
                <p className="sf-utt-text">{row.text}</p>
                {matched.length
                  ? <span className="sf-utt-tag"><WarningFilled />{matched.slice(0, 2).join(' / ')}</span>
                  : null}
              </div>
            </Fragment>
          })}
        </div>

        {hitRows.length
          ? <aside className="sf-utt-hits">
            <div className="sf-utt-hits-title">命中{riskLabel} · {hitRows.length} 处</div>
            {hitRows.map((row, order) => <Fragment key={`hit-${row.key}`}>
              {order === hitDividerIndex && divider
                ? <div className="sf-utt-hits-divider">{divider.text}</div>
                : null}
              <button type="button" className="sf-utt-hit" onClick={() => focusRow(row.key)}>
                <span className="sf-utt-hit-top">
                  <b>{clock(row.start)}</b>
                  <i>说话人 {row.speaker}</i>
                </span>
                <span className="sf-utt-hit-text">{row.text}</span>
              </button>
            </Fragment>)}
          </aside>
          : null}
      </div>

  return <div className={`sf-panel sf-transcript ${className ?? ''}`.trim()}>
    <div className="sf-transcript-top">
      <div className="sf-transcript-title">
        <div className="sf-panel-title">{title}</div>
        {phase === 'ready' && detail
          ? <div className="sf-transcript-meta">
            <span>时长 {clock(detail.duration_ms)}</span>
            <i />
            <span>{detail.speaker_count ?? '—'} 位说话人</span>
            <i />
            <span>{detail.segment_count} 段</span>
            {hitCount > 0 ? <><i /><span className="is-hit">{hitCount} 处命中{riskLabel}</span></> : null}
          </div>
          : null}
      </div>

      {phase === 'ready' && detail
        ? (audioUrl
          ? <audio className="sf-transcript-audio" src={audioUrl} controls autoPlay />
          : <button type="button" className="sf-transcript-play" onClick={play} disabled={audioBusy}>
            {audioBusy ? <LoadingOutlined /> : <CaretRightOutlined />}
            {audioBusy ? '加载中' : '播放原始录音'}
          </button>)
        : null}
    </div>

    {body}

    {phase === 'ready' && detail
      ? <div className="sf-transcript-foot">
        <span><SoundOutlined />{detail.model ?? '语音识别'} · 说话人分离 {detail.speaker_model ?? '—'} · 原文未人工改写</span>
        {note ? <em>{note}</em> : null}
      </div>
      : null}
  </div>
}
