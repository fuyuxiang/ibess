import { useState } from 'react'
import { Alert, Button, Card, Empty, Segmented, Table, Tag } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import { fetchMonitoringHealth } from '../api/supervision'
import { healthLabels } from '../api/workspace'
import { useAuthStore } from '../store/useAuthStore'
import { useRefreshingQuery } from '../components/workflow/useRefreshingQuery'
import { useSupervision } from '../components/workflow/useSupervision'
import { PageHeading, RequestError, eventColumns } from './Supervision'
import { formatDateTime } from '../utils/ui'

export default function MonitoringAssurance() {
  const role=useAuthStore(s=>s.user?.role);
  const query = useRefreshingQuery('monitoring-health',signal=>fetchMonitoringHealth(signal));
  const health = query.data;
  const [showAll,setShowAll]=useState(false);
  const items=health?.items||[];
  const needsAttention=items.filter(item=>(item.kind==='audio'?[item.audio_status,item.analysis_status]:[item.stream_status,item.analysis_status,item.recording_status]).some(state=>!['online','recent','running'].includes(state||'unknown')));
  const hasAudio=items.some(item=>item.kind==='audio');
  const error = query.error;
  const loading = query.loading;
  const faults = useSupervision({ category: "technical", page_size: 10 });
  return (
    <div className="sv-page">
      <PageHeading
        title="运行保障"
        description="查看机位、录像和分析状态，处理设备与服务故障。"
        extra={
          <Button
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={() => {
              query.reload();
              faults.reload();
            }}
          >
            检查状态
          </Button>
        }
      />
      <RequestError
        error={error || faults.error}
        reload={() => {
          query.reload();
          faults.reload();
        }}
      />
      <Alert
        type="info"
        showIcon
        message="状态基于近期检测"
        description="未收到有效记录时显示“待核验”。近期录像活动不等于全程录像完整。"
      />
      <Card
        title={`监测状态 · ${needsAttention.length} 路异常或待核验`}
        extra={
          <span className="sv-muted">
            检查时间：{formatDateTime(health?.checked_at)}
          </span>
        }
      >
        <Segmented style={{marginBottom:16}} value={showAll?'all':'attention'} options={[{value:'attention',label:'异常与待核验'},{value:'all',label:"全部通道"}]} onChange={value=>setShowAll(value==='all')}/>
        <Table
          rowKey="binding_id"
          dataSource={showAll?items:needsAttention}
          loading={loading}
          pagination={false}
          scroll={{ x: 900 }}
          columns={[
            {
              title: "项目 / 评标室",
              key: "room",
              render: (_, s) => (
                <div className="sv-event-cell">
                  <strong>{s.project_name}</strong>
                  <small>
                    {s.room_name} · {s.session_name}
                  </small>
                </div>
              ),
            },
            { title: "监测通道", render:(_,s)=>s.source_name||s.source_id },
            ...[["视频连接",'stream_status'],['语音输入','audio_status'],["分析状态",'analysis_status'],["录像状态",'recording_status']].filter(([,field])=>field!=='audio_status'||hasAudio).map(([title,dataIndex])=>({title,dataIndex,render:(state:string)=><Tag color={['running','recent','online'].includes(state)?'green':['offline','input_error','stale'].includes(state)?'red':'default'}>{healthLabels[state]||'待核验'}</Tag>})),
            {
              title: "操作",
              key: "action",
              render: (_, s) => (
                <Link to={`/monitor?binding_id=${s.binding_id}`}>查看现场</Link>
              ),
            },
          ]}
          locale={{
            emptyText: (
              <Empty
                description={
                  error ? "状态加载失败" : items.length?"暂无异常或待核验通道，可切换查看全部":"暂无监测通道，请在场次准备中选择现场资源"
                }
              />
            ),
          }}
        />
      </Card>
      <Card
        title="设备与服务故障"
        extra={<Link to="/events?category=technical">全部故障记录</Link>}
      >
        <Table
          rowKey="event_id"
          dataSource={faults.data?.items || []}
          columns={eventColumns()}
          loading={faults.loading}
          pagination={false}
          scroll={{ x: 1100 }}
        />
      </Card>
      {role==='admin'&&<Link to="/admin/maintenance"><Button>运维设置</Button></Link>}
    </div>
  );
}
