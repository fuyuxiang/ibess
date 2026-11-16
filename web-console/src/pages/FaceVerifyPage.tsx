import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Empty, Form, InputNumber, Select, Space, Switch, Table, Tabs, Tag, Upload, message } from 'antd'
import type { UploadFile } from 'antd'
import { CameraOutlined, IdcardOutlined, UploadOutlined } from '@ant-design/icons'
import { fetchFacePhoto, verifyFace } from '../api/faces'
import type { FaceVerification } from '../api/faces'
import { fetchRegistry } from '../api/registry'
import type { RegistryPerson } from '../api/registry'
import { fetchVerificationResults } from '../api/participantVerification'
import type { VerificationResult } from '../api/participantVerification'
import FaceMatchModal from '../components/face/FaceMatchModal'
import { captureFace, fetchCaptureCameras, fetchFaceVerifyConfig, updateFaceVerifyConfig } from '../api/faceVerify'
import type { CaptureCamera, CaptureResult, FaceVerifyConfig } from '../api/faceVerify'
import { formatDateTime } from '../utils/ui'
import '../face-match.css'

function conclusion(similarity: number, config: FaceVerifyConfig | null) {
  const same = config?.threshold_same ?? 0.363
  const suspect = config?.threshold_suspect ?? 0.28
  if (similarity >= same) return { label: '本人', color: 'green' as const }
  if (similarity >= suspect) return { label: '疑似', color: 'orange' as const }
  return { label: '不匹配', color: 'red' as const }
}

