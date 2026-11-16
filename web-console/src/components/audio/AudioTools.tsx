import { lazy, Suspense } from 'react'
import { Alert, Spin, Tabs } from 'antd'
const Transcript=lazy(()=>import('./AudioTranscriptPanel'))
const Analysis=lazy(()=>import('./SemanticReviewPanel'))
export default function AudioTools({roomId}:{roomId?:string}){
  return <div className="sv-page audio-support-tools"><Alert type="info" showIcon message="核对录音所属现场与采集时间" description="处理完成后，返回补证窗口关联时间重叠的录音。"/>
    <Tabs destroyOnHidden items={[{key:'transcript',label:'补充录音与转写',children:<Suspense fallback={<Spin/>}><Transcript/></Suspense>},{key:'analysis',label:"录音风险分析",children:<Suspense fallback={<Spin/>}><Analysis initialRoomId={roomId}/></Suspense>}]}/>
  </div>
}
