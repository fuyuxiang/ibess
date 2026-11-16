import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Space, Tag } from 'antd'
import { ExpandOutlined, VideoCameraOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import Hls from 'hls.js'
import type { MonitoringSource } from '../../api/supervision'
import { phaseTitle } from '../../api/stages'

export default function LivePlayer({ source, compact = false }: { source: MonitoringSource; compact?: boolean }) {
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
      () => markError("连接超时，请检查视频源或重新连接"),
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
          markError("画面暂不可用，请检查视频源和网络后重新连接");
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
    <div ref={box} className={"sv-live-player" + (compact ? " is-compact" : "")}>
      <div className="sv-live-top">
        <Space>
          <VideoCameraOutlined />
          <strong>{source.source_name || source.room_name}</strong>
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
          <strong>此机位尚未配置视频流</strong>
          <Link to="/assurance">查看接入状态</Link>
        </div>
      ) : (
        status !== "playing" && status !== 'paused' && (
          <div className="sv-live-empty">
            <VideoCameraOutlined />
            <strong>
              {status === "connecting"
                ? "正在连接画面…"
                : "实时画面暂不可用"}
            </strong>
            <span>{error || "连接后将自动播放"}</span>
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