function Workbench() {
  const [persons, setPersons] = useState<RegistryPerson[]>([])
  const [config, setConfig] = useState<FaceVerifyConfig | null>(null)
  const [personId, setPersonId] = useState<string>()
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState('')
  const [photo, setPhoto] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<FaceVerification | null>(null)
  const [cameras, setCameras] = useState<CaptureCamera[]>([])
  const [camera, setCamera] = useState('cam10')
  const [capturing, setCapturing] = useState(false)
  const [capture, setCapture] = useState<CaptureResult | null>(null)
  const [captureError, setCaptureError] = useState('')

  useEffect(() => {
    void fetchRegistry().then((snapshot) => setPersons(snapshot.persons)).catch(() => setError('人员列表加载失败'))
    void fetchFaceVerifyConfig().then(setConfig).catch(() => setError('核验规则加载失败'))
    void fetchCaptureCameras()
      .then((data) => { setCameras(data.items); if (data.default) setCamera((current) => current || data.default) })
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    if (!personId) { setPhoto(''); return }
    let alive = true
    void fetchFacePhoto(personId).then((blob) => { if (alive) setPhoto(URL.createObjectURL(blob)) }).catch(() => setPhoto(''))
    return () => { alive = false }
  }, [personId])

  const pick = (next: UploadFile[]) => {
    const raw = next[0]?.originFileObj
    if (!raw) { setFile(null); setPreview(''); return }
    setFile(raw); setPreview(URL.createObjectURL(raw)); setResult(null)
  }

  const run = async () => {
    if (!personId) { setError('请选择登记人员'); return }
    if (!file) { setError('请上传现场抓拍图'); return }
    setBusy(true); setError(''); setResult(null)
    try {
      setResult(await verifyFace(personId, file))
      void message.success('比对完成，已写入核验记录')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '比对失败')
    } finally { setBusy(false) }
  }

  const runCapture = async () => {
    setCapturing(true); setCaptureError(''); setCapture(null)
    try {
      const data = await captureFace(camera)
      setCapture(data)
      if (data.recorded) void message.success('抓拍命中，已写入核验记录')
    } catch (cause) {
      setCaptureError(cause instanceof Error ? cause.message : '抓拍失败，请重试')
    } finally {
      setCapturing(false)
    }
  }

  const shown = result ? conclusion(result.similarity, config) : null

  return <div className="face-verify-workbench">
    <Card
      className="facts-card"
      title="自动抓拍（现场摄像头）"
      extra={<Tag color={config?.auto_enabled ? 'green' : 'default'}>{config?.auto_enabled ? `自动比对中 · 每 ${config?.interval_minutes} 分钟` : '自动比对未开启（去「规则配置」开启）'}</Tag>}
    >
      <Space wrap>
        <Select style={{ width: 240 }} value={camera} onChange={setCamera} options={cameras.map((item) => ({ value: item.key, label: item.label }))} placeholder="选择摄像头" />
        <Button type="primary" icon={<CameraOutlined />} loading={capturing} onClick={() => void runCapture()}>立即抓拍比对</Button>
        <span className="wf-muted" style={{ fontSize: 12 }}>抓一帧 → 裁剪最大人脸 → 比对人脸库；命中（本人/疑似）即写入核验记录</span>
      </Space>
      {captureError && <Alert type="error" showIcon message={captureError} style={{ marginTop: 12 }} />}
      {capture && (
        <div style={{ display: 'grid', gridTemplateColumns: '260px minmax(0, 1fr)', gap: 16, marginTop: 14 }}>
          <div>
            {capture.image
              ? <img src={capture.image} alt="抓拍画面" style={{ width: '100%', borderRadius: 8, border: '1px solid #e6ecf3' }} />
              : <div className="face-verify-placeholder">无画面</div>}
            <p className="wf-muted" style={{ fontSize: 12, margin: '6px 0 0' }}>来源：{cameras.find((item) => item.key === capture.camera)?.label || capture.camera}</p>
          </div>
          <div>
            {capture.face_found && capture.status ? <>
              <Space size={10} wrap>
                <Tag color={capture.status === 'matched' ? 'green' : capture.status === 'suspect' ? 'orange' : 'red'}>{capture.conclusion}</Tag>
                <span style={{ fontSize: 22, fontWeight: 600 }}>{((capture.similarity ?? 0) * 100).toFixed(2)}%</span>
                <span className="wf-muted">最高相似：{capture.person_name}</span>
              </Space>
              <p className="wf-muted" style={{ fontSize: 12, marginTop: 10 }}>
                判定阈值：本人 ≥ {(capture.threshold ?? 0.363).toFixed(3)}、疑似 ≥ {(capture.threshold_suspect ?? 0.28).toFixed(3)}
                {capture.recorded ? ' · 已写入核验记录' : ' · 未达疑似阈值，不写记录'}
              </p>
              {(capture.candidates?.length ?? 0) > 1 && (
                <p className="wf-muted" style={{ fontSize: 12 }}>候选：{capture.candidates?.slice(0, 3).map((item) => `${item.person_name} ${(item.similarity * 100).toFixed(1)}%`).join(' · ')}</p>
              )}
            </> : <Alert type="warning" showIcon message={capture.message} />}
          </div>
        </div>
      )}
    </Card>
    <Card className="facts-card" title="抓拍比对" extra={<Tag color="blue">抓拍图 vs 登记照</Tag>}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        <div>
          <p className="wf-muted" style={{ marginTop: 0 }}>现场抓拍（支持事件截图或摄像头抓帧）</p>
          <Upload accept="image/*" maxCount={1} showUploadList={false} beforeUpload={() => false} fileList={[]} onChange={({ fileList }) => pick(fileList)}>
            <Button icon={<UploadOutlined />}>选择抓拍图片</Button>
          </Upload>
          {preview ? <img src={preview} alt="抓拍" style={{ width: '100%', marginTop: 10, borderRadius: 8, border: '1px solid #e6ecf3' }} /> : <div className="face-verify-placeholder">尚未选择抓拍图</div>}
        </div>
        <div>
          <p className="wf-muted" style={{ marginTop: 0 }}>登记人员（基础管理 · 人员照片）</p>
          <Select showSearch optionFilterProp="label" style={{ width: '100%' }} placeholder="选择登记人员" value={personId} onChange={setPersonId}
            options={persons.map((p) => ({ value: p.person_id, label: `${p.name}${p.role ? `（${p.role}）` : ''}` }))} />
          {photo ? <img src={photo} alt="登记照" style={{ width: '100%', marginTop: 10, borderRadius: 8, border: '1px solid #e6ecf3' }} />
            : <div className="face-verify-placeholder">{personId ? '该人员暂无登记照——请到「登记照片」Tab 上传' : '选择人员后显示登记照'}</div>}
        </div>
      </div>
      {error && <Alert type="error" showIcon message={error} style={{ margin: '12px 0' }} />}
      <div style={{ marginTop: 14, display: 'flex', gap: 12, alignItems: 'center' }}>
        <Button type="primary" loading={busy} onClick={() => void run()}>开始比对</Button>
        <span className="wf-muted" style={{ fontSize: 12 }}>判定阈值：本人 ≥ {(config?.threshold_same ?? 0.363).toFixed(3)}、疑似 ≥ {(config?.threshold_suspect ?? 0.28).toFixed(3)}（余弦相似度，可在「规则配置」调整）</span>
      </div>
      {result && shown && <Card size="small" style={{ marginTop: 14 }} title={<Space>比对结果<Tag color={shown.color}>{shown.label}</Tag></Space>} extra={<span className="wf-muted">{result.model} · {formatDateTime(result.checked_at)}</span>}>
        <div style={{ display: 'flex', gap: 24, alignItems: 'baseline' }}>
          <div><span className="wf-muted">相似度</span><div style={{ fontSize: 32, fontWeight: 600, color: shown.color === 'green' ? '#1a7f37' : shown.color === 'orange' ? '#b26a00' : '#cf1322' }}>{(result.similarity * 100).toFixed(2)}%</div></div>
          <div className="wf-muted" style={{ fontSize: 12 }}>人员：{result.person_name}<br />判定阈值：{(config?.threshold_same ?? 0.363).toFixed(3)}<br />登记版本：{result.reference_version || '—'}</div>
        </div>
      </Card>}
    </Card>
  </div>
}

