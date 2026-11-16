import { useEffect, useState } from 'react'
import { Image, Select } from 'antd'
import { AudioOutlined, CameraOutlined, PlayCircleOutlined, WarningOutlined } from '@ant-design/icons'
import type { EventDetail } from '../../api/client'
import { fetchEventRecordings } from '../../api/eventRecordings'
import { useRefreshingQuery } from '../workflow/useRefreshingQuery'
import { evidenceLabels } from '../workflow/supervision-model'
import { formatDateTime, formatTime } from '../../utils/ui'

export default function EvidencePlayer({ detail, tab, setTab }: { detail: EventDetail; tab: string; setTab: (tab: string) => void }) {
  const recordings = useRefreshingQuery('duty-recordings:' + detail.event_id, signal => fetchEventRecordings(detail.event_id,signal))
  const saved = (recordings.data?.items || []).map(item=>({id:'segment-'+item.index,url:item.playback_url,captured_at:new Date(item.start_epoch*1000).toISOString(),seek_seconds:item.seek_seconds}))
  const clips = [...saved,...detail.evidences.filter(item => item.evidence_type === 'video' && !saved.some(segment=>segment.url===item.url)).map(item=>({...item,seek_seconds:0}))]
  const frames = detail.evidences.filter(item => item.evidence_type === 'keyframe')
  const audio = detail.context_evidence?.audio || []
  const [clip, setClip] = useState(0), [frame, setFrame] = useState(0), [error, setError] = useState('')
  useEffect(() => { setClip(0); setFrame(0); setError('') }, [detail.event_id])
  const selectedClip = clips[Math.min(clip, clips.length - 1)]
  const selectedFrame = frames[Math.min(frame, frames.length - 1)]
  return <div className="duty-evidence">
    <div className="duty-media-tabs" role="tablist" aria-label="事件证据类型">{[{key:'video',label:'录像片段',count:clips.length,icon:<PlayCircleOutlined/>},{key:'frames',label:'关键帧',count:frames.length,icon:<CameraOutlined/>},{key:'audio',label:'现场原音',count:audio.length,icon:<AudioOutlined/>}].map(item => <button key={item.key} role="tab" aria-selected={tab === item.key} onClick={() => { setTab(item.key); setError('') }}>{item.icon}{item.label}<small>{item.count}</small></button>)}<span className="duty-evidence-state">{evidenceLabels[detail.evidence_status || ''] || '资料待核验'}</span></div>
    <div className="duty-evidence-screen">
      {tab === 'video' && (selectedClip ? <video key={selectedClip.id} src={selectedClip.url} controls autoPlay muted playsInline onLoadedMetadata={event=>{const element=event.currentTarget;if(Number.isFinite(selectedClip.seek_seconds)&&selectedClip.seek_seconds>0&&selectedClip.seek_seconds<element.duration)element.currentTime=selectedClip.seek_seconds}} onError={() => setError("录像读取失败，请重试或在事件详情中核对保全状态。")} aria-label="告警关联录像"/> : <div className="duty-screen-message"><PlayCircleOutlined/><strong>录像片段尚不可用</strong><span>{recordings.loading?"正在查找关联录像…":recordings.error||evidenceLabels[detail.evidence_status || ''] || "请等待证据关联或保全完成"}</span></div>)}
      {tab === 'frames' && (selectedFrame ? <Image key={selectedFrame.id} width="100%" height="100%" src={selectedFrame.full_url || selectedFrame.url} preview={{src:selectedFrame.full_url || selectedFrame.url}} onError={() => setError("关键帧读取失败，请在事件详情中核对。")} alt={'事件关键帧，采集于 ' + formatDateTime(selectedFrame.captured_at)}/> : <div className="duty-screen-message"><CameraOutlined/><strong>尚无关联关键帧</strong></div>)}
      {tab === 'audio' && <div className="duty-audio-evidence">{audio.length ? audio.map(item => <section key={item.recording_id}><h3><AudioOutlined/> {item.title}</h3><p>{formatDateTime(item.capture_started_at)}</p><audio src={item.playback_url} controls preload="none" aria-label={item.title} onError={() => setError('原音读取失败，请核对资料状态。')}/>{item.segments?.map(segment => <p key={segment.segment_id}><b>{segment.speaker || '未知说话人'}</b> {segment.text}</p>)}</section>) : <div className="duty-screen-message"><AudioOutlined/><strong>尚无关联原音</strong></div>}</div>}
      <div className="duty-playback-label"><span>事件证据</span>{formatDateTime(detail.occurred_at)}</div>
      {error && <div className="duty-media-error"><WarningOutlined/> {error}</div>}
    </div>
    {tab==='video'&&recordings.data&&!recordings.data.event_covered&&<p className="duty-coverage-note"><WarningOutlined/> 事发时段的录像覆盖待核验，请核对证据。</p>}
    {tab === 'video' && clips.length > 1 && <Select aria-label="切换证据片段" value={clip} onChange={value=>{setClip(value);setError('')}} options={clips.map((item,index) => ({value:index,label:'片段 ' + (index+1) + ' · ' + formatDateTime(item.captured_at)}))}/>}
    {tab === 'frames' && frames.length > 0 && <div className="duty-frame-strip">{frames.map((item,index) => <button key={item.id} className={frame === index ? 'active' : ''} onClick={() => { setFrame(index); setError('') }} aria-label={'关键帧 ' + (index+1)}><img src={item.url} alt=""/><span>{formatTime(item.captured_at)}</span></button>)}</div>}
  </div>
}
