import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Form, InputNumber, Switch, Tag, message } from 'antd'
import { fetchFaceVerifyConfig, updateFaceVerifyConfig, type FaceVerifyConfig } from '../../api/faceVerify'

export default function FaceSettings() {
  const [form] = Form.useForm<FaceVerifyConfig>()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    setLoading(true); setError('')
    try { form.setFieldsValue(await fetchFaceVerifyConfig()) } catch (cause) { setError(cause instanceof Error ? cause.message : "参数读取失败，请重试") } finally { setLoading(false) }
  }, [form])
  useEffect(() => { void load() }, [load])

  const save = async () => {
    try {
      const value = await form.validateFields()
      setSaving(true); setError(''); setNotice('')
      const saved = await updateFaceVerifyConfig(value)
      form.setFieldsValue(saved)
      setNotice("参数已保存，后续核验使用新设置")
      void message.success("参数已保存")
    } catch (cause) {
      if (cause instanceof Error) setError(cause.message)
    } finally { setSaving(false) }
  }

  return <Card className="facts-card" title="身份核验参数" loading={loading} extra={<Tag color="blue">全局设置</Tag>}>
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
    {notice && <Alert type="success" showIcon message={notice} style={{ marginBottom: 12 }} />}
    <Form form={form} layout="vertical" style={{ maxWidth: 560 }}>
      <Form.Item label="匹配通过阈值" name="threshold_same" rules={[{ required: true }]} extra="分数达到此值时匹配通过，请结合现场样本校准">
        <InputNumber min={0.1} max={1} step={0.005} style={{ width: 200 }} />
      </Form.Item>
      <Form.Item label="疑似匹配阈值" name="threshold_suspect" rules={[{ required: true }]} extra="分数位于两个阈值之间时标记为疑似，需人工复核">
        <InputNumber min={0} max={1} step={0.005} style={{ width: 200 }} />
      </Form.Item>
      <Form.Item label="自动比对" name="auto_enabled" valuePropName="checked" extra="仅对已启用身份核查的场次生效，定时取入口画面比对并记录结果">
        <Switch checkedChildren="启用" unCheckedChildren="停用" />
      </Form.Item>
      <Form.Item label="自动比对周期（分钟）" name="interval_minutes" rules={[{ required: true }]} extra="默认 10 分钟，缩短周期会增加资源占用">
        <InputNumber min={1} max={60} step={1} style={{ width: 200 }} />
      </Form.Item>
      <Button type="primary" loading={saving} onClick={() => void save()}>保存参数</Button>
    </Form>
  </Card>
}
