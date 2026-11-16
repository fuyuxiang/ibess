// 3.4 语义研判面板：S06/S07 任务列表、从授权快照组装研判输入、重试与轮询。
// 只使用录音的显式业务绑定；匿名说话人必须有精确标签绑定才能关联人员。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Empty, Input, InputNumber, Select, Space, Tag } from "antd";
import { Link } from "react-router-dom";
import {
  fetchAudioDetail,
  fetchAudioRecordings,
  saveCaptureTime,
} from "../../api/audioRecordings";
import { supervisionRequest, fetchSources } from "../../api/supervision";
import { fetchStageConfig } from "../../api/stages";
import { newRequestId } from "../workflow/workflow-model";
import {
  bindRecording,
  createSemanticAnalysis,
  getRecordingBinding,
  latestAuthzSnapshot,
  listSemanticAnalyses,
  retrySemanticAnalysis,
} from "../../api/semantic";
import type {
  AuthzSnapshotContext,
  SemanticAnalysis,
  SemanticScene,
} from "../../api/semantic";
import { useRegistry } from "../../store/useRegistryStore";
import { activeRooms, roomLabel } from "../../utils/registry";
import { formatDateTime, riskColor, riskText } from "../../utils/ui";
import { errorText } from "../workflow/workflow-model";
import "./semantic-review.css";
import { useAuthStore } from '../../store/useAuthStore';
import { semanticWindow } from './semantic-window';

const conclusionMeta: Record<string, { label: string; color: string }> = {
  normal: { label: "正常", color: "green" },
  suspected: { label: "疑似风险，待核查", color: "red" },
  insufficient: { label: "证据不足，待补充", color: "default" },
};

const sceneLabels: Record<SemanticScene, string> = {
  S06: "疑似倾向性施压",
  S07: "疑似保密信息外传",
};

interface AudioRecordingItem {
  recording_id: string;
  title: string;
  status: string;
}

