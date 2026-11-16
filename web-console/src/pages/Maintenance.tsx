import { lazy, Suspense } from 'react'
import { Alert, Collapse, Spin, Typography } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { PageHeading } from './Supervision'
const FaceSettings=lazy(()=>import('../components/face/FaceSettings'))
export default function Maintenance(){
  const [params]=useSearchParams()
  const sessionId=params.get('session_id')
  const scopeId=sessionId&&/^[0-9a-f-]{36}$/i.test(sessionId)?sessionId:'<场次 UUID>'
  return <div className="sv-page"><PageHeading title="运维设置" description="管理服务接入和身份核验参数。"/>
    <Alert type="info" showIcon message="服务接入" description="新增场次后，在服务器项目目录运行以下命令，登记本场监测范围，再重新创建 API 容器。"/>
    <Typography.Paragraph code style={{marginTop:16}}>{`python3 scripts/configure_phase_consumer.py --session-id ${scopeId}`}</Typography.Paragraph>
    <p className="hj-help">命令读取本场通道范围，不输出令牌。完整说明见 deploy/linux/README.md。</p>
    <Collapse destroyOnHidden items={[{key:'identity',label:"身份核验参数（仅对已启用场次生效）",children:<Suspense fallback={<Spin/>}><FaceSettings/></Suspense>}]}/>
  </div>
}
