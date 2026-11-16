import { useEffect, useState } from "react";
import { ManualMarkButton } from "../components/workflow/WorkspaceActions";
import type { ReactNode } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  Checkbox,
  Tabs,
} from "antd";
import {
  ArrowRightOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import {
  Link,
  useSearchParams,
  useLocation,
} from "react-router-dom";
import type { EventItem } from "../api/client";
import {
  type SupervisionQuery,
  type MonitoringSource,
} from "../api/supervision";
import { useSupervision } from "../components/workflow/useSupervision";
import {
  workflowColors,
  workflowLabels,
  workflowStatus,
  overdue,
  resolutionLabels,
} from "../components/workflow/supervision-model";
import { useRegistry } from "../store/useRegistryStore";
import { eventLabel, formatDateTime, riskColor, riskText } from "../utils/ui";

export function PageHeading({
  title,
  description,
  extra,
}: {
  title: string;
  description: string;
  extra?: ReactNode;
}) {
  return (
    <div className="sv-heading">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      <Space wrap>{extra}</Space>
    </div>
  );
}
export function RequestError({
  error,
  reload,
}: {
  error: string | null;
  reload: () => void;
}) {
  return error ? (
    <Alert
      type="error"
      showIcon
      message="数据加载失败"
      description={error}
      action={<Button onClick={reload}>重试</Button>}
    />
  ) : null;
}
export function WorkflowTag({ event }: { event: EventItem }) {
  if (event.category === "observation") return <Tag>基础观察记录</Tag>;
  const status = workflowStatus(event);
  return (
    <Space size={0} wrap>
      <Tag color={workflowColors[status]}>
        {workflowLabels[status] || status}
      </Tag>
      {overdue(event) && <Tag color="red">已逾期</Tag>}
    </Space>
  );
}
export function eventColumns(returnTo = "/events") {
  return [
    {
      title: "告警事件",
      dataIndex: "event_type",
      width: 220,
      render: (value: string, e: EventItem) => (
        <div className="sv-event-cell">
          <Link
            to={`/event-detail?event_id=${encodeURIComponent(e.event_id)}`}
            state={{ returnTo }}
          >
            {eventLabel(value)}
          </Link>
          <small title={e.event_id}>
            {e.event_id.slice(0, 8).toUpperCase()} ·{" "}
            {e.occurred_at ? formatDateTime(e.occurred_at) : '现场时间未登记'}
          </small>
          {e.is_demo && <Tag>模拟数据</Tag>}
        </div>
      ),
    },
    {
      title: "项目 / 评标室",
      key: "scope",
      width: 210,
      render: (_: unknown, e: EventItem) => (
        <div className="sv-event-cell">
          <span>{e.project_name || "未关联项目"}</span>
          <small>{e.room || e.camera_id}</small>
        </div>
      ),
    },
    {
      title: "异常线索",
      dataIndex: "reason",
      width: 260,
      render: (value: string) => <span className="sv-clue-preview" title={value}>{value || "待补充"}</span>,
    },
    {
      title: "风险等级",
      dataIndex: "risk_level",
      width: 100,
      render: (value: string, event: EventItem) => (
        <Tag
          color={
            event.machine_conclusion === "insufficient"
              ? "gold"
              : riskColor[value]
          }
        >
          {event.machine_conclusion === "insufficient"
            ? "风险未定"
            : riskText[value] || value}
        </Tag>
      ),
    },
    {
      title: "处理状态",
      key: "workflow",
      width: 185,
      render: (_: unknown, e: EventItem) => <div className="sv-event-cell"><WorkflowTag event={e} /><small>{resolutionLabels[e.resolution || ''] || "尚未认定"}</small></div>,
    },
    {
      title: "操作",
      key: "action",
      width: 100,
      fixed: "right" as const,
      render: (_: unknown, e: EventItem) => (
        <Link
          to={`/event-detail?event_id=${encodeURIComponent(e.event_id)}`}
          state={{ returnTo }}
        >
          {workflowStatus(e) === "ARCHIVED" ? "查看档案" : "核查处置"}{" "}
          <ArrowRightOutlined />
        </Link>
      ),
    },
  ];
}

export function SupervisionList({
  mode = "events", embedded=false,
}: {
  mode?: "events" | "process" | "archive"; embedded?:boolean;
}) {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const backPath = location.pathname + location.search;
  const [advanced,setAdvanced]=useState(false);
  const registry = useRegistry();
  const queue =
    mode === "process"
      ? "process"
      : mode === "archive"
        ? "archive"
        : params.get("queue") || undefined;
  const queryParams: SupervisionQuery = {
    page: Math.max(1, Number(params.get("page")) || 1),
    page_size: 20,
    category: (params.get("category") ||
      "behavior") as SupervisionQuery["category"],
    keyword: params.get("keyword") || undefined,
    project_id: params.get("project_id") || undefined,
    room_id: params.get("room_id") || undefined,
    session_id:params.get("session_id")||undefined,
    mine:params.get("mine")==="true",
    overdue:params.get("overdue")==="true",
    workflow_status: params.get("workflow_status") || undefined,
    resolution: params.get('resolution') || undefined,
    risk_level: params.get("risk_level") || undefined,
    queue: queue as SupervisionQuery["queue"],
    date_from: params.get("date_from") || undefined,
    date_to: params.get("date_to") || undefined,
  };
  const query = useSupervision(queryParams);
  const [keyword, setKeyword] = useState(params.get("keyword") || "");
  useEffect(() => setKeyword(params.get("keyword") || ""), [params]);
  function setFilter(key: string, value?: string) {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      value ? next.set(key, value) : next.delete(key);
      next.delete("page");
      if (key === "project_id") next.delete("room_id");
      if (key === "project_id" || key === "room_id") next.delete("session_id");
      return next;
    });
  }
  useEffect(() => {
    if (
      query.data &&
      !query.loading &&
      queryParams.page! > Math.max(1, Math.ceil(query.data.total / 20))
    ) {
      setParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          next.set(
            "page",
            String(Math.max(1, Math.ceil(query.data!.total / 20))),
          );
          return next;
        },
        { replace: true },
      );
    }
  }, [query.data, query.loading, queryParams.page, setParams]);
  const title =
    mode === "archive"
      ? "监管档案"
      : mode === "process"
        ? "处置中心"
        : "告警处置";
  return (
    <div className="sv-page console-queue">
      {!embedded&&<PageHeading
        title={title}
        description={
          mode === "archive"
            ? "查阅核查结论、证据和处置记录。"
            : mode === "process"
              ? "跟踪处置任务、反馈和复核进度。"
              : "核查告警证据，跟进处置与复核。"
        }
        extra={
<Space><ManualMarkButton/><Button icon={<ReloadOutlined/>} loading={query.loading} onClick={query.reload}>刷新</Button></Space>
        }
      />}
      {mode!=="archive"&&<Tabs className="console-queue-tabs" activeKey={queue||"all"} onChange={value=>{setParams(previous=>{const next=new URLSearchParams(previous);value==='all'?next.delete('queue'):next.set('queue',value);next.delete('workflow_status');next.delete('page');return next;});}} items={[{key:"confirm",label:"待核查"},{key:"dispatch",label:"待处置"},{key:"review",label:"待复核"},{key:"active",label:"全部未办结"},{key:"all",label:"全部事件"}]}/> }
      {(queryParams.session_id||queryParams.overdue)&&<Space wrap style={{marginBottom:12}}>{queryParams.session_id&&<Tag closable onClose={()=>setFilter('session_id')}>已限定场次 · {queryParams.session_id.slice(0,8)}</Tag>}{queryParams.overdue&&<Tag color="red" closable onClose={()=>setFilter('overdue')}>仅逾期待办</Tag>}</Space>}
      <RequestError
        error={query.error || registry.error}
        reload={() => {
          query.reload();
          void registry.refresh(true);
        }}
      />
      <Card className="sv-filter-card">
        <div className="sv-filters">
          <Input.Search
            aria-label="搜索告警事件"
            placeholder="事件编号、项目、评标室或线索"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onSearch={(value) => setFilter("keyword", value.trim())}
            allowClear
            style={{ width: 300 }}
          />
          <Button aria-expanded={advanced} aria-controls="event-advanced-filters" onClick={()=>setAdvanced(!advanced)}>{advanced?'收起筛选':'更多筛选'}{[queryParams.project_id,queryParams.room_id,queryParams.date_from,queryParams.date_to,queryParams.risk_level,queryParams.workflow_status,queryParams.resolution,queryParams.category!=='behavior'].filter(Boolean).length>0?'（已设置）':''}</Button>
          <Checkbox checked={queryParams.mine} onChange={e=>setFilter('mine',e.target.checked?'true':'false')}>仅本账号负责</Checkbox>
          <div className="sv-advanced-filters" id="event-advanced-filters" hidden={!advanced}>
          <Select
            aria-label="所属项目"
            placeholder="全部项目"
            allowClear
            value={queryParams.project_id}
            style={{ width: 220 }}
            onChange={(value) => setFilter("project_id", value)}
            options={registry.data?.projects.map((p) => ({
              value: p.project_id,
              label: p.name,
            }))}
          />
          <Select
            aria-label="评标室"
            placeholder="全部评标室"
            allowClear
            value={queryParams.room_id}
            style={{ width: 180 }}
            onChange={(value) => setFilter("room_id", value)}
            options={registry.data?.rooms
              .filter(
                (r) =>
                  !queryParams.project_id ||
                  r.project_id === queryParams.project_id,
              )
              .map((r) => ({ value: r.room_id, label: r.name }))}
          />

          <Select
            aria-label="风险等级"
            placeholder="全部风险"
            allowClear
            value={queryParams.risk_level}
            style={{ width: 130 }}
            onChange={(value) => setFilter("risk_level", value)}
            options={["high", "medium", "low"].map((s) => ({
              value: s,
              label: riskText[s],
            }))}
          />
          {mode!=="archive"&&<Select
            aria-label="处理状态"
            placeholder="全部状态"
            allowClear
            value={queryParams.workflow_status}
            style={{ width: 170 }}
            onChange={(value) => setFilter("workflow_status", value)}
            options={Object.entries(workflowLabels)
              .filter(([key]) =>
                mode === "process" ? ["CONFIRMED", "ASSIGNED", "FEEDBACK"].includes(key) : true,
              )
              .map(([value, label]) => ({ value, label }))}
          />
          }
          <Select
            aria-label="人工结论" placeholder="全部人工结论" allowClear value={queryParams.resolution}
            style={{width:170}} onChange={value=>setFilter('resolution',value)}
            options={[...Object.entries(resolutionLabels).map(([value,label])=>({value,label})),{value:'unreviewed',label:'尚未认定'}]}
          />
          <Select
            aria-label="告警分类"
            value={queryParams.category}
            style={{ width: 150 }}
            onChange={(value) => setFilter("category", value)}
            options={[
              { value: "behavior", label: "行为告警" },
              { value: "technical", label: "设备与服务故障" },
              { value: "all", label: "全部记录" },
            ]}
          />
          <label className="sv-date-label">
            开始日期
            <input
              type="date"
              aria-label="开始日期"
              value={queryParams.date_from || ""}
              onChange={(e) => setFilter("date_from", e.target.value)}
            />
          </label>
          <label className="sv-date-label">
            结束日期
            <input
              type="date"
              aria-label="结束日期"
              value={queryParams.date_to || ""}
              onChange={(e) => setFilter("date_to", e.target.value)}
            />
          </label>
          <small className="sv-muted">按北京时间筛选发生日期；未登记采集时间的录音不纳入日期筛选。</small>
          </div>
          <Button
            onClick={() => {
              setParams(embedded?{tab:"events"}:{});
              setKeyword("");
            }}
          >
            重置
          </Button>
        </div>
      </Card>
      <Card className="console-queue-table"
        title={
          <Space>
            {mode==='archive'?'归档记录':({confirm:'待核查告警',dispatch:'待处置任务',review:'待复核反馈',active:'全部未办结'} as Record<string,string>)[queue||'']||'全部告警'}
            <Tag>{query.data?.total ?? "—"} 条</Tag>
            {queue === "confirm" && (
              <Tag color="orange">
                待核查队列{" "}
                <button
                  className="sv-tag-close"
                  aria-label="清除待核查筛选"
                  onClick={() => setFilter("queue")}
                >
                  ×
                </button>
              </Tag>
            )}
          </Space>
        }
        extra={
          <Typography.Text type="secondary">
            {mode==='archive' ? '按最近归档时间倒序' : '高风险优先'} · 最近同步 {formatDateTime(query.updatedAt)}{query.refreshing && !query.loading ? ' · 正在同步' : ''}
          </Typography.Text>
        }
      >
        <Table
          rowKey="event_id"
          columns={
            (queue==='dispatch'||queue==='review'||mode === "process")
              ? [
                  ...eventColumns(backPath).slice(0, 2),
                  {
                    title: "处置责任 / 期限",
                    key: "assignment",
                    width: 230,
                    render: (_: unknown, e: EventItem) => (
                      <div className="sv-event-cell">
                        <strong>{e.assignee || "未分派"}</strong>
                        <small>{e.task_receipt?.received_at?"已接收":"待接收"}</small>
                        <small>
                          {e.due_at ? formatDateTime(e.due_at) : "未指定期限"}
                        </small>
                      </div>
                    ),
                  },
                  ...eventColumns(backPath).slice(3),
                ]
              : eventColumns(backPath)
          }
          dataSource={query.data?.items || []}
          loading={query.loading}
          scroll={{ x: 1080 }}
          pagination={{
            current: queryParams.page,
            pageSize: 20,
            total: query.data?.total || 0,
            showSizeChanger: false,
            showTotal: (total) => `共 ${total} 条`,
            onChange: (page) =>
              setParams((previous) => {
                const next = new URLSearchParams(previous);
                next.set("page", String(page));
                return next;
              }),
          }}
          locale={{
            emptyText: (
              <Empty
                description={
                  query.error
                    ? "加载失败，请重试"
                    : "暂无匹配记录，请调整筛选条件"
                }
              />
            ),
          }}
        />
      </Card>
    </div>
  );
}

export function sourceLabel(source: MonitoringSource) {
  return `${source.project_name} / ${source.room_name} / ${source.session_name} · ${source.source_name||source.source_id}（${source.source_id}）`;
}