export default function SemanticReviewPanel({initialRoomId}:{initialRoomId?:string}) {
  const readonly=useAuthStore(s=>s.user?.role)==='viewer';
  const registry = useRegistry();
  const rooms = useMemo(() => activeRooms(registry.data), [registry.data]);
  const [roomId, setRoomId] = useState<string | undefined>(initialRoomId||rooms[0]?.room_id);
  const [recordings, setRecordings] = useState<AudioRecordingItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | undefined>();
  const [analyses, setAnalyses] = useState<SemanticAnalysis[]>([]);
  const [snapshot, setSnapshot] = useState<AuthzSnapshotContext | null>(null);
  const [snapshotError, setSnapshotError] = useState("");
  const [bindingRoom, setBindingRoom] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [windowStart, setWindowStart] = useState(0);

  const selection = useRef(selectedId); selection.current = selectedId;
  const mounted = useRef(true);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;}},[]);
  const [captureTime,setCaptureTime] = useState('');
  const [savedCaptureTime,setSavedCaptureTime] = useState('');
  const [captureLoading,setCaptureLoading] = useState(false);
  const selected = recordings.find((item) => item.recording_id === selectedId);

  const reloadAnalyses = useCallback(
    async (recordingId: string | undefined) => {
      if (!recordingId) {
        setAnalyses([]);
        return;
      }
      try {
        const data = await listSemanticAnalyses(recordingId);
        if (mounted.current && selection.current===recordingId) setAnalyses(data.items);
      } catch (cause) {
        if (mounted.current && selection.current===recordingId) setError(cause instanceof Error ? cause.message : "加载语义任务失败");
      }
    },
    [],
  );

  useEffect(() => {
    void registry.refresh(true);
  }, [registry.refresh]);
  useEffect(() => {
    if (!roomId && rooms.length) setRoomId(rooms[0].room_id);
  }, [roomId, rooms]);

  useEffect(() => {
    let current = true;
    (async () => {
      try {
        const data = await fetchAudioRecordings();
        if (!current) return;
        const ready = (data.items as AudioRecordingItem[]).filter(
          (item) => item.status === "ready",
        );
        setRecordings(ready);
        setSelectedId((previous) => previous || ready[0]?.recording_id);
      } catch {
        if (current) setError("录音列表加载失败");
      }
    })();
    return () => {
      current = false;
    };
  }, []);

  useEffect(() => {
    let current = true;
    setBindingRoom(null);
    setCaptureTime('');
    setSavedCaptureTime('');
    if (!selectedId) return;
    setCaptureLoading(true);
    const controller = new AbortController();
    fetchAudioDetail(selectedId,controller.signal).then(detail=>{
      if (current) {
        const value = detail.capture_started_at ? new Date(new Date(detail.capture_started_at).getTime()+8*3600000).toISOString().slice(0,16) : '';
        setCaptureTime(value);setSavedCaptureTime(value);
      }
    }).catch(e=>{if(current)setError(e.message);}).finally(()=>{if(current)setCaptureLoading(false);});
    getRecordingBinding(selectedId)
      .then((binding) => {
        if (current && binding.room_id) {
          setBindingRoom(binding.room_id);
          setRoomId(binding.room_id);
        }
      })
      .catch(() => {
        /* 未绑定：保留人工选择 */
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [selectedId]);

  const bindCurrent = async () => {
    if (!selectedId || !roomId || busy) return;
    setBusy(true);
    try {
      const binding = await bindRecording(selectedId, roomId);
      setBindingRoom(binding.room_id);
      setNotice("录音已绑定评标室，后续分析使用本室授权记录。");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "绑定失败");
    } finally { setBusy(false); }
  };
  const saveTime = async () => {
    if (!selectedId || busy) return;
    setBusy(true);setError('');
    try {
      await saveCaptureTime(selectedId,captureTime ? `${captureTime}:00+08:00` : null);
      setSavedCaptureTime(captureTime);
      setNotice("采集时间已保存，仅用于后续新分析，已有事件与归档记录不变。");
    } catch(e) { setError(e instanceof Error ? e.message : '采集时间保存失败'); }
    finally { setBusy(false); }
  };

  useEffect(() => {
    setSnapshot(null);
    setSnapshotError("");
  }, [roomId]);
  useEffect(() => {
    void reloadAnalyses(selectedId);
  }, [selectedId, reloadAnalyses]);

  useEffect(() => {
    if (!selectedId) return;
    const timer = window.setTimeout(() => {
      void reloadAnalyses(selectedId);
    }, 8000);
    return () => window.clearTimeout(timer);
  }, [analyses, selectedId, reloadAnalyses]);

  const resolveSnapshot =
    useCallback(async (): Promise<AuthzSnapshotContext | null> => {
      if (!roomId) return null;
      try {
        const data = await latestAuthzSnapshot(roomId);
        setSnapshot(data);
        setSnapshotError("");
        return data;
      } catch {
        setSnapshot(null);
        setSnapshotError(
          "此评标室暂无有效授权记录，请在场次人员资料中核对；缺少依据时，分析将标记为证据不足。",
        );
        return null;
      }
    }, [roomId]);

  const assemble = (
    segments: Array<{
      segment_id: string;
      speaker: string | null;
      start_ms: number;
      end_ms: number;
      text: string;
    }>,
    scene: SemanticScene,
    authz: AuthzSnapshotContext | null,
  ) => {
    const speakers = [
      ...new Set(segments.map((segment) => segment.speaker)),
    ].sort();
    const bindings = speakers.map((speaker) => {
      const source = speaker
        ? authz?.speaker_bindings.find(
            (binding) =>
              binding.segment_speaker === speaker && binding.identity_verified,
          )
        : undefined;
      if (!source)
        return {
          segment_speaker: speaker,
          person_id: null,
          display_name: null,
          role: null,
          identity_verified: false,
          basis_fact_id: null,
        };
      return {
        segment_speaker: speaker,
        person_id: source.person_id,
        display_name: source.display_name,
        role: source.role,
        identity_verified: source.identity_verified,
        basis_fact_id: source.basis_fact_id,
      };
    });
    const visibility = authz?.information_visibility ?? "public";
    if (scene === "S07") {
      return {
        applicable_rules: [
          "未公开评审信息仅可提供给有权限的对象",
          "保密目录内信息不得外传",
        ],
        exceptions: ["已批准的必要澄清"],
        speaker_bindings: bindings,
        recipient_authorized: authz?.recipient_authorized ?? null,
        recipient_fact_id: authz?.recipient_fact_id ?? null,
        confidentiality_policy_id: authz?.confidentiality_policy_id ?? null,
        information_visibility: visibility,
      };
    }
    return {
      applicable_rules: [
        "专家应按评分细则独立打分",
        "不得诱导或施压他人调整分值",
      ],
      exceptions: ["正常讨论与纠错", "引用他人话语"],
      speaker_bindings: bindings,
      recipient_authorized: null,
      recipient_fact_id: null,
      confidentiality_policy_id: null,
      information_visibility: "public",
    };
  };

  const generate = async (scene: SemanticScene) => {
    if (!selected || busy) return;
    if (captureTime!==savedCaptureTime) { setError('请先保存已修改的采集时间，再生成研判。'); return; }
    if (!bindingRoom) {
      setError("请先将录音绑定到实际评标室，再生成研判。");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const detail = await fetchAudioDetail(selected.recording_id);
      const analysisWindow = semanticWindow(detail.segments,Math.round(windowStart*1000),detail.duration_ms);
      const {start,end} = analysisWindow;
      const segments = analysisWindow.segments.map((item) => ({
          segment_id: item.segment_id,
          speaker: item.speaker || null,
          start_ms: item.start_ms,
          end_ms: item.end_ms,
          text: item.text,
        }));
      if (!segments.length) {
        setNotice("此录音暂无有效转写，请先完成转写。");
        return;
      }
      const binding = await getRecordingBinding(selected.recording_id);
      if (binding.room_id !== bindingRoom) {
        setError("录音业务绑定已变化，请刷新核对");
        return;
      }
      const authz = await resolveSnapshot();
      const context = assemble(segments, scene, authz);
      const candidates = (await fetchSources()).items.filter(
        (source) =>
          source.room_id === bindingRoom && source.session_status === "running",
      );
      const sessions=[...new Set(candidates.map(s=>s.session_id))];
      const source = sessions.length === 1 ? candidates[0] : null;
      const phase = source
        ? await fetchStageConfig(source.session_id).catch(() => null)
        : null;
      const input = {
        contract_version: "2.0.0",
        analysis_id: newRequestId(),
        recording_id: selected.recording_id,
        project_id: binding.project_id,
        room_id: bindingRoom,
        session_id: source?.session_id || null,
        stage_snapshot_id: phase?.stage_snapshot_id || null,
        phase_id: phase?.phase_id || authz?.phase_id || null,
        phase_version: phase?.phase_version || null,
        scene_id: scene,
        rule_version: `${scene.toLowerCase()}-v1`,
        prompt_version: `${scene.toLowerCase()}-prompt-${scene === "S07" ? "v3" : "v1"}`,
        target_segment_ids: segments.map((segment) => segment.segment_id),
        segments,
        context,
        source_id: detail.source_id,
        binding_status: authz ? "bound" : "unbound",
        media_run_id: `recording-${selected.recording_id}`,
        asset_id: `asset-${selected.recording_id}`,
        occurred_at: detail.capture_started_at || detail.created_at,
        time_basis: detail.capture_started_at ? 'capture_start' : 'unknown',
        asr_model: detail.model || "Paraformer",
        asr_version: "funasr-v1",
        speaker_model: detail.speaker_model || "CAM++",
        speaker_model_version: "campplus-v1",
        model_name: "semantic-worker",
        model_version: "deployment-config",
        window_start_ms: start,
        window_end_ms: end,
        quality_flags: authz
          ? ["authz_snapshot"]
          : ["authorization_context_missing"],
      };
      const created = await createSemanticAnalysis(
        selected.recording_id,
        input,
      );
      setNotice(
        `${scene} 任务${created.deduplicated ? "已存在" : "已排队"}，分析时段 ${(start/1000).toFixed(1)}–${(end/1000).toFixed(1)} 秒，完成后自动更新结果。`,
      );
      await reloadAnalyses(selected.recording_id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "生成研判失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  const retry = async (analysisId: string) => {
    setBusy(true);
    setError("");
    try {
      await retrySemanticAnalysis(analysisId);
      await reloadAnalyses(selectedId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "重试失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      className="semantic-review-card"
      title="录音风险分析"
      extra={selected ? <Tag color="blue">{selected.title}</Tag> : undefined}
    >
      <div
        style={{
          display: "flex",
          gap: 12,
          flexWrap: "wrap",
          alignItems: "center",
          marginBottom: 12,
        }}
      >
        <Select
          value={selectedId}
          disabled={busy}
          showSearch
          optionFilterProp="label"
          onChange={(value) => {
            selection.current=value;
            setSelectedId(value);
            setWindowStart(0);
            setAnalyses([]);
          }}
          style={{ width: 260 }}
          placeholder="选择录音"
          options={recordings.map((item) => ({
            value: item.recording_id,
            label: item.title,
          }))}
        />
        <Select
          value={roomId}
          showSearch
          optionFilterProp="label"
          onChange={setRoomId}
          style={{ width: 240 }}
          disabled={busy || Boolean(bindingRoom)}
          placeholder="选择录音所属评标室"
          options={rooms.map((item) => ({
            value: item.room_id,
            label: roomLabel(item, registry.data),
          }))}
        />
        <label>
          分析起点（秒）{" "}
          <InputNumber
            min={0}
            max={599}
            value={windowStart}
            onChange={(value) => setWindowStart(value || 0)}
            disabled={busy}
          />
        </label>
        <Button
          type="primary"
          loading={busy}
          disabled={readonly || !selected || captureLoading}
          onClick={() => void generate("S06")}
        >
          分析施压风险
        </Button>
        <Button
          loading={busy}
          disabled={readonly || !selected || captureLoading}
          onClick={() => void generate("S07")}
        >
          分析保密风险
        </Button>
        {bindingRoom && <Tag color="cyan">已绑定评标室</Tag>}
        {!readonly && !bindingRoom && roomId && (
          <Button size="small" onClick={() => void bindCurrent()}>
            绑定评标室
          </Button>
        )}
        {snapshot && (
          <Tag color="green">
            授权版本 {snapshot.snapshot_id.slice(0, 8)} ·{" "}
            {snapshot.information_visibility === "confidential"
              ? "保密"
              : "公开"}{" "}
            · {snapshot.speaker_bindings.length} 人在名单中
          </Tag>
        )}
      </div>
      <Space wrap style={{marginBottom:12}}>
        <label>采集开始时间（北京时间） <Input type="datetime-local" value={captureTime} disabled={readonly || busy || captureLoading || !selectedId} onChange={e=>setCaptureTime(e.target.value)} style={{width:230}}/></label>
        <Button disabled={readonly || busy || captureLoading || !selectedId} onClick={()=>void saveTime()}>保存采集时间</Button>
        <span>未登记时，告警发生时间显示为“现场时间未登记”。</span>
      </Space>
      {error && (
        <Alert
          type="error"
          showIcon
          message={errorText(error)}
          style={{ marginBottom: 12 }}
        />
      )}
      {snapshotError && (
        <Alert
          type="warning"
          showIcon
          message={errorText(snapshotError)}
          style={{ marginBottom: 12 }}
        />
      )}
      {notice && (
        <Alert
          type="info"
          showIcon
          message={notice}
          style={{ marginBottom: 12 }}
        />
      )}
      {analyses.length ? (
        <div className="semantic-review-list">
          {analyses.map((analysis) => (
            <article className="semantic-review-row" key={analysis.analysis_id}>
              <header className="semantic-review-head">
                <Tag color="purple">{sceneLabels[analysis.scene_id]}</Tag>
                {analysis.status === "ready" && analysis.result ? (
                  <Tag
                    color={
                      (
                        conclusionMeta[analysis.result.conclusion] ?? {
                          color: "default",
                          label: analysis.result.conclusion,
                        }
                      ).color
                    }
                  >
                    {
                      (
                        conclusionMeta[analysis.result.conclusion] ?? {
                          color: "default",
                          label: analysis.result.conclusion,
                        }
                      ).label
                    }
                  </Tag>
                ) : undefined}
                {analysis.status === "ready" &&
                analysis.result &&
                analysis.result.conclusion === "suspected" ? (
                  <Tag color={riskColor[analysis.result.risk_level]}>
                    {riskText[analysis.result.risk_level]}
                  </Tag>
                ) : undefined}
                {analysis.status !== "ready" && (
                  <Tag color={analysis.status === "failed" ? "red" : "blue"}>
                    {{ queued: "排队中", processing: "研判中", failed: "失败" }[
                      analysis.status as "queued" | "processing" | "failed"
                    ] ?? analysis.status}
                  </Tag>
                )}
                {analysis.intake_error && <><Tag color="red">告警生成失败</Tag><Button size="small" disabled={readonly || busy} onClick={()=>{setBusy(true);void supervisionRequest(`/audio/semantic-analyses/${analysis.analysis_id}/intake-retry`,{method:'POST'}).then(()=>reloadAnalyses(selectedId)).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>重试生成告警</Button></>}
                {analysis.event_id && (
                  <Link to={`/event-detail?event_id=${analysis.event_id}`}>
                    核查告警
                  </Link>
                )}
                {analysis.deduplicated ? <Tag>已合并重复任务</Tag> : undefined}
                <small>{formatDateTime(analysis.created_at)}</small>
                <small>分析时段 {(analysis.window_start_ms/1000).toFixed(1)}–{(analysis.window_end_ms/1000).toFixed(1)} 秒</small>
                {analysis.status === "failed" ? (
                  <Button
                    size="small"
                    disabled={readonly || busy} onClick={() => void retry(analysis.analysis_id)}
                  >
                    重试
                  </Button>
                ) : undefined}
              </header>
              {analysis.status === "ready" && analysis.result ? (
                <>
                  <p className="semantic-review-reason">
                    {analysis.result.reason}
                  </p>
                  {analysis.result.evidence.length ? (
                    <ul className="semantic-review-evidence">
                      {analysis.result.evidence.map((item, index) => (
                        <li key={index}>
                          <Tag>证据</Tag>
                          <q>{item.quote}</q>
                        </li>
                      ))}
                    </ul>
                  ) : undefined}
                  {analysis.result.missing_facts.length ? (
                    <p className="semantic-review-missing">
                      待补充依据：{analysis.result.missing_facts.join("、")}
                    </p>
                  ) : undefined}
                </>
              ) : undefined}
              {analysis.status === "failed" && (
                <p className="semantic-review-missing">
                  {analysis.error
                    ? errorText(analysis.error)
                    : "研判失败，可重试。"}
                </p>
              )}
            </article>
          ))}
        </div>
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            selected
              ? "暂无分析记录，请选择风险类型开始分析。"
              : "暂无已完成转写的录音。"
          }
        />
      )}
      <p style={{ color: "#7891ac", fontSize: 11, margin: "12px 0 0" }}>
        分析使用所选评标室的当前授权记录，请核对是否适用于录音发生时。缺少绑定或授权依据时标记为信息不足，疑似风险需人工核查。
      </p>
    </Card>
  );
}