function Records() {
  const [persons, setPersons] = useState<RegistryPerson[]>([])
  const [personId, setPersonId] = useState<string>()
  const [items, setItems] = useState<VerificationResult[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => { void fetchRegistry().then((s) => setPersons(s.persons)).catch(() => setError('人员列表加载失败')) }, [])
  const load = useCallback(async (id: string) => {
    setLoading(true); setError('')
    try { setItems((await fetchVerificationResults(id)).items) } catch (cause) { setError(cause instanceof Error ? cause.message : '核验记录加载失败') } finally { setLoading(false) }
  }, [])
  useEffect(() => { if (personId) void load(personId) }, [personId, load])

  return <Card className="facts-card" title="核验记录" extra={<Select showSearch optionFilterProp="label" style={{ width: 240 }} placeholder="选择人员查看核验历史" value={personId} onChange={setPersonId}
    options={persons.map((p) => ({ value: p.person_id, label: p.name }))} />}>
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
    {personId ? <Table rowKey={(row) => row.result_id || `${row.checked_at}-${row.similarity}`} size="small" loading={loading} dataSource={items}
      columns={[
        { title: '核验时间', dataIndex: 'checked_at', width: 170, render: (value: string) => formatDateTime(value) },
        { title: '相似度', dataIndex: 'similarity', width: 110, render: (value: number) => (value * 100).toFixed(2) + '%' },
        { title: '结论', dataIndex: 'status', width: 110, render: (value: string) => <Tag color={value === 'matched' ? 'green' : value === 'suspect' ? 'orange' : value === 'unmatched' ? 'red' : 'default'}>{value === 'matched' ? '本人' : value === 'suspect' ? '疑似' : value === 'unmatched' ? '不匹配' : '无效'}</Tag> },
        { title: '来源', dataIndex: 'room_name', render: (value: string | null) => value || '工作台手动比对' },
        { title: '模型', dataIndex: 'model', width: 160 },
      ]}
      locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该人员暂无核验记录" /> }} pagination={{ pageSize: 8, showSizeChanger: false }} />
      : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择人员后查看其核验记录（工作台比对与自动比对都会留痕）" />}
  </Card>
}

function Config() {
  const [form] = Form.useForm<FaceVerifyConfig>()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    setLoading(true); setError('')
    try { form.setFieldsValue(await fetchFaceVerifyConfig()) } catch (cause) { setError(cause instanceof Error ? cause.message : '规则加载失败') } finally { setLoading(false) }
  }, [form])
  useEffect(() => { void load() }, [load])

  const save = async () => {
    try {
      const value = await form.validateFields()
      setSaving(true); setError(''); setNotice('')
      const saved = await updateFaceVerifyConfig(value)
      form.setFieldsValue(saved)
      setNotice('规则已保存，自动比对与工作台将使用新阈值')
      void message.success('规则已保存')
    } catch (cause) {
      if (cause instanceof Error) setError(cause.message)
    } finally { setSaving(false) }
  }

  return <Card className="facts-card" title="规则配置" loading={loading} extra={<Tag color="blue">管理侧只配置，不在此比对</Tag>}>
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
    {notice && <Alert type="success" showIcon message={notice} style={{ marginBottom: 12 }} />}
    <Form form={form} layout="vertical" style={{ maxWidth: 560 }}>
      <Form.Item label="判定本人阈值" name="threshold_same" rules={[{ required: true }]} extra="余弦相似度达到该值判定为本人（模型校准值 0.363，建议沿用）">
        <InputNumber min={0.1} max={1} step={0.005} style={{ width: 200 }} />
      </Form.Item>
      <Form.Item label="疑似阈值" name="threshold_suspect" rules={[{ required: true }]} extra="介于两个阈值之间判定为疑似，需人工复核（建议 0.280）">
        <InputNumber min={0} max={1} step={0.005} style={{ width: 200 }} />
      </Form.Item>
      <Form.Item label="自动比对" name="auto_enabled" valuePropName="checked" extra="开启后由后台按周期从入口摄像头抓帧自动比对并留痕">
        <Switch checkedChildren="启用" unCheckedChildren="停用" />
      </Form.Item>
      <Form.Item label="自动比对周期（分钟）" name="interval_minutes" rules={[{ required: true }]} extra="默认 10 分钟；周期越短占用摄像头与算力越多">
        <InputNumber min={1} max={60} step={1} style={{ width: 200 }} />
      </Form.Item>
      <Button type="primary" loading={saving} onClick={() => void save()}>保存规则</Button>
    </Form>
  </Card>
}

