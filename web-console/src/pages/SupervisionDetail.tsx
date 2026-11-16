import { useEffect, useRef, useState } from "react";
import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Empty,
  Form,
  Input,
  Popconfirm,
  Select,
  Space,
  Spin,
  Steps,
  Tag,
  Timeline,
  Typography,
  message,
} from "antd";
import {
  ArrowLeftOutlined,
  CheckCircleOutlined,
  DownloadOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { Link, useLocation } from "react-router-dom";
import { safeReturnTo } from "../api/auth";
import { phaseTitle } from "../api/stages";
import type { EventDetail } from "../api/client";
import {
  downloadEvidencePackage,
  operateEvent,
  supervisionRequest,
  type SupervisionAction,
  type Operation,
} from "../api/supervision";
import EvidencePlayer from "../components/evidence/EvidencePlayer";
import { returnLabel } from "../components/layout/workspace-navigation";
import { BusinessBasis, EvidenceSupplement, TaskReceipt } from "../components/workflow/WorkspaceActions";
import { useAuthStore } from "../store/useAuthStore";
import { useRefreshingQuery } from "../components/workflow/useRefreshingQuery";
import { beijingIso } from '../api/workspace';
import { useWorkflowDetail } from "../components/workflow/useWorkflowDetail";
import {
  actionLabels,
  allowedActions,
  workflowStatus,
  workflowStep,
  workflowLabels,
  resolutionLabels,
  evidenceLabels,
  auditNote,
  factLabels,
} from "../components/workflow/supervision-model";
import { newRequestId } from "../components/workflow/workflow-model";
import { notifyWorkflowChange } from "../store/useWorkflowStore";
import { useRegistry } from "../store/useRegistryStore";
import { eventLabel, formatDateTime, riskColor, riskText } from "../utils/ui";

import { PageHeading, RequestError, WorkflowTag } from "./Supervision";

function EvidenceWorkspace({detail}:{detail:EventDetail}) {
  const [tab,setTab]=useState('video');
  const context=detail.context_evidence;
  return <div className="case-evidence-workspace">
    <EvidencePlayer detail={detail} tab={tab} setTab={setTab}/>
    <details className="sv-context-details case-manifest"><summary>证据详情与校订</summary>
      <p>采集源：{detail.camera_id || '未登记'} · 事件时刻：{formatDateTime(detail.occurred_at)}</p>
      {context?.range?.started_at&&<p>取证范围：{formatDateTime(context.range.started_at)} — {formatDateTime(context.range.ended_at)}</p>}
      {(context?.videos||[]).map(v=><div className="sv-file-row" key={v.source_id}><Tag>机位</Tag><strong>{v.source_id}</strong><span>{formatDateTime(v.started_at)} — {formatDateTime(v.ended_at)}</span><small>{v.note}</small></div>)}
      {(context?.audio||[]).map(a=><div className="sv-file-row" key={a.recording_id}><Tag>原音</Tag><strong>{a.title}</strong><small>{a.note}</small></div>)}
      {detail.evidences.map(e=><details className="sv-context-details" key={e.id}><summary>{e.evidence_type==='keyframe'?'关键帧':'已留存片段'} · {formatDateTime(e.captured_at)}</summary><p>文件引用：{e.object_key}</p><small>SHA-256：{e.sha256||'未登记'}</small></details>)}
      {context?.annotations?.map(a=><div className="sv-file-row" key={a.annotation_id}><Tag>{a.kind}</Tag><span>{a.original?.speaker || '未知说话人'}：{a.original?.text}</span><small>{a.actor} · {formatDateTime(a.at)}</small></div>)}
      {!detail.evidences.length&&!context?.videos?.length&&!context?.audio?.length&&<p className="sv-muted">暂无文件清单，请核对录像是否可播放。</p>}
    </details>
  </div>;
}

export function ActionPanel({
  detail,
  reload,
}: {
  detail: EventDetail;
  reload: () => void;
}) {
  const user=useAuthStore(s=>s.user);
  const allowed = allowedActions(detail).filter(action=>user?.role==='viewer'?false:user?.role==='operator'?action==='supplement'||(action==='feedback'&&detail.assignee===user.username&&detail.task_receipt?.received_by===user.username):true);
  const operators=useRefreshingQuery<{username:string;display_name:string}[]>('assignment-operators',signal=>supervisionRequest('/supervision/operators',{signal}));
  const [action, setAction] = useState<SupervisionAction>(
    allowed[0] || "confirm",
  );
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const [reopenRequested,setReopenRequested] = useState(false);
  useEffect(()=>{if(workflowStatus(detail)==='ARCHIVED')setReopenRequested(false);},[detail.workflow_status,detail.workflow_version]);
  const pending = useRef<{ signature: string; id: string } | null>(null);
  const [form] = Form.useForm<{
    note: string;
    assignee: string;
    due_at: string;
    resolution: string;
    acknowledge_missing_evidence: boolean;
    evidence_note: string;
  }>();
  const [baseline, setBaseline] = useState({
    status: workflowStatus(detail),
    version: detail.workflow_version || 0,
  });
  const changed =
    baseline.status !== workflowStatus(detail) ||
    baseline.version !== (detail.workflow_version || 0);
  useEffect(() => {
    if (changed && !form.isFieldsTouched() && !busy)
      setBaseline({
        status: workflowStatus(detail),
        version: detail.workflow_version || 0,
      });
  }, [changed, detail, form, busy]);
  const registry = useRegistry();
  const invalidate = notifyWorkflowChange;
  const [api, holder] = message.useMessage();
  const activeAction = allowed.includes(action) ? action : allowed[0];
  const needsEvidenceNote = ['archive','dismiss'].includes(activeAction) && !['READY','NOT_APPLICABLE'].includes(detail.evidence_status || (detail.is_demo ? 'NOT_APPLICABLE' : 'PENDING'));
  async function submit() {
    if (locked.current || !activeAction || changed) return;
    try {
      const values = await form.validateFields();
      const payload: Omit<Operation, "request_id"> = {
        action: activeAction,
        note: values.note.trim(),
        expected_status: workflowStatus(detail),
        expected_version: detail.workflow_version || 0,
        ...(activeAction === "assign"
          ? {
              assignee: values.assignee.trim(),
              ...(values.due_at
                ? { due_at: beijingIso(values.due_at) }
                : {}),
            }
          : {}),
        ...(activeAction === "dismiss"
          ? { resolution: values.resolution }
          : {}),
        ...(needsEvidenceNote ? {acknowledge_missing_evidence:values.acknowledge_missing_evidence,evidence_note:values.evidence_note.trim()} : {}),
      };
      const signature = JSON.stringify(payload);
      if (pending.current?.signature !== signature)
        pending.current = { signature, id: newRequestId() };
      locked.current = true;
      setBusy(true);
      setError("");
      await operateEvent(detail.event_id, {
        ...payload,
        request_id: pending.current.id,
      });
      pending.current = null;
      form.resetFields();
      const nextAction:Partial<Record<SupervisionAction,SupervisionAction>>={confirm:'assign',assign:'feedback',feedback:'archive',return:'feedback',reopen:'confirm'};
      if(nextAction[activeAction])setAction(nextAction[activeAction]!);
      api.success("操作已保存");
      invalidate();
      reload();
    } catch (e) {
      if (e instanceof Error) setError(e.message);
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }
  return (
    <Card className="sv-action-card" title="核查与处置">
      {holder}
      <p>人工结论：<Tag color={detail.resolution==='confirmed' ? 'red' : detail.resolution ? 'green' : 'default'}>{resolutionLabels[detail.resolution || ''] || '尚未认定'}</Tag></p>
      {workflowStatus(detail)==='ARCHIVED' && <div className="sv-archived"><Checkmark/><h3>事件已归档</h3><p>归档时间：{formatDateTime(detail.archived_at)}</p><p>重新核查将保留原结论和归档记录。</p></div>}
      {changed && (
        <Alert
          type="warning"
          showIcon
          message="事件状态已更新"
          description="请核对最新状态后继续，已填写内容保留。"
          action={
            <Button
              size="small"
              onClick={() =>
                setBaseline({
                  status: workflowStatus(detail),
                  version: detail.workflow_version || 0,
                })
              }
            >
              已核对，继续填写
            </Button>
          }
        />
      )}
      {detail.assignee && (
        <div className="sv-assignment">
          <strong>责任人 / 岗位：{detail.assignee}</strong>
          <span>处置期限：{formatDateTime(detail.due_at)}</span>
        </div>
      )}
      <TaskReceipt detail={detail} reload={reload}/>
      {detail.feedback && (
        <Alert
          type="info"
          message="已提交处置反馈"
          description={
            <span style={{ whiteSpace: "pre-wrap" }}>{detail.feedback}</span>
          }
        />
      )}
      {detail.category === "observation" ? (
        <Alert
          type="info"
          showIcon
          message="基础观察记录"
          description="此记录仅供分析追溯，无需处置。"
        />
      ) : workflowStatus(detail)==='ARCHIVED' && !reopenRequested && allowed.includes('reopen') ? (
        <Button block onClick={()=>setReopenRequested(true)}>发起重新核查</Button>
      ) : allowed.length === 0 ? (
        <Alert type="info" showIcon message="当前为只读状态" description={user?.role==='viewer'?"当前账号可查阅，认定与处置由授权岗位办理。":user?.role==='operator'?"请先接收本人任务。认定与复核由监管人员办理。":"当前暂无可办理操作，可查阅已有记录。"}/>
      ) : (
        <Form
          form={form}
          layout="vertical"
          onFinish={() => void submit()}
          disabled={busy || changed}
          className="sv-action-form"
        >
          <Form.Item label="操作类型">
            <Select
              aria-label="操作类型"
              value={activeAction}
              onChange={(value) => {
                setAction(value);
                setError("");
              }}
              options={allowed.map((value) => ({
                value,
                label: actionLabels[value],
              }))}
            />
          </Form.Item>
          {activeAction==='reopen' && <Alert type="warning" showIcon message="重新核查后将撤回当前结论" description="请填写新证据或更正原因。原结论、处置及归档记录保留。"/>}
          {workflowStatus(detail)==='ARCHIVED' && <Button type="link" onClick={()=>{setReopenRequested(false);form.resetFields();}}>取消重新核查</Button>}
          {activeAction === "dismiss" && (
            <Form.Item
              name="resolution"
              label="排除结论"
              rules={[{ required: true, message: "请选择排除结论" }]}
            >
              <Select
                options={[
                  { value: "false_positive", label: "识别误报" },
                  { value: "authorized_exception", label: "已核实为授权例外" },
                  { value: "normal", label: "正常现场行为" },
                ]}
                placeholder="请选择并在说明中填写依据"
              />
            </Form.Item>
          )}
          {activeAction === "assign" && (
            <>
              <Form.Item
                name="assignee"
                label="责任人或岗位"
                rules={[
                  {
                    required: true,
                    whitespace: true,
                    message: "请选择或填写责任人、岗位",
                  },
                ]}
              >
                <AutoComplete
                  options={[...(operators.data||[]).map(p=>({value:p.username,label:p.display_name+'（'+p.username+'）'})),...(registry.data?.persons
                    .filter(
                      (p) =>
                        p.enabled && ["supervisor", "staff"].includes(p.role),
                    )
                    .map((p) => ({ value: `${p.name}（${p.unit}）` }))||[])]}
                  placeholder="选择或填写责任人、岗位"
                  maxLength={128}
                />
              </Form.Item>
              <Form.Item name="due_at" label="处置期限（北京时间，选填）">
                <Input type="datetime-local" aria-label="处置期限" />
              </Form.Item>
              <p className="sv-muted">
                系统账号可接收本人任务；外部岗位请线下通知并登记反馈。
              </p>
            </>
          )}
          <Form.Item
            name="note"
            label={
              activeAction === "feedback"
                ? "处置措施与结果"
                : activeAction === "archive"
                  ? "复核结论与归档说明"
                  : "核查依据 / 操作说明"
            }
            rules={[
              {
                required: true,
                whitespace: true,
                message: "请填写具体依据或处理结果",
              },
            ]}
          >
            <Input.TextArea
              rows={5}
              showCount
              maxLength={4000}
              placeholder={
                activeAction === "confirm"
                  ? "填写证据片段、观察事实及认定依据"
                  : "填写核查依据、处置措施或复核结论"
              }
            />
          </Form.Item>
          {error && (
            <Alert
              type="error"
              showIcon
              message="操作未完成"
              description={error}
              action={
                <Button size="small" onClick={reload}>
                  刷新状态
                </Button>
              }
            />
          )}
          {needsEvidenceNote && <>
            <Alert type="warning" showIcon message="证据尚未完整保全" description="可等待保全完成；按现有资料归档时，需核实缺失情况并说明影响和依据，档案将标记资料不完整。"/>
            <Form.Item name="acknowledge_missing_evidence" valuePropName="checked" rules={[{validator:(_,value)=>value ? Promise.resolve() : Promise.reject(new Error('请先核实并确认资料缺失情况'))}]}><Checkbox>已核实缺失情况，确认按现有资料归档</Checkbox></Form.Item>
            <Form.Item name="evidence_note" label="资料缺失情况与归档依据" rules={[{required:true,whitespace:true,message:'请说明缺失资料、影响及归档依据'}]}><Input.TextArea rows={3} maxLength={2000} showCount/></Form.Item>
          </>}
          {["archive", "dismiss", "reopen"].includes(activeAction) ? (
            <Popconfirm
              title={
                activeAction==='reopen' ? "确认重新核查？原记录将保留。" : activeAction === "archive"
                  ? "确认复核通过并归档？"
                  : "确认排除并归档？"
              }
              onConfirm={() => void submit()}
              okText="确认"
              cancelText="继续核查"
              disabled={busy}
            >
              <Button block type="primary" loading={busy}>
                {actionLabels[activeAction]}
              </Button>
            </Popconfirm>
          ) : (
            <Button block type="primary" htmlType="submit" loading={busy}>
              {actionLabels[activeAction]}
            </Button>
          )}
        </Form>
      )}
    </Card>
  );
}
function Checkmark() {
  return <span className="sv-checkmark">✓</span>;
}

export default function SupervisionDetail() {
  const user=useAuthStore(s=>s.user);
  const location = useLocation();
  const returnTo = safeReturnTo(
    (location.state as { returnTo?: string } | null)?.returnTo,
    "/events",
  );
  const state = useWorkflowDetail();
  const { detail, loading, error, reload } = state;
  // 音频线索：没有录像但有拾音，提示应说明"能看什么"，而不是判定为资料缺失
  const audioOnly = detail?.source_type === 'audio' && (detail?.context_evidence?.audio?.length || 0) > 0 && !detail?.context_evidence?.videos?.length && !(detail?.evidences || []).some(e => e.evidence_type === 'video')
  const [exporting, setExporting] = useState("");
  const [exportError, setExportError] = useState("");
  async function download(kind: string) {
    if (!detail || exporting) return;
    setExporting(kind);
    setExportError("");
    try {
      await downloadEvidencePackage(detail.event_id);
      reload();
    } catch (e) {
      setExportError(e instanceof Error ? e.message : "导出失败，请重试");
    } finally {
      setExporting("");
    }
  }
  if (!detail)
    return (
      <div className="sv-page">
        <PageHeading
          title="事件核查"
          description="选择告警，查看证据并办理核查处置。"
          extra={
            <Link to="/events">
              <Button>返回告警处置</Button>
            </Link>
          }
        />
        <RequestError error={error} reload={reload} />
        <Card>
          {loading ? (
            <div className="sv-loading">
              <Spin />
            </div>
          ) : (
            <Empty
              description={
                error
                  ? "事件读取失败，请核对编号后重试"
                  : "请从告警列表选择事件"
              }
            >
              <Link to="/events">
                <Button type="primary">选择事件</Button>
              </Link>
            </Empty>
          )}
        </Card>
      </div>
    );
  const missing = [
    ...new Set([
      ...(detail.clues || []).flatMap((c) => c.missing_facts || []),
      ...(detail.semantic?.result.missing_facts || []),
    ]),
  ];
  const quality = [
    ...new Set((detail.clues || []).flatMap((c) => c.quality_flags || [])),
  ];
  const step = workflowStep(detail);
  return (
    <div className="sv-page case-page">
      <PageHeading
        title={eventLabel(detail.event_type)}
        description={`${detail.project_name || "未关联项目"} / ${detail.room || detail.camera_id}`}
        extra={
          <>
            <Link to={returnTo}>
              <Button icon={<ArrowLeftOutlined />}>{returnLabel(returnTo)}</Button>
            </Link>
            <Button
              icon={<ReloadOutlined />}
              onClick={reload}
              loading={loading}
            >
              刷新
            </Button>
            {['admin','supervisor'].includes(user?.role||'')&&<Button
              icon={<DownloadOutlined />}
              onClick={() => void download("zip")}
              loading={exporting === "zip"}
              disabled={Boolean(exporting)}
            >
              导出证据包
            </Button>}
          </>
        }
      />
      <RequestError
        error={error || exportError}
        reload={() => {
          setExportError("");
          reload();
        }}
      />
      <Card className="sv-case-header">
        <div className="sv-case-meta">
          <Space wrap>
            <Tag
              color={
                detail.machine_conclusion === "insufficient"
                  ? "gold"
                  : detail.workflow_status==='ARCHIVED'?'default':riskColor[detail.risk_level]
              }
            >
              {detail.machine_conclusion === "insufficient"
                ? "风险未定"
                : "识别风险："+riskText[detail.risk_level]}
            </Tag>
            <WorkflowTag event={detail} />
            <Tag color={detail.resolution==='confirmed' ? 'red' : detail.resolution ? 'green' : 'default'}>人工结论：{resolutionLabels[detail.resolution || ''] || '尚未认定'}</Tag>
            {detail.is_demo && <Tag>模拟数据</Tag>}
            <span>发生时间：{detail.occurred_at ? formatDateTime(detail.occurred_at) : '现场时间未登记'}</span>
          </Space>
          <Typography.Text copyable className="sv-case-id">
            {detail.event_id}
          </Typography.Text>
        </div>
        {detail.behavior&&<div className="sv-behavior-strip"><strong>行为时段</strong><span>{formatDateTime(detail.behavior.started_at)} — {formatDateTime(detail.behavior.ended_at||detail.behavior.last_seen_at)}</span><Tag>{detail.behavior.active?"持续监测中":detail.behavior.ended_at?'已记录起止':"监测已中断，结束时间待核验"}</Tag><span>累计 {detail.behavior.trigger_count} 次触发 · 合并办理</span></div>}
        <Steps
          responsive={false}
          size="small"
          current={step}
          status={workflowStatus(detail) === "ARCHIVED" ? "finish" : "process"}
          items={[
            { title: "告警发现" },
            { title: "证据核查" },
            { title: workflowStatus(detail)==='ARCHIVED' && detail.resolution!=='confirmed' ? "核实排除" : "处置反馈" },
            { title: "复核归档" },
          ]}
        />
      </Card>
      {workflowStatus(detail) === "LEGACY_CLOSED" && (
        <Alert
          showIcon
          type="warning"
          message="此历史事件尚无正式归档记录，请核查后确认或排除。"
        />
      )}
      {detail.is_demo && (
        <Alert
          type="info"
          showIcon
          message="此事件为模拟数据，仅用于流程验证。"
        />
      )}
      <div className="sv-detail-grid">
        <div className="sv-evidence-stack">
          <Card
            title="证据核查"
            extra={
              <Tag>
                {detail.source_type === "audio"
                  ? "音频线索"
                  : detail.is_demo
                    ? "模拟输入"
                    : "视频线索"}
              </Tag>
            }
          >
            {detail.evidence_status==='READY'&&!detail.is_demo&&!audioOnly ? <div className="case-evidence-status"><CheckCircleOutlined/><strong>证据已保全</strong><span>{detail.media_hold ? `${detail.media_hold.files} 个片段 · 已独立保存` : "查看关联证据"}</span></div> : <Alert type={detail.is_demo || audioOnly ? 'info' : 'warning'} showIcon
              message={audioOnly && detail.evidence_status!=='READY' ? "已关联现场录音" : (evidenceLabels[detail.evidence_status || 'PENDING'] || "证据状态待核验")}
              description={audioOnly
                ? `已关联 ${detail.context_evidence?.audio?.length} 条录音及转写，暂无关联录像。`
                : detail.media_hold ? `保全清单 ${detail.media_hold.files} 个片段。${detail.media_hold.error || ''}` : undefined}
              action={!detail.is_demo && !audioOnly && user?.role!=='viewer' && Boolean(detail.context_evidence?.videos.length) && detail.evidence_status!=='READY' ? <Button size="small" onClick={()=>{void supervisionRequest(`/supervision/events/${detail.event_id}/preservation-retry`,{method:'POST'}).then(()=>{reload();notifyWorkflowChange();}).catch(e=>setExportError(e.message));}}>重试保全</Button> : undefined}/>}
            <EvidenceWorkspace key={detail.event_id} detail={detail} />
            <EvidenceSupplement detail={detail} reload={reload}/>
          </Card>
          <Card title="线索与核查依据" className="case-facts-card">
            <Descriptions
              size="small"
              column={{ xs: 1, sm: 2, md: 2, lg: 2, xl: 2, xxl: 2 }}
              items={[
                {
                  key: "time",
                  label: "发生时间",
                  children: detail.occurred_at ? formatDateTime(detail.occurred_at) : "现场时间未登记；上传时间不作为发生时间",
                },
                { key: "source", label: "采集源", children: detail.camera_id },
                {
                  key: "phase",
                  label: "评标阶段",
                  children: detail.phase_id
                    ? phaseTitle(detail.phase_id)
                    : "未记录",
                },
                {
                  key: "model",
                  label: "模型 / 版本",
                  children:
                    detail.origin==='manual'?'人工标记':[detail.model_name, detail.model_version]
                      .filter(Boolean)
                      .join(" / ") || "未记录",
                },
              ]}
            />
            <BusinessBasis detail={detail}/>
            <div className="sv-finding">
              <h3>{detail.is_demo ? "模拟数据" : detail.origin==='manual' ? "人工观察记录" : "系统识别结果"}</h3>
              <p>{detail.reason || "未记录"}</p>
              <small>
                {detail.is_demo
                  ? "此事件仅用于流程验证，不作为现场证据。"
                  : detail.origin==='manual'?"请结合原始证据、业务许可和现场情况核查。":"识别结果供参考，请结合证据和现场情况核查。"}
              </small>
            </div>
            {(detail.clues || []).map((c) => (
              <div key={c.clue_id} className="sv-clue">
                <Space wrap>
                  <Tag>{c.scene_id}</Tag>
                  <strong>{c.rule_id}</strong>
                  <Tag>版本 {c.rule_version}</Tag>
                  <Tag>
                    {c.conclusion === "suspected"
                      ? "疑似异常"
                      : c.conclusion === "insufficient"
                        ? "信息不足"
                        : "正常"}
                  </Tag>
                </Space>
                <p>{c.reason}</p>
              </div>
            ))}
            {missing.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message="待补充的核查依据"
                description={
                  <ul>
                    {missing.map((m) => (
                      <li key={m}>{factLabels[m] || m}</li>
                    ))}
                  </ul>
                }
              />
            )}
            {quality.length > 0 && (
              <Alert
                type="warning"
                message="资料质量提示"
                description={quality.join("；")}
              />
            )}
            {user?.role==='admin'&&detail.semantic?.context && (
              <details className="sv-context-details">
                <summary>技术详情</summary>
                <pre>{JSON.stringify(detail.semantic.context, null, 2)}</pre>
              </details>
            )}
          </Card>
          {(detail.archives?.length || 0)>0 && <Card title="历次归档记录">{detail.archives!.map((a,index)=><details key={a.version} className="sv-context-details"><summary>第 {index+1} 次归档（流程版本 {a.version}） · {resolutionLabels[a.resolution || ''] || '未记录结论'} · {formatDateTime(a.at)}</summary><p>{a.actor} · {a.note}</p><p>归档范围：{String(a.snapshot.project_name || '未关联项目')} / {String(a.snapshot.room || '未关联评标室')}</p><p>归档资料：{evidenceLabels[String(a.snapshot.evidence_status)] || '未记录'}</p>{a.evidence_note && <Alert type="warning" message="资料缺失说明" description={a.evidence_note}/>}</details>)}</Card>}
          <Card title="操作记录">
            <details className="sv-context-details"><summary>查看操作记录（{detail.audit.length}）</summary>
            <Timeline
              items={[
                {
                  key: "discovered",
                  color: "blue",
                  children: (
                    <div>
                      <strong>异常线索入库</strong>
                      <small className="sv-log-time">
                        {formatDateTime(detail.created_at)}
                      </small>
                    </div>
                  ),
                },
                ...detail.audit.map((a, index) => ({
                  key: `audit-${index}`,
                  color:
                    a.action === "archive" || a.action === "dismiss"
                      ? "green"
                      : "blue",
                  children: (
                    <div>
                      <strong>
                        {actionLabels[a.action as SupervisionAction] ||
                          {
                            export: "导出记录",
                            evidence_export: "导出证据包",
                            semantic_intake: "语义线索接入",
                            simulate: "生成业务模拟事件",
                            false_positive:'排除误报',escalate:'转交处理',close:'关闭事件',auto_close:'系统关闭',preservation_retry:'重新核对录像保全',
                            manual_mark:'人工标记现场事实',video_window:'关联机位 / 扩展证据时段',audio_link:'关联现场原音',task_receive:'登记任务接收',evidence_annotation:'转写勘误 / 身份核查备注',evidence_unlink:'撤销补充关联',
                          }[a.action] ||
                          a.action}
                      </strong>
                      <small className="sv-log-time">
                        {a.actor} · {formatDateTime(a.at)}
                      </small>
                      <p className="sv-log-note">{auditNote(a.note)}</p>
                    </div>
                  ),
                })),
              ]}
            />
            </details>
            {detail.audit.length === 0 &&
              detail.decisions.map((d, i) => (
                <p key={i}>
                  {formatDateTime(d.decided_at)} · {d.reviewer_id} · {d.note}
                </p>
              ))}
          </Card>
        </div>
        <ActionPanel key={detail.event_id} detail={detail} reload={reload} />
      </div>
      <div className="sv-page-foot">
        当前处理状态：{workflowLabels[workflowStatus(detail)]} · 全流程记录可追溯
      </div>
    </div>
  );
}
