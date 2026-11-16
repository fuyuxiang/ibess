import { useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { Link } from "react-router-dom";
import { supervisionRequest } from "../api/supervision";
import { PageHeading, RequestError } from "./Supervision";
import { riskColor, riskText } from "../utils/ui";
import { useRefreshingQuery } from "../components/workflow/useRefreshingQuery";

interface Operator {
  updated_at: string | null;
  operator_id: string;
  code: string;
  name: string;
  description: string | null;
  enabled: boolean;
  version: string;
  output_event_type: string;
  risk_level: string;
  parameters: Record<string, number>;
  supported: boolean;
  application_status: string;
  application_error?: string | null;
  recognition_sources?: {source_id:string;session_id:string;status:string}[];
  parameter_schema: {key:string;label:string;type:string;min:number;max:number;default:number}[];
}
interface Binding {
  binding_id: string;
  session_id: string | null;
  camera_id: string | null;
  zone_id: string | null;
  parameters: Record<string, unknown>;
}
export default function SupervisionRules() {
  const query = useRefreshingQuery<Operator[]>('operators',signal=>supervisionRequest('/operators',{signal}));
  const items = query.data || [];
  const loading = query.loading;
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<Operator | null>(null);
  const [bindings, setBindings] = useState<Binding[]>([]);
  const [audit, setAudit] = useState<
    { id: number; actor: string; action: string; at: string }[]
  >([]);
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const editGeneration = useRef(0);
  const [api, holder] = message.useMessage();
  const [form] = Form.useForm<{
    name: string;
    version: string;
    risk_level: string;
    description: string;
    parameters: Record<string,number>;
  }>();
  async function toggle(item: Operator) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      await supervisionRequest(
        `/operators/${item.operator_id}/${item.enabled ? "disable" : "enable"}${item.updated_at ? `?expected_updated_at=${encodeURIComponent(item.updated_at)}` : ""}`,
        { method: "POST" },
      );
      api.success("规则开关已保存");
      query.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }
  async function edit(item: Operator) {
    const generation = ++editGeneration.current;
    setEditing(item);
    setBindings([]);
    setAudit([]);
    setError("");
    form.setFieldsValue({
      ...item,
      description: item.description || "",
      parameters: {...Object.fromEntries(item.parameter_schema.map(p=>[p.key,p.default])),...item.parameters},
    });
    try {
      const [r, history] = await Promise.all([
        supervisionRequest<Binding[]>(
          `/operators/${item.operator_id}/bindings`,
        ),
        supervisionRequest<
          { id: number; actor: string; action: string; at: string }[]
        >(`/operators/${item.operator_id}/audit`),
      ]);
      if (generation!==editGeneration.current) return;
      setBindings(r);
      setAudit(history);
    } catch (e) {
      if (generation===editGeneration.current) setError(e instanceof Error ? e.message : "作用域加载失败");
    }
  }
  async function save() {
    if (!editing || lock.current) return;
    try {
      const values = await form.validateFields();
      const parameters = values.parameters || {};
      lock.current = true;
      setBusy(true);
      await supervisionRequest(`/operators/${editing.operator_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...values,
          parameters,
          expected_updated_at: editing.updated_at,
        }),
      });
      api.success("规则配置已保存");
      setEditing(null);
      query.reload();
    } catch (e) {
      if (e instanceof Error) setError(e.message);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }
  return (
    <div className="sv-page">
      {holder}
      <PageHeading
        title="规则管理"
        description="管理实际部署的异常行为规则、风险等级与参数，场景生效范围由场次配置控制。"
        extra={
          <>
            <Link to="/stages/console">
              <Button>场次与场景配置</Button>
            </Link>
            <Link to="/facts">
              <Button>业务许可与例外</Button>
            </Link>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => query.reload()}
              loading={loading}
            >
              刷新
            </Button>
          </>
        }
      />
      <RequestError error={error || query.error} reload={query.reload} />
      <Alert
        type="info"
        showIcon
        message="规则输出是核查线索，最终结论由监管人员确认。"
        description="规则开关与场次启用场景共同决定运行范围；参数修改后请核对消费者应用状态与实际输出。"
      />
      <Card title="异常分析规则">
        <Table
          rowKey="operator_id"
          dataSource={items}
          loading={loading}
          scroll={{ x: 1050 }}
          columns={[
            {
              title: "规则名称",
              dataIndex: "name",
              render: (v: string, o: Operator) => (
                <div className="sv-event-cell">
                  <strong>{v}</strong>
                  <small>{o.code}</small>
                </div>
              ),
            },
            { title: "版本", dataIndex: "version", width: 100 },
            {
              title: "风险等级",
              dataIndex: "risk_level",
              width: 100,
              render: (s: string) => (
                <Tag color={riskColor[s]}>{riskText[s] || s}</Tag>
              ),
            },
            { title: "规则说明", dataIndex: "description", ellipsis: true },
            { title:'消费者应用',key:'application',width:160,render:(_:unknown,o:Operator)=><Tag color={o.application_status==='applied' ? 'green' : o.application_status==='error' ? 'red' : 'gold'} title={o.application_error || undefined}>{({applied:'配置已读取',pending:'等待应用',unknown:'回执待核验',error:'应用失败',unsupported:'当前未部署'} as Record<string,string>)[o.application_status] || '待核验'}</Tag> },
            {title:'识别来源',key:'recognition',width:240,render:(_:unknown,o:Operator)=>o.recognition_sources===undefined ? '—' : o.recognition_sources.length===0 ? <Tag color="gold">等待摄像头回执</Tag> : <Space direction="vertical">{o.recognition_sources.map(s=><Tag key={`${s.session_id}:${s.source_id}`} color={s.status==='ready'?'green':'gold'}>{s.source_id} · {({ready:'识别就绪',camera_degraded:'画面异常暂停识别',model_not_configured:'视觉模型未配置',binding_missing:'未绑定场次',zones_or_binding_missing:'区域或绑定缺失',entry_line_or_binding_missing:'入口线或绑定缺失',entry_line_missing:'入口线未配置',face_or_authorization_unverified:'人脸或授权待核验',model_or_database_unavailable:'人脸模型或数据库不可用',review_failed:'画面复核失败'} as Record<string,string>)[s.status]||'待核验'}</Tag>)}</Space>},
            {
              title: "配置状态",
              dataIndex: "enabled",
              width: 110,
              render: (v: boolean) => (
                <Tag color={v ? "green" : "default"}>
                  {v ? "配置启用" : "配置停用"}
                </Tag>
              ),
            },
            {
              title: "操作",
              key: "actions",
              width: 180,
              render: (_, o) => (
                <Space>
                  <Button
                    type="link"
                    onClick={() => void edit(o)}
                    disabled={busy}
                  >
                    配置
                  </Button>
                  <Popconfirm
                    title={`确认${o.enabled ? "停用" : "启用"}规则“${o.name}”？`}
                    onConfirm={() => void toggle(o)}
                    okText="确认"
                    cancelText="取消"
                  >
                    <Button type="link" danger={o.enabled} disabled={busy || (!o.supported && !o.enabled)}>
                      {o.enabled ? "停用" : "启用"}
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
          pagination={{ pageSize: 20, showSizeChanger: false }}
        />
      </Card>
      <Modal
        title={`配置规则 · ${editing?.name || ""}`}
        open={Boolean(editing)}
        onCancel={() => {
          if (!busy) { editGeneration.current++; setEditing(null); }
        }}
        onOk={() => void save()}
        confirmLoading={busy}
        okText="保存配置"
        okButtonProps={{disabled:!editing?.supported}}
        cancelText="取消"
        maskClosable={false}
        width={680}
      >
        {error && <Alert type="error" message={error} />}
        <Form form={form} layout="vertical" disabled={busy || !editing?.supported}>
          <Form.Item
            name="name"
            label="规则名称"
            rules={[{ required: true, whitespace: true }]}
          >
            <Input maxLength={128} />
          </Form.Item>
          <Space align="start">
            <Form.Item
              name="version"
              label="版本标识"
              rules={[{ required: true, whitespace: true }]}
            >
              <Input maxLength={32} />
            </Form.Item>
            <Form.Item
              name="risk_level"
              label="风险等级"
              rules={[{ required: true }]}
            >
              <Select
                style={{ width: 150 }}
                options={Object.entries(riskText).map(([value, label]) => ({
                  value,
                  label,
                }))}
              />
            </Form.Item>
          </Space>
          <Form.Item name="description" label="规则说明">
            <Input.TextArea rows={2} />
          </Form.Item>
          {editing?.parameter_schema.map(p=><Form.Item key={p.key} name={['parameters',p.key]} label={p.label}
            rules={[{required:true,message:`请填写${p.label}`},{type:'number',min:p.min,max:p.max,message:`有效范围 ${p.min}–${p.max}`}]}>
            <InputNumber min={p.min} max={p.max} precision={p.type==='integer' ? 0 : 2} style={{width:'100%'}}/>
          </Form.Item>)}
          {editing?.supported && !editing.parameter_schema.length && <p className="sv-muted">该规则当前没有可调参数。</p>}
          {editing && !editing.supported && <Alert type="warning" message="当前检测服务未部署该规则，不能启用或修改执行参数。"/>}
        </Form>
        <h4>当前作用域绑定</h4>
        {bindings.length ? (
          bindings.map((b) => (
            <p key={b.binding_id}>
              场次 {b.session_id || "全部"} · 采集源 {b.camera_id || "全部"} ·
              区域 {b.zone_id || "全部"}
            </p>
          ))
        ) : (
          <p className="sv-muted">
            未配置显式绑定。请结合场次启用场景核对实际运行范围。
          </p>
        )}
        <h4>最近配置留痕</h4>
        {audit.length ? (
          audit.map((a) => (
            <p key={a.id} className="sv-muted">
              {new Date(a.at).toLocaleString("zh-CN", {
                timeZone: "Asia/Shanghai",
                hour12: false,
              })}{" "}
              · {a.actor} ·{" "}
              {{
                configure: "修改配置",
                enable: "启用规则",
                disable: "停用规则",
                bind: "新增作用域",
                unbind: "移除作用域",
              }[a.action] || a.action}
            </p>
          ))
        ) : (
          <p className="sv-muted">暂无配置变更记录。</p>
        )}
      </Modal>
    </div>
  );
}
