import { useEffect, useMemo, useRef, useState } from 'react'
import { ApartmentOutlined, EditOutlined, PlusOutlined, ReloadOutlined, SearchOutlined, TeamOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Empty, Form, Input, Modal, Popconfirm, Select, Space, Switch, Table, Tabs, Tag, message } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { createRegistryItem, deleteRegistryItem, updateRegistryItem } from '../api/registry'
import type { ParticipantInput, PersonInput, PersonRole, ProjectInput, ProjectStatus, RegistryEntity, RegistryParticipant, RegistryPerson, RegistryProject, RegistryRoom, RegistrySeat, RoomInput, RoomMode, SeatInput } from '../api/registry'
import { useRegistry, useRegistryStore } from '../store/useRegistryStore'
import FaceMatchModal from '../components/face/FaceMatchModal'
import PersonVerificationAdminModal from '../components/face/PersonVerificationAdminModal'
import '../registry.css'

type MainEntity = Exclude<RegistryEntity, 'participants'>
type MainRow = RegistryProject | RegistryRoom | RegistryPerson | RegistrySeat
interface EditorState { entity: MainEntity; row: MainRow | null }
interface EditorValues {
  code: string; name: string; owner: string; start_at?: string; end_at?: string
  status: ProjectStatus; project_id: string; location: string; mode: RoomMode
  room_id: string; person_id?: string; unit: string; role: PersonRole; enabled: boolean
}
const entityLabels: Record<MainEntity, string> = { projects: '项目', rooms: '评标室', seats: '工位', persons: '人员' }
const roleLabels: Record<PersonRole, string> = { expert: '评标专家', supervisor: '监督人员', staff: '工作人员', owner: '业主代表', agent: '代理机构', bidder: '投标人' }
const statusLabels: Record<ProjectStatus, string> = { draft: '草稿', running: '进行中', closed: '已关闭' }
const roleOptions = Object.entries(roleLabels).map(([value, label]) => ({ value, label }))
const requiredText = [{ required: true, whitespace: true, message: '请输入非空内容' }]
function idOf(row: MainRow) {
  if ('project_id' in row && 'owner' in row) return row.project_id
  if ('room_id' in row && 'location' in row) return row.room_id
  if ('person_id' in row && 'unit' in row) return row.person_id
  return (row as RegistrySeat).seat_id
}
function enabledTag(enabled: boolean) { return <Tag color={enabled ? 'green' : undefined}>{enabled ? '启用' : '停用'}</Tag> }
function errorText(error: unknown) { return error instanceof Error ? error.message : '操作失败，请重试' }
function dateLabel(value: string | null) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '未设置' }
function localDateInput(value: string | null) {
  if (!value) return undefined
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return undefined
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000)
  return shifted.toISOString().slice(0, 16)
}

