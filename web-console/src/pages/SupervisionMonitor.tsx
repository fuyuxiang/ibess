import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Empty, Form, Input, Modal, Select, Space, Table, Tabs, Tag } from "antd";
import { ManualMarkButton } from "../components/workflow/WorkspaceActions";
import RoomAudio from "../components/audio/RoomAudio";
import { healthLabels } from "../api/workspace";
import { useAuthStore } from "../store/useAuthStore";
import { supervisionRequest } from "../api/supervision";
import {
  ExpandOutlined,
  ReloadOutlined,
  VideoCameraOutlined,
} from "@ant-design/icons";
import { Link, useSearchParams } from "react-router-dom";
import Hls from "hls.js";
import {
  fetchMonitoringHealth,
  fetchSources,
  type MonitoringSource,
} from "../api/supervision";
import { useSupervision } from "../components/workflow/useSupervision";
import { useRefreshingQuery } from "../components/workflow/useRefreshingQuery";
import {
  PageHeading,
  RequestError,
  eventColumns,
  sourceLabel,
} from "./Supervision";
import { phaseTitle } from "../api/stages";
import { formatDateTime } from "../utils/ui";

function LivePlayer({ source }: { source: MonitoringSource }) {
  const video = useRef<HTMLVideoElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<"connecting" | "playing" | "error" | "paused">(
    "connecting",
  );
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    const element = video.current;
    if (!element || !source.hls_url) return;
    let alive = true;
    let timer: number | undefined;
    let stallTimer: number | undefined;
    let hls: Hls | null = null;
    setStatus("connecting");
    setError("");
    const markError = (text: string) => {
      if (alive) {
        setStatus("error");
        setError(text);
      }
    };
    timer = window.setTimeout(
      () => markError("画面连接超时，请检查推流或重新连接"),
      20000,
    );
    if (Hls.isSupported()) {
      hls = new Hls({ lowLatencyMode: false, maxBufferLength: 15, backBufferLength: 10, liveSyncDurationCount: 3 });
      let recoveries = 0;
      hls.loadSource(source.hls_url);
      hls.attachMedia(element);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        void element
          .play()
          .catch(() => markError("浏览器未开始播放，请点击播放按钮"));
      });
      hls.on(Hls.Events.ERROR, (_, data) => {
        if (!alive || !data.fatal) return;
        if (data.type === Hls.ErrorTypes.MEDIA_ERROR && recoveries++ < 2)
          hls?.recoverMediaError();
        else {
          markError("实时画面暂不可用，请核对对应采集源、推流及网络后重新连接");
          hls?.stopLoad();
        }
      });
    } else if (element.canPlayType("application/vnd.apple.mpegurl")) {
      element.src = source.hls_url;
      void element.play().catch(() => markError("请点击播放器开始播放"));
    } else markError("当前浏览器不支持该视频流，请使用支持 HLS 的浏览器");
    const playing = () => {
      if (alive) {
        window.clearTimeout(timer);
        window.clearTimeout(stallTimer);
        setStatus("playing");
        setError("");
      }
    };
    const paused = () => { if(alive) { window.clearTimeout(timer); setStatus('paused'); } };
    // 分片边界会有毫秒级缓冲，立刻盖遮罩就是"一直闪"的元凶；持续 2.5 秒才提示
    const waiting = () => {
      if (!alive) return;
      window.clearTimeout(stallTimer);
      stallTimer = window.setTimeout(() => {
        if (!alive) return;
        setStatus('connecting');
        window.clearTimeout(timer);
        timer = window.setTimeout(() => markError('画面持续缓冲，请检查网络或重新连接'), 20000);
      }, 2500);
    };
    const failed = () => markError("播放中断，请重新连接采集源");
    element.addEventListener("playing", playing);
    element.addEventListener("error", failed);
    element.addEventListener('pause',paused);
    element.addEventListener('waiting',waiting);
    element.addEventListener('stalled',waiting);
    element.addEventListener('ended',failed);
    const ticking = () => { if (alive && element.currentTime > 0) window.clearTimeout(stallTimer); };
    element.addEventListener('timeupdate', ticking);
    return () => {
      alive = false;
      window.clearTimeout(timer);
      hls?.destroy();
      element.pause();
      element.removeAttribute("src");
      element.load();
      element.removeEventListener("playing", playing);
      element.removeEventListener("error", failed);
      element.removeEventListener('pause',paused);
      element.removeEventListener('waiting',waiting);
      element.removeEventListener('stalled',waiting);
      element.removeEventListener('ended',failed);
      element.removeEventListener('timeupdate', ticking);
      window.clearTimeout(stallTimer);
    };
  }, [source.hls_url, nonce]);
  return (
    <div ref={box} className="sv-live-player">
      <div className="sv-live-top">
        <Space>
          <VideoCameraOutlined />
          <strong>{source.room_name}</strong>
          <Tag
            color={
              status === "playing"
                ? "green"
                : status === "error"
                  ? "red"
                  : "gold"
            }
          >
            {status === "playing"
              ? "画面播放中"
              : status === 'paused' ? '已暂停' : status === "error"
                ? "画面不可用"
                : "连接中"}
          </Tag>
        </Space>
        <Button
          size="small"
          icon={<ExpandOutlined />}
          onClick={() => {
            void box.current
              ?.requestFullscreen()
              .catch(() => setError("无法进入全屏，请使用播放器的全屏按钮"));
          }}
        >
          全屏
        </Button>
      </div>
      <video
        ref={video}
        controls
        autoPlay
        muted
        playsInline
        aria-label={`${source.room_name}实时画面`}
      />
      {!source.hls_url ? (
        <div className="sv-live-empty">
          <VideoCameraOutlined />
          <strong>该来源尚未配置可播放通道</strong>
          <Link to="/stages/console">前往场次与来源配置</Link>
        </div>
      ) : (
        status !== "playing" && status !== 'paused' && (
          <div className="sv-live-empty">
            <VideoCameraOutlined />
            <strong>
              {status === "connecting"
                ? "正在连接实际监测画面"
                : "实时画面暂不可用"}
            </strong>
            <span>{error || "画面连接完成后自动播放"}</span>
            {status === "error" && (
              <Button onClick={() => setNonce((n) => n + 1)}>重新连接</Button>
            )}
          </div>
        )
      )}
      <div className="sv-live-bottom">
        <span>
          来源 {source.source_id} · {source.session_name}
        </span>
        <span>{phaseTitle(source.phase_id)}</span>
      </div>
      {error && status === "playing" && (
        <Alert
          type="warning"
          message={error}
          closable
          onClose={() => setError("")}
        />
      )}
    </div>
  );
}

