import { Alert, Tabs } from "antd";
import { Link } from "react-router-dom";
import AudioTranscriptPanel from "../components/audio/AudioTranscriptPanel";
import SemanticReviewPanel from "../components/audio/SemanticReviewPanel";
import { PageHeading } from "./Supervision";

export default function SupervisionAudio() {
  return (
    <div className="sv-page">
      <PageHeading
        title="音频分析"
        description="上传或选择实际录音，查看原音与转写，结合可信业务上下文分析疑似施压和保密风险。"
        extra={<Link to="/events">查看已接入的预警事件</Link>}
      />
      <Alert
        type="info"
        showIcon
        message="疑似风险与信息不足结果会自动接入预警事件，供人工核查。正常分析保留在任务记录中。"
        description="匿名说话人不会自动对应具体专家；只有经过核验的精确说话人绑定才作为身份依据。"
      />
      <Tabs
        destroyOnHidden
        items={[
          {
            key: "transcript",
            label: "录音与转写",
            children: <AudioTranscriptPanel />,
          },
          {
            key: "semantic",
            label: "语义分析与预警",
            children: <SemanticReviewPanel />,
          },
        ]}
      />
    </div>
  );
}