function RegistryTab() {
  const [persons, setPersons] = useState<RegistryPerson[]>([])
  const [active, setActive] = useState<RegistryPerson | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    void fetchRegistry()
      .then((snapshot) => { if (alive) setPersons(snapshot.persons) })
      .catch((cause) => { if (alive) setError(cause instanceof Error ? cause.message : '人员列表加载失败') })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [])

  return <>
    <Alert
      type="info"
      showIcon
      style={{ marginBottom: 12 }}
      message="登记要求：正面免冠、人脸清晰（建议人脸高度 ≥ 200px）、单人照片；支持上传图片或调用摄像头现场采集。登记后该人员才可用于工作台比对与自动比对。"
    />
    <Card className="facts-card" title="人员登记照" extra={<Tag color="blue">{persons.length} 人</Tag>} loading={loading}>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      <Table
        rowKey={(row) => row.person_id}
        size="small"
        dataSource={persons}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        columns={[
          { title: '姓名', dataIndex: 'name', width: 140 },
          { title: '默认角色', dataIndex: 'role', width: 120, render: (value: string) => value || '—' },
          { title: '单位', dataIndex: 'unit', width: 240, render: (value: string) => value || '未填写' },
          {
            title: '操作', width: 140, render: (_: unknown, row: RegistryPerson) => (
              <Button size="small" onClick={() => setActive(row)}>登记 / 更新照片</Button>
            ),
          },
        ]}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无人员，请先到基础管理新增人员" /> }}
      />
    </Card>
    <FaceMatchModal person={active} onClose={() => setActive(null)} />
  </>
}

export default function FaceVerifyPage() {
  return <div className="workflow-demo workflow-page desktop-workbench">
    <div className="monitor-reference-toolbar" style={{ minHeight: 66, height: 'auto', padding: '12px 14px' }}>
      <div className="monitor-page-title"><IdcardOutlined /><h1>人脸核验</h1><Tag color="blue">配置 · 比对 · 留痕</Tag></div>
    </div>
    <Tabs defaultActiveKey="workbench" items={[
      { key: 'workbench', label: '核验工作台', children: <Workbench /> },
      { key: 'records', label: '核验记录', children: <Records /> },
      { key: 'registry', label: '登记照片', children: <RegistryTab /> },
      { key: 'config', label: '规则配置', children: <Config /> },
    ]} />
  </div>
}
