import { useState } from 'react';
import { Alert, Button, Checkbox, Empty, Select, Space, Tag } from 'antd';
import { supervisionRequest } from '../../api/supervision';
import { fetchAudioDetail } from '../../api/audioRecordings';
import { useRefreshingQuery } from '../workflow/useRefreshingQuery';
import { formatDateTime } from '../../utils/ui';
export default function RoomAudio({sessionId}:{sessionId:string}){
  const records=useRefreshingQuery<{items:{recording_id:string;title:string;status:string;capture_started_at:string|null;source_id:string}[]}>('room-audio:'+sessionId,signal=>supervisionRequest(`/supervision/sessions/${sessionId}/audio`,{signal}));
  const [selected,setSelected]=useState(''),[follow,setFollow]=useState(true),[playError,setPlayError]=useState('');
  const current=records.data?.items.find(r=>r.recording_id===selected)||records.data?.items[0];
  const detail=useRefreshingQuery('room-transcript:'+current?.recording_id,signal=>fetchAudioDetail(current!.recording_id,signal),Boolean(current));
  if(records.error)return <Alert type="warning" message={records.error} action={<Button onClick={records.reload}>重试</Button>}/>;
  if(!current)return <Empty description="本场次评标室暂无已关联录音；没有原音时不生成交流内容结论"/>;
  return <div className="sv-evidence-stack"><Space wrap><Select aria-label="近期场次录音" style={{minWidth:240,maxWidth:'100%'}} value={current.recording_id} options={records.data!.items.map(r=>({value:r.recording_id,label:r.title}))} onChange={value=>{setSelected(value);setFollow(false);setPlayError('');}}/>
    <Checkbox checked={follow} onChange={e=>{setFollow(e.target.checked);if(e.target.checked)setSelected('');}}>跟随最新片段</Checkbox><Button onClick={()=>{records.reload();detail.reload();}}>刷新原音与转写</Button></Space>
    <Space wrap><Tag>{current.capture_started_at?'采集起点：'+formatDateTime(current.capture_started_at):'现场采集时间未登记'}</Tag><Tag>{({ready:'识别完成',queued:'等待识别',processing:'正在识别',failed:'识别失败'} as Record<string,string>)[current.status]||current.status}</Tag><span>来源 {current.source_id}</span></Space>
    {playError&&<Alert type="warning" message={playError}/>}
    <audio key={current.recording_id} src={'/api/audio/recordings/'+current.recording_id+'/original'} controls preload="metadata" style={{width:'100%'}} onError={()=>setPlayError('原始录音不可读取或完整性核验失败，请核对原件')} onPlay={()=>{setSelected(current.recording_id);setFollow(false);}}/>
    <small className="sv-muted">显示按评标室关联的最近30条录音。分段转写有处理延迟；说话人标签不代表已核验专家身份。播放时暂停跟随，避免新片段打断回听。</small>
    {detail.error&&<Alert type="warning" message={detail.error}/>}
    <div className="sv-room-transcripts">{detail.data?.segments.map(s=><div key={s.segment_id} className="sv-transcript-line"><Tag>说话人 {s.speaker||'未区分'}</Tag><small>{(s.start_ms/1000).toFixed(1)}—{(s.end_ms/1000).toFixed(1)} 秒</small><p>{s.text}</p></div>)||<p>正在读取转写…</p>}</div>
  </div>;
}
