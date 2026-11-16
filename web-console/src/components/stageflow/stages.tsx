import {
  BlockCheckIn, BlockClarification, BlockDetailed, BlockOfflineQa, BlockOpening,
  BlockPreliminary, BlockRecusal, BlockReport, BlockScoring,
} from './StageBlocks'

export type Stage = {
  no: string
  title: string
  action: string
  focus: string
  scenes: string[]
  links: { label: string; to: string }[]
  block: React.ReactNode
}

export const STAGES: Stage[] = [
  {
    no: '01',
    title: '开标',
    action: '电子平台中的开标记录已形成，展示开标与封闭评标的业务边界。',
    focus: '避免安排投标人进入评标室作为常态演示。这一步讲的是边界，不依赖画面。',
    scenes: [],
    links: [
      { label: '首页总览', to: '/overview' },
      { label: '事件详情', to: '/event-detail' },
    ],
    block: <BlockOpening />,
  },
  {
    no: '02',
    title: '签到与入场',
    action: '专家到场签到、存放通讯设备、门禁核验。',
    focus: '演示 S04 授权核验，以及 S01 入口机位的条件性检测——设备寄存后才放行，未寄存会被记为条件性异常。',
    scenes: ['S04', 'S01'],
    links: [
      { label: '人脸核验', to: '/face-verify' },
      { label: '视频监测 · 入口机位', to: '/monitor?cam=cam2' },
    ],
    block: <BlockCheckIn />,
  },
  {
    no: '03',
    title: '回避与换人',
    action: '专家回避申报后，按批准流程更换并补抽。',
    focus: '展示 S04「合法换人」不触发违规认定的难负样本：换人有批文与补抽记录，就不算替身。',
    scenes: ['S04'],
    links: [
      { label: '人脸核验', to: '/face-verify' },
      { label: '人工处置', to: '/manual' },
    ],
    block: <BlockRecusal />,
  },
  {
    no: '04',
    title: '宣读纪律与初步评审',
    action: '宣读纪律、查看招标文件、开展初步评审。',
    focus: '展示模型不会把正常低头阅读或合法讨论判成异常。这一阶段的「没有告警」本身就是结论。',
    scenes: [],
    links: [
      { label: '视频监测 · 评标室全景', to: '/monitor?cam=cam1' },
      { label: '首页总览', to: '/overview' },
    ],
    block: <BlockPreliminary />,
  },
  {
    no: '05',
    title: '详细评审',
    action: '详细评审中播放经授权的可见手机使用片段、越界离岗片段，以及带上下文的倾向性施压录音。',
    focus: 'S02 通话、S03 离岗、S06 倾向性发言。S06 展示真实录音的完整转写与说话人分离，命中句可点到秒定位；授权与违规的差别在上下文，不在动作本身。',
    scenes: ['S02', 'S03', 'S06'],
    links: [
      { label: '视频监测 · 工位离岗', to: '/monitor?cam=cam9' },
      { label: '视频监测 · 区域越界', to: '/monitor?cam=cam10' },
      { label: '音频监测', to: '/audio' },
    ],
    block: <BlockDetailed />,
  },
  {
    no: '06',
    title: '澄清',
    action: '必要澄清通过模拟电子评审系统形成书面记录。',
    focus: 'S07 真实录音里，01:07 之前是对内澄清，之后是打给投标人的电话——同一句话，接收对象不同，结论就相反。',
    scenes: ['S07'],
    links: [
      { label: '音频监测', to: '/audio' },
      { label: '事件详情', to: '/event-detail' },
    ],
    block: <BlockClarification />,
  },
  {
    no: '07',
    title: '评分',
    action: '评分完成后，展示客观项评分一致性与计算结果校验的模拟接口。',
    focus: '突出该环节依赖评审数据。视频只提供见证，不参与打分判断。',
    scenes: [],
    links: [
      { label: '业务事实', to: '/facts' },
      { label: '证据留存', to: '/evidence' },
    ],
    block: <BlockScoring />,
  },
  {
    no: '08',
    title: '报告与签署',
    action: '生成评标报告、完成电子签署与资料交接。',
    focus: '系统检测视频断流与遮挡，生成见证缺口记录（S10）。缺口要如实写进归档，不能隐去。',
    scenes: ['S10'],
    links: [
      { label: '证据留存', to: '/evidence' },
      { label: '视频监测 · 遮挡恢复', to: '/monitor?cam=cam8' },
    ],
    block: <BlockReport />,
  },
  {
    no: '09',
    title: '离线质检',
    action: '系统回放同一批录像进行离线质检。',
    focus: '生成项目时间轴、人工确认清单、规则与模型版本，以及证据校验报告。',
    scenes: [],
    links: [
      { label: '历史审计', to: '/video-audit' },
      { label: '证据校验', to: '/evidence' },
    ],
    block: <BlockOfflineQa />,
  },
]
