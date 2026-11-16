import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import CreateSession from '../components/workflow/CreateSession'
import { PlusOutlined, ReloadOutlined, VideoCameraOutlined } from '@ant-design/icons'
import { Alert, Button, Empty, Input, Space, Table, Tabs, Tag } from 'antd'
import { useRefreshingQuery } from '../components/workflow/useRefreshingQuery'
import { supervisionRequest } from '../api/supervision'
import { lifecycleLabels, lifecycleColors, type SessionDirectory, type SessionRow } from '../api/sessionWorkflow'
import { phaseTitle } from '../api/stages'
import { formatDateTime } from '../utils/ui'
import { useAuthStore } from '../store/useAuthStore'
import { PageHeading } from './Supervision'

export default function Sessions() {
  const role = useAuthStore(state => state.user?.role)
  const [params, setParams] = useSearchParams()
  const status = params.get('status') || 'all', keyword = params.get('keyword') || '', page = Math.max(1, Number(params.get('page')) || 1)
  const [search, setSearch] = useState(keyword)
  const [creating,setCreating]=useState(false)
  useEffect(() => setSearch(keyword), [keyword])
  function filter(key: string, value: string) {
    setParams(previous => { const next = new URLSearchParams(previous); value ? next.set(key, value) : next.delete(key); next.delete('page'); return next })
  }
  const query = useRefreshingQuery<SessionDirectory>(`sessions:${status}:${keyword}:${page}`, signal => supervisionRequest(`/supervision/workflows?page=${page}&keyword=${encodeURIComponent(keyword)}${status === 'all' ? '' : '&status=' + encodeURIComponent(status)}`, { signal }))
  const summary = query.data?.summary
  return <div className="sv-page console-sessions">
    <PageHeading title="场次管理" description="管理场次准备、现场监管与收尾归档。" extra={<><Button icon={<ReloadOutlined />} onClick={query.reload}>刷新</Button>{role === 'admin' && <Button type="primary" icon={<PlusOutlined />} onClick={()=>setCreating(true)}>新建场次</Button>}</>} />
    <div className="console-count-strip" aria-label="场次统计"><span>评前准备 <b>{summary?.preparing ?? '—'}</b></span><span><i className="console-live-dot" />正在监管 <b>{summary?.running ?? '—'}</b></span><span>现场已结束 <b>{summary?.closed ?? '—'}</b></span><small>全部场次统计</small></div>
    <section className="console-directory">
      <div className="console-directory-toolbar"><Tabs activeKey={status} onChange={value => filter('status', value)} items={[{ label: '全部场次', value: 'all' }, { label: '评前准备', value: 'draft' }, { label: '进行中', value: 'running' }, { label: '现场已结束', value: 'closed' }].map(item => ({ key: item.value, label: item.label }))} /><Input.Search aria-label="搜索场次" placeholder="项目、场次或评标室" value={search} onChange={event => setSearch(event.target.value)} allowClear onSearch={value => filter('keyword', value.trim())} /></div>
      {query.error && <Alert type="error" showIcon message={query.error} action={<Button onClick={query.reload}>重试</Button>} />}
      <Table<SessionRow> rowKey="session_id" loading={query.loading} dataSource={query.data?.items || []} scroll={{ x: 1050 }} pagination={{ current: page, pageSize: 12, total: query.data?.total || 0, showSizeChanger: false, showTotal: total => `共 ${total} 场`, onChange: value => setParams(previous => { const next = new URLSearchParams(previous); next.set('page', String(value)); return next }) }} columns={[
        { title: '场次 / 项目', key: 'name', width: 300, render: (_, s) => <div className="sv-event-cell"><Link to={'/sessions/' + s.session_id}>{s.name}</Link><small>{s.project_name}</small></div> },
        { title: '现场 / 阶段', key: 'room', width: 200, render: (_, s) => <div className="sv-event-cell"><span>{s.room_name}</span><small>{phaseTitle(s.phase_id)}</small></div> },
        { title: '监管进度', key: 'status', width: 150, render: (_, s) => <Tag color={lifecycleColors[s.lifecycle]}>{lifecycleLabels[s.lifecycle] || s.lifecycle}</Tag> },
        { title: "待办 / 告警", key: 'events', width: 130, render: (_, s) => <div className="sv-event-cell"><Link className={s.pending_events ? 'console-attention' : ''} to={'/events?queue=active&session_id=' + s.session_id}>{s.pending_events} 条未办结</Link><small>累计 {s.total_events} 条</small></div> },
        { title: '开始时间', dataIndex: 'started_at', width: 160, render: value => value ? formatDateTime(value) : '尚未开始' },
        { title: '操作', key: 'action', width: 210, fixed: 'right', render: (_, s) => <Space>{s.status === 'running' ? <Link to={'/monitor?session_id=' + s.session_id}><Button type="primary" size="small" icon={<VideoCameraOutlined />}>进入值守</Button></Link> : <Link to={'/sessions/' + s.session_id}><Button size="small">{s.status === 'draft' ? "评前准备" : s.lifecycle === 'archived' ? '查看档案' : '收尾归档'}</Button></Link>}{s.status === 'running' && <Link to={'/sessions/' + s.session_id}>场次详情</Link>}</Space> },
      ]} locale={{ emptyText: <Empty description={query.error ? '加载失败，请重试' : "暂无匹配场次"}>{role === 'admin' && !query.error && <Button onClick={()=>setCreating(true)}>新建场次</Button>}</Empty> }} />
    </section>
    {role==='admin'&&<CreateSession open={creating} onClose={()=>setCreating(false)}/>}
  </div>
}
