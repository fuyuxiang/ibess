import { AuditOutlined } from '@ant-design/icons'
import { Card, Tag } from 'antd'
import StageConsole from '../components/stages/StageConsole'
import '../stage-console-page.css'

export default function Stages() {
  return <div className="stage-page">
    <Card className="stage-page-heading">
      <div className="stage-page-title"><AuditOutlined /><div><h1>评标阶段</h1><p>按开标、评审、签署和离线质检流程推进业务演示。</p></div></div>
      <Tag color="blue">业务流程控制台</Tag>
    </Card>
    <StageConsole />
    <div className="stage-page-note">阶段配置保存后，由检测服务确认应用；具体事件仍需人工复核。素材登记与播放接入正在开发。</div>
  </div>
}