export default function SupervisionMonitor() {
  const [view,setView]=useState('single');
  const role=useAuthStore(s=>s.user?.role);
  const health=useRefreshingQuery('monitor-health',signal=>fetchMonitoringHealth(signal));
  const [channelOpen,setChannelOpen]=useState(false),[channelBusy,setChannelBusy]=useState(false),[channelError,setChannelError]=useState('');
  const [channelForm]=Form.useForm();
  const [params, setParams] = useSearchParams();
  const sourceQuery = useRefreshingQuery('monitor-sources',signal=>fetchSources(signal));
  const sources = sourceQuery.data?.items || [];
  const loading = sourceQuery.loading;
  const error = sourceQuery.error;
  const eligible = sources.filter(
    (s) =>
      (!params.get("project_id") ||
        s.project_id === params.get("project_id")) &&
      (!params.get("room_id") || s.room_id === params.get("room_id")),
  );
  const requested = params.get("binding_id");
  const selected = requested
    ? eligible.find((s) => s.binding_id === requested)
    : eligible.find((s) => s.session_status === "running"&&s.kind!=='audio') || eligible[0];
  const events = useSupervision(
    {
      page_size: 10,
      category: "behavior",
      session_id: selected?.session_id,
      camera_id: view==='single'?selected?.source_id:undefined,
      queue:'active',
      room_id: selected?.room_id,
    },
    Boolean(selected),
  );
  const projects = [
    ...new Map(
      sources.map((s) => [
        s.project_id,
        { value: s.project_id, label: s.project_name },
      ]),
    ).values(),
  ];
  return (
    <div className="sv-page">
      <PageHeading
        title="现场监测"
        description="选择已绑定的项目、评标室和采集源，核查对应画面与异常事件。"
        extra={
          <>
            <Link to="/audio">
              <Button>音频分析</Button>
            </Link>
            <ManualMarkButton source={selected}/>
            {role==='admin'&&selected&&<Button onClick={()=>{channelForm.resetFields();channelForm.setFieldsValue({kind:'video'});setChannelError('');setChannelOpen(true);}}>登记辅助来源</Button>}
            <Button
              icon={<ReloadOutlined />}
              loading={loading}
              onClick={() => {
                sourceQuery.reload();
                events.reload();
              }}
            >
              刷新
            </Button>
          </>
        }
      />
      <RequestError error={error} reload={() => sourceQuery.reload()} />
      <Card className="sv-filter-card">
        <Space wrap>
          <Select
            aria-label="监测项目"
            placeholder="全部已绑定项目"
            allowClear
            value={params.get("project_id") || undefined}
            options={projects}
            style={{ width: 260 }}
            onChange={(value) => setParams(value ? { project_id: value } : {})}
          />
          <Select
            aria-label="监测场次与采集源"
            placeholder="选择监测场次与采集源"
            value={selected?.binding_id}
            style={{ width: "min(660px, 70vw)" }}
            options={eligible.map((s) => ({
              value: s.binding_id,
              label: sourceLabel(s),
            }))}
            onChange={(value) =>
              setParams((previous) => {
                const next = new URLSearchParams(previous);
                next.set("binding_id", value);
                return next;
              })
            }
          />
          {role==='admin'&&<Link to="/stages/console">管理场次绑定</Link>}
          <Select aria-label="画面布局" value={view} onChange={setView} options={[{value:'single',label:'单画面核查'},{value:'quad',label:'同场次多画面（最多4路）'}]} style={{width:210}}/>
        </Space>
      </Card>
      {!selected ? (
        <Card loading={loading}>
          <Empty
            description={
              error
                ? "加载失败，请重试"
                : requested
                  ? "所选监测源已不存在或不属于当前筛选范围"
                  : "当前范围内没有绑定的监测场次"
            }
          >
            <Link to="/stages/console">
              <Button type="primary">配置项目、评标室与采集源</Button>
            </Link>
            {requested && (
              <Button onClick={() => setParams({})}>清除筛选</Button>
            )}
          </Empty>
        </Card>
      ) : (
        <>
          <div className="sv-monitor-grid">
            <Card className="sv-monitor-media"><Tabs destroyOnHidden defaultActiveKey={selected.kind==='audio'?'audio':'video'} key={selected.binding_id} items={[
              ...(selected.kind!=='audio'?[{key:'video',label:'实时画面',children:view==='single'?<LivePlayer key={selected.binding_id} source={selected}/>:<div className="sv-live-grid">{eligible.filter(s=>s.session_id===selected.session_id&&s.kind!=='audio').slice(0,4).map(s=><LivePlayer key={s.binding_id} source={s}/>)}</div>}]:[]),
              {key:'audio',label:'场次原音与转写',children:<RoomAudio key={selected.session_id} sessionId={selected.session_id}/>},
            ]}/></Card>
            <Card title="当前监管范围">
              <DescriptionsBlock source={selected} />
              <Space wrap>{(['stream_status','audio_status','recording_status','analysis_status'] as const).map(key=>{const item=health.data?.items.find(s=>s.binding_id===selected.binding_id);const labels={stream_status:'画面',audio_status:'原音',recording_status:'录像',analysis_status:'分析'};const status=item?.[key]||'unknown';return <Tag key={key} color={['offline','input_error','stale'].includes(status)?'red':status==='unknown'?'gold':'default'}>{labels[key]}：{healthLabels[status]||status}</Tag>;})}</Space>
              <Alert
                type="info"
                showIcon
                message="监测状态分别核验"
                description="画面连接、近期录音/录像活动和分析回执分别呈现。无原音或分析暂停时，不以没有预警判定现场正常。"
              />
              <Space wrap style={{ marginTop: 20 }}>
                <Link
                  to={`/events?project_id=${selected.project_id}&room_id=${selected.room_id}`}
                >
                  查看该评标室预警
                </Link>
                <Link to="/assurance">检查监测保障</Link>
              </Space>
            </Card>
          </div>
          <RequestError error={events.error} reload={events.reload} />
          <Card
            title="当前采集源关联的异常事件"
            extra={<Tag>按当前场次、来源与评标室筛选</Tag>}
          >
            <Table
              rowKey="event_id"
              columns={eventColumns().filter((c) => c.key !== "scope")}
              dataSource={events.data?.items || []}
              loading={events.loading}
              pagination={false}
              scroll={{ x: 900 }}
              locale={{
                emptyText: (
                  <Empty
                    description={
                      events.error
                        ? "加载失败，请重试"
                        : "当前采集源暂无现场行为预警"
                    }
                  />
                ),
              }}
            />
          </Card>
        </>
      )}
      <Modal title="登记本场次辅助监测来源" open={channelOpen} confirmLoading={channelBusy} okText="保存来源" cancelText="取消" maskClosable={false} onCancel={()=>{if(!channelBusy)setChannelOpen(false);}} onOk={()=>{void(async()=>{if(channelBusy||!selected)return;try{const values=await channelForm.validateFields();setChannelBusy(true);setChannelError('');await supervisionRequest(`/supervision/sessions/${selected.session_id}/channels`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});setChannelOpen(false);sourceQuery.reload();health.reload();}catch(e){if(e instanceof Error)setChannelError(e.message);}finally{setChannelBusy(false);}})();}}>
        <Alert type="info" showIcon message="辅助来源复用当前项目、评标室及场次" description="仅登记实际部署的通道标识，不创建虚假场次；保存配置不表示设备或算法已经运行。"/>{channelError&&<Alert type="error" message={channelError}/>}
        <Form form={channelForm} layout="vertical" disabled={channelBusy}><Form.Item name="name" label="机位 / 拾音源名称" rules={[{required:true,whitespace:true}]}><Input maxLength={128}/></Form.Item><Form.Item name="source_id" label="实际来源标识" rules={[{required:true},{pattern:/^[A-Za-z0-9_-]+$/,message:'使用字母、数字、下划线或连字符'}]}><Input maxLength={64} placeholder="例如 camera-002 或 mic-01"/></Form.Item><Form.Item name="kind" label="来源类型" rules={[{required:true}]}><Select options={[{value:'video',label:'视频机位'},{value:'audio',label:'拾音来源'}]}/></Form.Item></Form>
      </Modal>
    </div>
  );
}
function DescriptionsBlock({ source }: { source: MonitoringSource }) {
  return (
    <dl className="sv-source-facts">
      {[
        ["项目", source.project_name],
        ["评标室", source.room_name],
        ["场次", source.session_name],
        ["采集源", source.source_id],
        ["评标阶段", phaseTitle(source.phase_id)],
        [
          "场次状态",
          { running: "进行中", draft: "未开始", closed: "已结束" }[
            source.session_status
          ] || source.session_status,
        ],
      ].map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function MonitoringAssurance() {
  const query = useRefreshingQuery('monitoring-health',signal=>fetchMonitoringHealth(signal));
  const health = query.data;
  const error = query.error;
  const loading = query.loading;
  const faults = useSupervision({ category: "technical", page_size: 10 });
  return (
    <div className="sv-page">
      <PageHeading
        title="监测保障"
        description="分别核对画面连接、语音输入、分析回执与录像活动，技术故障不计入专家行为异常。"
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
        message="状态以实际探测或服务回执为准"
        description="画面通过播放列表探测；分析按近期来源观察回执、录像按文件活动判断。未收到回执显示待核验，近期活动不代表全程录像完整。"
      />
      <Card
        title="监测源状态"
        extra={
          <span className="sv-muted">
            检查时间：{formatDateTime(health?.checked_at)}
          </span>
        }
      >
        <Table
          rowKey="binding_id"
          dataSource={health?.items || []}
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
            { title: "来源", dataIndex: "source_id" },
            ...[['画面连接','stream_status'],['语音输入','audio_status'],['分析回执','analysis_status'],['录像活动','recording_status']].map(([title,dataIndex])=>({title,dataIndex,render:(state:string)=><Tag color={['running','recent','online'].includes(state)?'green':['offline','input_error','stale'].includes(state)?'red':'default'}>{healthLabels[state]||'待核验'}</Tag>})),
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
                  error ? "状态加载失败" : "尚未配置监测源，请在场次管理中绑定"
                }
              />
            ),
          }}
        />
      </Card>
      <Card
        title="监测技术故障"
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
    </div>
  );
}