export default function Registry() {
  const { data, loading, error, refresh } = useRegistry()
  const projects = data?.projects ?? []
  const rooms = data?.rooms ?? []
  const persons = data?.persons ?? []
  const seats = data?.seats ?? []
  const participants = data?.participants ?? []
  const [tab, setTab] = useState<MainEntity>('projects')
  const [search, setSearch] = useState('')
  const [facePersonId, setFacePersonId] = useState<string | null>(null)
  const [verificationPersonId, setVerificationPersonId] = useState<string | null>(null)
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [memberProjectId, setMemberProjectId] = useState<string | null>(null)
  const [editingMember, setEditingMember] = useState<RegistryParticipant | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [operationError, setOperationError] = useState<string | null>(null)
  const mutationLock = useRef(false)
  const [form] = Form.useForm<EditorValues>()
  const [memberForm] = Form.useForm<ParticipantInput>()
  const selectedRoomId = Form.useWatch('room_id', form)
  const selectedRoom = rooms.find((room) => room.room_id === selectedRoomId)
  const [messageApi, messageHolder] = message.useMessage()
  useEffect(() => {
    if (!editor) return
    form.resetFields()
    const values: Partial<EditorValues> = { status: 'draft', mode: 'centralized', role: 'expert', enabled: true }
    if (editor.row) Object.assign(values, editor.row)
    if (editor.entity === 'projects' && editor.row) {
      const project = editor.row as RegistryProject
      values.start_at = localDateInput(project.start_at)
      values.end_at = localDateInput(project.end_at)
    }
    if (editor.entity === 'seats' && editor.row) values.person_id = (editor.row as RegistrySeat).person_id ?? undefined
    form.setFieldsValue(values)
  }, [editor, form])
  const projectMap = useMemo(() => new Map(projects.map((item) => [item.project_id, item])), [projects])
  const roomMap = useMemo(() => new Map(rooms.map((item) => [item.room_id, item])), [rooms])
  const personMap = useMemo(() => new Map(persons.map((item) => [item.person_id, item])), [persons])
  const projectLabel = (id: string) => {
    const project = projectMap.get(id)
    return project ? `${project.name}（${project.code}）` : `未找到项目 · ${id}`
  }
  const roomLabel = (id: string) => {
    const room = roomMap.get(id)
    return room ? `${projectLabel(room.project_id)} / ${room.name}（${room.code}）` : `未找到评标室 · ${id}`
  }
  const personLabel = (id: string | null) => {
    if (!id) return '未分配'
    const person = personMap.get(id)
    return person ? `${person.name}（${person.code}）${person.enabled ? '' : ' · 已停用'}` : `未找到人员 · ${id}`
  }
  const memberProject = memberProjectId ? projectMap.get(memberProjectId) : undefined
  const projectMembers = participants.filter((item) => item.project_id === memberProjectId)
  const memberIds = new Set(projectMembers.map((item) => item.person_id))
  const editingSeatId = editor?.entity === 'seats' && editor.row ? (editor.row as RegistrySeat).seat_id : null
  const assignedPersonId = editor?.entity === 'seats' && editor.row ? (editor.row as RegistrySeat).person_id : null
  const eligiblePersonIds = new Set(participants.filter((member) => member.project_id === selectedRoom?.project_id).map((member) => member.person_id))
  const seatPersonOptions = persons.filter((person) => person.enabled && eligiblePersonIds.has(person.person_id)).map((person) => {
    const occupied = seats.some((seat) => seat.seat_id !== editingSeatId && seat.project_id === selectedRoom?.project_id && seat.person_id === person.person_id)
    return { value: person.person_id, label: `${personLabel(person.person_id)} · ${person.unit || '未填单位'}${occupied ? ' · 已分配工位' : ''}`, disabled: occupied }
  })
  if (assignedPersonId && selectedRoomId === (editor?.row as RegistrySeat | null)?.room_id && !seatPersonOptions.some((option) => option.value === assignedPersonId)) {
    seatPersonOptions.push({ value: assignedPersonId, label: `${personLabel(assignedPersonId)} · 当前分配（只能保留或取消）`, disabled: true })
  }

  async function mutate(key: string, action: () => Promise<unknown>, onSuccess?: () => void) {
    if (mutationLock.current) return
    mutationLock.current = true
    setBusy(key)
    setOperationError(null)
    try {
      await action()
      onSuccess?.()
      messageApi.success('操作成功，已保存到后端')
      await refresh(true)
      if (useRegistryStore.getState().error) messageApi.warning('保存成功，但配置刷新失败，请点击刷新重试')
    } catch (cause) { setOperationError(errorText(cause)) }
    finally { mutationLock.current = false; setBusy(null) }
  }

  function openEditor(entity: MainEntity, row: MainRow | null = null) {
    setOperationError(null)
    setEditor({ entity, row })
  }

  async function saveEditor(values: EditorValues) {
    if (!editor) return
    const entity = editor.entity
    const rowId = editor.row ? idOf(editor.row) : null
    const text = (value?: string) => value?.trim() ?? ''
    const save = () => {
      switch (entity) {
        case 'projects': {
          const input: ProjectInput = { code: text(values.code), name: text(values.name), owner: text(values.owner), status: values.status, start_at: values.start_at ? new Date(values.start_at).toISOString() : null, end_at: values.end_at ? new Date(values.end_at).toISOString() : null }
          return rowId ? updateRegistryItem(entity, rowId, input) : createRegistryItem(entity, input)
        }
        case 'rooms': {
          const input: RoomInput = { code: text(values.code), name: text(values.name), project_id: values.project_id, location: text(values.location), mode: values.mode, enabled: values.enabled }
          return rowId ? updateRegistryItem(entity, rowId, input) : createRegistryItem(entity, input)
        }
        case 'persons': {
          const input: PersonInput = { code: text(values.code), name: text(values.name), unit: text(values.unit), role: values.role, enabled: values.enabled }
          return rowId ? updateRegistryItem(entity, rowId, input) : createRegistryItem(entity, input)
        }
        case 'seats': {
          const input: SeatInput = { code: text(values.code), room_id: values.room_id, person_id: values.person_id || null, enabled: values.enabled }
          return rowId ? updateRegistryItem(entity, rowId, input) : createRegistryItem(entity, input)
        }
      }
    }
    await mutate('editor', save, () => setEditor(null))
  }

  async function toggleEnabled(entity: 'rooms' | 'persons' | 'seats', row: RegistryRoom | RegistryPerson | RegistrySeat) {
    await mutate(`${entity}:${idOf(row)}`, () => {
      if (entity === 'rooms') {
        const item = row as RegistryRoom
        return updateRegistryItem(entity, item.room_id, { project_id: item.project_id, code: item.code, name: item.name, location: item.location, mode: item.mode, enabled: !item.enabled })
      }
      if (entity === 'persons') {
        const item = row as RegistryPerson
        return updateRegistryItem(entity, item.person_id, { code: item.code, name: item.name, unit: item.unit, role: item.role, enabled: !item.enabled })
      }
      const item = row as RegistrySeat
      return updateRegistryItem(entity, item.seat_id, { room_id: item.room_id, code: item.code, person_id: item.person_id, enabled: !item.enabled })
    })
  }

  function actions(entity: MainEntity, row: MainRow) {
    const id = idOf(row)
    return <Space size={8} wrap>
      <Button type="link" size="small" icon={<EditOutlined />} disabled={!!busy} onClick={() => openEditor(entity, row)}>编辑</Button>
      {entity === 'projects' && <Button type="link" size="small" disabled={!!busy} onClick={() => { setOperationError(null); setMemberProjectId(id); setEditingMember(null); memberForm.resetFields() }}>关联人员</Button>}
      {entity !== 'projects' && <Popconfirm title={`确认${(row as RegistrySeat).enabled ? '停用' : '启用'}该${entityLabels[entity]}？`} description="不会更改已记录的历史事件名称。" okText="确认" cancelText="取消" disabled={!!busy} onConfirm={() => toggleEnabled(entity, row as RegistryRoom | RegistryPerson | RegistrySeat)}><Button type="link" size="small" disabled={!!busy}>{(row as RegistrySeat).enabled ? '停用' : '启用'}</Button></Popconfirm>}
      <Popconfirm title={`确认删除该${entityLabels[entity]}？`} description="有关联子项、工位分配或历史事件引用时，后端会拒绝删除。" okText="删除" cancelText="取消" okButtonProps={{ danger: true }} disabled={!!busy} onConfirm={() => mutate(`delete:${id}`, () => deleteRegistryItem(entity, id))}><Button type="link" danger size="small" disabled={!!busy}>删除</Button></Popconfirm>
    </Space>
  }

  const projectColumns: ColumnsType<RegistryProject> = [
    { title: '项目编号', dataIndex: 'code', width: 140 },
    { title: '项目名称', dataIndex: 'name', width: 200 },
    { title: '业主 / 负责人', dataIndex: 'owner', width: 150, render: (value: string) => value || '未填写' },
    { title: '项目时间', width: 205, render: (_, row) => <div className="registry-date"><span>{dateLabel(row.start_at)}</span><span>至 {dateLabel(row.end_at)}</span></div> },
    { title: '状态', dataIndex: 'status', width: 90, render: (value: ProjectStatus) => <Tag color={value === 'running' ? 'blue' : undefined}>{statusLabels[value] ?? value}</Tag> },
    { title: '关联人员', width: 95, render: (_, row) => participants.filter((item) => item.project_id === row.project_id).length },
    { title: '操作', width: 250, fixed: 'right', render: (_, row) => actions('projects', row) },
  ]
  const roomColumns: ColumnsType<RegistryRoom> = [
    { title: '评标室编号', dataIndex: 'code', width: 140 },
    { title: '评标室名称', dataIndex: 'name', width: 170 },
    { title: '所属项目', dataIndex: 'project_id', width: 220, render: (id: string) => projectLabel(id) },
    { title: '位置', dataIndex: 'location', width: 180, render: (value: string) => value || '未填写' },
    { title: '模式', dataIndex: 'mode', width: 100, render: (value: RoomMode) => value === 'remote' ? '异地评标' : '集中评标' },
    { title: '状态', dataIndex: 'enabled', width: 80, render: enabledTag },
    { title: '操作', width: 205, fixed: 'right', render: (_, row) => actions('rooms', row) },
  ]
  const personColumns: ColumnsType<RegistryPerson> = [
    { title: '人员编号', dataIndex: 'code', width: 140 },
    { title: '姓名', dataIndex: 'name', width: 130 },
    { title: '单位', dataIndex: 'unit', width: 240, render: (value: string) => value || '未填写' },
    { title: '默认角色', dataIndex: 'role', width: 120, render: (value: PersonRole) => roleLabels[value] ?? value },
    { title: '状态', dataIndex: 'enabled', width: 80, render: enabledTag },
    { title: '操作', width: 470, fixed: 'right', render: (_, row) => <Space size={4} wrap>{actions('persons', row)}<Button type="link" size="small" disabled={!!busy} onClick={() => setFacePersonId(row.person_id)}>人脸登记</Button><Button type="link" size="small" disabled={!!busy} onClick={() => setVerificationPersonId(row.person_id)}>人员核验</Button></Space> },
  ]
  const seatColumns: ColumnsType<RegistrySeat> = [
    { title: '工位编号', dataIndex: 'code', width: 130 },
    { title: '所属评标室 / 项目', dataIndex: 'room_id', width: 300, render: (id: string) => roomLabel(id) },
    { title: '分配人员', dataIndex: 'person_id', width: 200, render: (id: string | null) => personLabel(id) },
    { title: '状态', dataIndex: 'enabled', width: 80, render: enabledTag },
    { title: '操作', width: 270, fixed: 'right', render: (_, row) => <Space size={4} wrap>{actions('seats', row)}{row.person_id && <Popconfirm title="确认取消该工位的人员分配？" okText="取消分配" cancelText="返回" disabled={!!busy} onConfirm={() => mutate(`unassign:${row.seat_id}`, () => updateRegistryItem('seats', row.seat_id, { code: row.code, room_id: row.room_id, person_id: null, enabled: row.enabled }))}><Button type="link" size="small" disabled={!!busy}>取消分配</Button></Popconfirm>}</Space> },
  ]
  const memberColumns: ColumnsType<RegistryParticipant> = [
    { title: '人员', dataIndex: 'person_id', width: 230, render: (id: string) => personLabel(id) },
    { title: '单位', width: 170, render: (_, row) => personMap.get(row.person_id)?.unit || '未填写' },
    { title: '项目内角色', dataIndex: 'role', width: 130, render: (value: PersonRole) => roleLabels[value] ?? value },
    { title: '操作', width: 150, render: (_, row) => <Space size={4}><Button type="link" size="small" disabled={!!busy} onClick={() => { setEditingMember(row); memberForm.setFieldsValue(row); setOperationError(null) }}>编辑</Button><Popconfirm title="确认解除该人员与项目的关联？" description="人员仍被工位分配时，后端会拒绝解除。" okText="解除关联" cancelText="取消" disabled={!!busy} onConfirm={() => mutate(`member:${row.membership_id}`, () => deleteRegistryItem('participants', row.membership_id), () => { if (editingMember?.membership_id === row.membership_id) { setEditingMember(null); memberForm.resetFields() } })}><Button type="link" danger size="small" disabled={!!busy}>移除</Button></Popconfirm></Space> },
  ]
  const matches = (values: unknown[]) => values.join(' ').toLowerCase().includes(search.trim().toLowerCase())
  const empty = <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={error && !data ? '基础配置未加载，请重试' : search.trim() ? '没有匹配的配置' : '暂无配置，点击新增开始录入'} />
  const tableProps = { loading, size: 'middle' as const, pagination: { pageSize: 10, showSizeChanger: false, hideOnSinglePage: true }, locale: { emptyText: empty }, scroll: { x: 1050 } }

  return <div className="registry-page">
    {messageHolder}
    <Card className="registry-heading-card"><div className="registry-heading"><div><h1><ApartmentOutlined />基础管理</h1><p>维护项目、评标室、工位与人员，配置保存后立即同步业务页面。</p></div><Button icon={<ReloadOutlined />} loading={loading} disabled={!!busy} onClick={() => void refresh(true)}>刷新配置</Button></div></Card>
    {error && <Alert type="error" showIcon message="基础配置加载失败" description={`${error}${data ? '；当前保留最近成功加载的配置。' : ''}`} action={<Button size="small" loading={loading} onClick={() => void refresh(true)}>重试</Button>} />}
    {operationError && !editor && !memberProjectId && <Alert type="error" showIcon closable onClose={() => setOperationError(null)} message="操作未完成" description={operationError} />}
    <Card className="registry-content-card">
      <div className="registry-table-tools"><Input prefix={<SearchOutlined />} allowClear value={search} maxLength={200} onChange={(event) => setSearch(event.target.value)} placeholder="搜索编号、名称、项目或单位" aria-label="搜索基础配置" /><Button type="primary" icon={<PlusOutlined />} disabled={!data || !!busy} onClick={() => openEditor(tab)}>新增{entityLabels[tab]}</Button></div>
      <Tabs activeKey={tab} onChange={(key) => { setTab(key as MainEntity); setSearch('') }} items={[
        { key: 'projects', label: '项目', children: <Table {...tableProps} columns={projectColumns} dataSource={projects.filter((row) => matches([row.code, row.name, row.owner]))} rowKey="project_id" /> },
        { key: 'rooms', label: '评标室', children: <Table {...tableProps} columns={roomColumns} dataSource={rooms.filter((row) => matches([row.code, row.name, row.location, projectLabel(row.project_id)]))} rowKey="room_id" /> },
        { key: 'seats', label: '工位', children: <Table {...tableProps} columns={seatColumns} dataSource={seats.filter((row) => matches([row.code, roomLabel(row.room_id), personLabel(row.person_id)]))} rowKey="seat_id" /> },
        { key: 'persons', label: '人员', children: <Table {...tableProps} columns={personColumns} dataSource={persons.filter((row) => matches([row.code, row.name, row.unit, roleLabels[row.role]]))} rowKey="person_id" /> },
      ]} />
    </Card>

    <Modal title={editor ? `${editor.row ? '编辑' : '新增'}${entityLabels[editor.entity]}` : ''} open={!!editor} width={620} footer={null} closable={!busy} maskClosable={!busy} keyboard={!busy} onCancel={() => { if (!mutationLock.current) setEditor(null) }}>
      {operationError && <Alert className="registry-modal-alert" type="error" showIcon message="保存失败" description={operationError} />}
      <Form form={form} layout="vertical" initialValues={{ status: 'draft', mode: 'centralized', role: 'expert', enabled: true }} onFinish={saveEditor} disabled={!!busy} className="registry-form" preserve={false} onValuesChange={(changed) => { if ('room_id' in changed) form.setFieldValue('person_id', undefined) }}>
        <Form.Item name="code" label={`${editor ? entityLabels[editor.entity] : ''}编号`} rules={requiredText}><Input maxLength={64} placeholder="输入业务编号" /></Form.Item>
        {editor?.entity !== 'seats' && <Form.Item name="name" label={editor?.entity === 'persons' ? '姓名' : '名称'} rules={requiredText}><Input maxLength={128} /></Form.Item>}
        {editor?.entity === 'projects' && <><Form.Item name="owner" label="业主 / 负责人" rules={requiredText}><Input maxLength={128} /></Form.Item><div className="registry-form-grid"><Form.Item name="start_at" label="开始时间（可选）"><Input type="datetime-local" /></Form.Item><Form.Item name="end_at" label="结束时间（可选）" dependencies={['start_at']} rules={[({ getFieldValue }) => ({ validator(_, value: string | undefined) { const start = getFieldValue('start_at') as string | undefined; return !value || !start || new Date(value).getTime() >= new Date(start).getTime() ? Promise.resolve() : Promise.reject(new Error('结束时间不能早于开始时间')) } })]}><Input type="datetime-local" /></Form.Item></div><Form.Item name="status" label="项目状态" rules={[{ required: true, message: '请选择项目状态' }]}><Select options={Object.entries(statusLabels).map(([value, label]) => ({ value, label }))} /></Form.Item></>}
        {editor?.entity === 'rooms' && <><Form.Item name="project_id" label="所属项目" rules={[{ required: true, message: '请选择项目' }]}><Select showSearch optionFilterProp="label" options={projects.map((project) => ({ value: project.project_id, label: projectLabel(project.project_id) }))} placeholder="选择项目" /></Form.Item><Form.Item name="location" label="位置" rules={requiredText}><Input maxLength={256} /></Form.Item><Form.Item name="mode" label="评标模式" rules={[{ required: true, message: '请选择评标模式' }]}><Select options={[{ value: 'centralized', label: '集中评标' }, { value: 'remote', label: '异地评标' }]} /></Form.Item></>}
        {editor?.entity === 'persons' && <><Form.Item name="unit" label="单位" rules={requiredText}><Input maxLength={128} /></Form.Item><Form.Item name="role" label="默认角色" rules={[{ required: true, message: '请选择角色' }]}><Select options={roleOptions} /></Form.Item><p className="registry-help">项目内的实际参加角色在项目“关联人员”中单独维护。</p></>}
        {editor?.entity === 'seats' && <><Form.Item name="room_id" label="所属评标室" rules={[{ required: true, message: '请选择评标室' }]}><Select showSearch optionFilterProp="label" options={rooms.map((room) => ({ value: room.room_id, label: `${roomLabel(room.room_id)}${room.enabled ? '' : ' · 已停用'}`, disabled: !room.enabled && room.room_id !== (editor.row as RegistrySeat | null)?.room_id }))} placeholder="选择评标室" /></Form.Item><Form.Item label="所属项目"><Input readOnly value={selectedRoom ? projectLabel(selectedRoom.project_id) : '请先选择评标室'} /></Form.Item><Form.Item name="person_id" label="分配人员（可选）"><Select allowClear showSearch optionFilterProp="label" disabled={!selectedRoom || !!busy} options={seatPersonOptions} placeholder="不选择人员表示未分配" notFoundContent="该项目暂无可分配的启用成员" /></Form.Item><p className="registry-help">仅可选择该项目已关联且启用的人员。同一人在同一项目只能占用一个工位；清空选项可取消分配。</p></>}
        {editor?.entity !== 'projects' && <Form.Item name="enabled" label="启用状态" valuePropName="checked"><Switch checkedChildren="启用" unCheckedChildren="停用" /></Form.Item>}
        <div className="registry-form-actions"><Button disabled={!!busy} onClick={() => setEditor(null)}>取消</Button><Button type="primary" htmlType="submit" loading={busy === 'editor'} disabled={!!busy && busy !== 'editor'}>保存</Button></div>
      </Form>
    </Modal>

    <Modal title={<span><TeamOutlined /> 关联人员 · {memberProject?.name ?? '项目'}</span>} open={!!memberProjectId} width={850} footer={null} closable={!busy} maskClosable={!busy} keyboard={!busy} onCancel={() => { if (!mutationLock.current) { setMemberProjectId(null); setEditingMember(null) } }}>
      <p className="registry-help">{memberProjectId && projectLabel(memberProjectId)} · 关联关系与角色保存后用于该项目的工位分配。</p>
      {operationError && <Alert className="registry-modal-alert" type="error" showIcon message="关联人员操作失败" description={operationError} />}
      <Table columns={memberColumns} dataSource={projectMembers} rowKey="membership_id" size="small" loading={loading} scroll={{ x: 680 }} pagination={{ pageSize: 5, showSizeChanger: false, hideOnSinglePage: true }} locale={{ emptyText: '该项目暂未关联人员' }} />
      <div className="registry-member-editor"><h3>{editingMember ? '编辑参加人员' : '新增参加人员'}</h3><Form form={memberForm} layout="vertical" initialValues={{ role: 'expert' }} disabled={!!busy} onFinish={async (values) => {
        if (!memberProjectId) return
        const input: ParticipantInput = { project_id: memberProjectId, person_id: values.person_id, role: values.role }
        await mutate('membership', () => editingMember ? updateRegistryItem('participants', editingMember.membership_id, input) : createRegistryItem('participants', input), () => { setEditingMember(null); memberForm.resetFields() })
      }}><div className="registry-member-fields"><Form.Item name="person_id" label="参加人员" rules={[{ required: true, message: '请选择参加人员' }]}><Select showSearch optionFilterProp="label" options={persons.map((person) => ({ value: person.person_id, label: `${personLabel(person.person_id)} · ${person.unit || '未填单位'}`, disabled: memberIds.has(person.person_id) && person.person_id !== editingMember?.person_id }))} placeholder="选择人员" /></Form.Item><Form.Item name="role" label="项目内角色" rules={[{ required: true, message: '请选择项目内角色' }]}><Select options={roleOptions} /></Form.Item></div><div className="registry-form-actions">{editingMember && <Button onClick={() => { setEditingMember(null); memberForm.resetFields() }}>取消编辑</Button>}<Button type="primary" htmlType="submit" loading={busy === 'membership'} disabled={!persons.length || (!!busy && busy !== 'membership')}>{editingMember ? '保存修改' : '添加关联'}</Button></div></Form></div>
      {!persons.length && <Alert type="info" showIcon message="请先在“人员”标签中录入人员，再关联到项目。" />}
    </Modal>
    <FaceMatchModal person={persons.find((item) => item.person_id === facePersonId) ?? null} onClose={() => setFacePersonId(null)} />
    <PersonVerificationAdminModal person={persons.find((item) => item.person_id === verificationPersonId) ?? null} projects={projects} rooms={rooms} participants={participants} onClose={() => setVerificationPersonId(null)} />
  </div>
}
