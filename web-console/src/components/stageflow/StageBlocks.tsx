import { CheckCircleFilled, CloseCircleFilled, ExclamationCircleFilled } from '@ant-design/icons'
import TranscriptPanel from './TranscriptPanel'

function Check({ children }: { children: React.ReactNode }) {
  return <li className="sf-check"><CheckCircleFilled />{children}</li>
}

function ResultCard({ tone, title, note }: { tone: 'ok' | 'warn' | 'bad'; title: string; note?: string }) {
  const Icon = tone === 'ok' ? CheckCircleFilled : tone === 'warn' ? ExclamationCircleFilled : CloseCircleFilled
  return <div className={`sf-result sf-result-${tone}`}>
    <Icon />
    <div><b>{title}</b>{note ? <small>{note}</small> : null}</div>
  </div>
}

function Kv({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return <div className="sf-kv">
    <span>{label}</span>
    <b className={strong ? 'is-strong' : undefined}>{value}</b>
  </div>
}

const BIDDERS = [
  { no: '01', name: '投标人 A', price: '1,286.50' },
  { no: '02', name: '投标人 B', price: '1,312.00' },
  { no: '03', name: '投标人 C', price: '1,268.80' },
  { no: '04', name: '投标人 D', price: '1,340.20' },
  { no: '05', name: '投标人 E', price: '1,299.00' },
  { no: '06', name: '投标人 F', price: '1,275.60' },
  { no: '07', name: '投标人 G', price: '1,320.40' },
  { no: '08', name: '投标人 H', price: '1,288.90' },
  { no: '09', name: '投标人 I', price: '1,305.30' },
  { no: '10', name: '投标人 J', price: '1,278.20' },
  { no: '11', name: '投标人 K', price: '1,295.70' },
  { no: '12', name: '投标人 L', price: '1,310.10' },
]

function BidderTable({ rows }: { rows: typeof BIDDERS }) {
  return <table className="sf-table">
    <thead>
      <tr><th>序号</th><th>投标人</th><th>投标报价（万元）</th><th>解密</th></tr>
    </thead>
    <tbody>
      {rows.map(row => <tr key={row.no}>
        <td>{row.no}</td>
        <td>{row.name}</td>
        <td>{row.price}</td>
        <td className="sf-rate"><CheckCircleFilled />已解密</td>
      </tr>)}
    </tbody>
  </table>
}

export function BlockOpening() {
  return <div className="sf-stack">
    <div className="sf-grid">
      <div className="sf-panel">
        <div className="sf-panel-title">开标记录</div>
        <div className="sf-kv-list">
          <Kv label="开标时间" value="2026-10-31 09:00" />
          <Kv label="开标地点" value="电子评标平台" />
          <Kv label="投标人数量" value="12 家" />
          <Kv label="记录状态" value="已生成" strong />
        </div>
      </div>
      <div className="sf-panel">
        <div className="sf-panel-title">业务边界</div>
        <div className="sf-boundary">
          <div className="sf-boundary-item sf-boundary-open">
            <b>开标阶段</b>
            <small>投标人可进入</small>
          </div>
          <div className="sf-boundary-arrow">→</div>
          <div className="sf-boundary-item sf-boundary-closed">
            <b>封闭评标阶段</b>
            <small>投标人不得进入</small>
          </div>
        </div>
        <p className="sf-panel-note">边界一旦跨越，入口机位应留下记录；本阶段不安排投标人入场。</p>
      </div>
    </div>

    <div className="sf-panel">
      <div className="sf-panel-title">投标人一览 · 12 家 · 投标文件均已解密</div>
      <div className="sf-grid sf-grid-split">
        <BidderTable rows={BIDDERS.slice(0, 6)} />
        <BidderTable rows={BIDDERS.slice(6)} />
      </div>
      <p className="sf-panel-note">12 家投标人全程未进入评标区，入口机位无人员记录——这是“开标结束即封闭”的直接证据。</p>
    </div>
  </div>
}

const ENTRY_LOG = [
  { name: '测试专家02', time: '08:52', device: '已寄存', face: '0.81', result: '放行' },
  { name: '测试专家01', time: '08:54', device: '已寄存', face: '0.76', result: '放行' },
  { name: '测试专家03', time: '08:57', device: '已寄存', face: '0.79', result: '放行' },
  { name: '测试专家04', time: '09:01', device: '已寄存', face: '0.83', result: '放行' },
  { name: '测试专家06', time: '09:03', device: '未寄存 → 已补寄存', face: '0.78', result: '提示后放行', warn: true },
]

export function BlockCheckIn() {
  return <div className="sf-stack">
    <div className="sf-grid sf-grid-thumb">
      <figure className="sf-thumb">
        <img src="/stage/placeholder.svg" alt="入口机位示意图" />
        <figcaption>S01 入口机位 · 条件性检测</figcaption>
      </figure>
      <div className="sf-stack">
        <div className="sf-flow">
          <span className="sf-flow-item">签到</span>
          <i className="sf-flow-arrow" />
          <span className="sf-flow-item">寄存通讯设备</span>
          <i className="sf-flow-arrow" />
          <span className="sf-flow-item">门禁核验</span>
        </div>
        <div className="sf-panel sf-panel-tight">
          <div className="sf-panel-title">S04 授权核验</div>
          <ul className="sf-checks">
            <Check>身份核验</Check>
            <Check>人脸比对</Check>
            <Check>权限校验</Check>
          </ul>
          <ResultCard tone="ok" title="授权通过" note="设备已寄存，放行" />
        </div>
      </div>
    </div>

    <div className="sf-panel">
      <div className="sf-panel-title">入场核验记录 · 5 位专家</div>
      <table className="sf-table">
        <thead>
          <tr><th>专家</th><th>签到时间</th><th>通讯设备</th><th>人脸比对</th><th>处理结果</th></tr>
        </thead>
        <tbody>
          {ENTRY_LOG.map(row => <tr key={row.name} className={row.warn ? 'is-outlier' : undefined}>
            <td>{row.name}</td>
            <td>{row.time}</td>
            <td>{row.device}</td>
            <td>{row.face}</td>
            <td className={row.warn ? 'sf-warn' : 'sf-state-ok'}>
              {row.warn ? <><ExclamationCircleFilled />{row.result}</> : row.result}
            </td>
          </tr>)}
        </tbody>
      </table>
      <p className="sf-panel-note">最后一位未寄存被拦下，补寄存后才放行——这就是“条件性检测”：不是抓违规，而是卡住条件。</p>
    </div>
  </div>
}

export function BlockRecusal() {
  return <div className="sf-stack">
    <div className="sf-grid">
      <div className="sf-panel">
        <div className="sf-panel-title">更换流程</div>
        <ol className="sf-steps">
          <li className="sf-step is-done"><span className="sf-step-no">1</span>
            <div><b>专家回避申报</b><small>已提交</small></div></li>
          <li className="sf-step is-done"><span className="sf-step-no">2</span>
            <div><b>组织更换专家</b><small>已批准</small></div></li>
          <li className="sf-step is-done"><span className="sf-step-no">3</span>
            <div><b>补抽专家</b><small>已完成</small></div></li>
        </ol>
      </div>
      <div className="sf-panel">
        <div className="sf-panel-title">S04 检测结果</div>
        <ResultCard tone="ok" title="未发现违规" note="换人有批文与补抽记录" />
        <p className="sf-panel-note">合法换人不触发替身告警，这是本阶段最需要讲清的负样本。</p>
      </div>
    </div>

    <div className="sf-panel">
      <div className="sf-panel-title">回避申报与补抽记录 · 全流程留痕</div>
      <div className="sf-grid sf-grid-split">
        <div className="sf-kv-list">
          <Kv label="回避专家" value="王建军（评委 2）" />
          <Kv label="回避事由" value="与投标人 B 存在近亲属关系" />
          <Kv label="申报时间" value="2026-10-31 09:12" />
        </div>
        <div className="sf-kv-list">
          <Kv label="审批结果" value="已批准（组长 测试专家02）" strong />
          <Kv label="补抽专家" value="测试专家05 · 系统随机抽取" />
          <Kv label="到岗核验" value="人脸比对 0.81 · 通过" />
        </div>
      </div>
    </div>
  </div>
}

const PRELIM_CASES = [
  { action: '低头阅读招标文件', verdict: '正常', tone: 'ok', basis: '视线停留在桌面文件区' },
  { action: '两人低声讨论', verdict: '正常', tone: 'ok', basis: '无手机、无资料传递' },
  { action: '起身取水', verdict: '正常', tone: 'ok', basis: '离座 20 秒内返回' },
  { action: '向邻座递纸质材料', verdict: '需关注', tone: 'warn', basis: '发生资料递接，留待复核' },
]

export function BlockPreliminary() {
  return <div className="sf-stack">
    <div className="sf-grid sf-grid-thumb">
      <figure className="sf-thumb">
        <img src="/stage/placeholder.svg" alt="评标室示意图" />
        <figcaption>评标室全景 · 低头阅读与讨论</figcaption>
      </figure>
      <div className="sf-panel">
        <div className="sf-panel-title">VLM 识别结果</div>
        <ul className="sf-checks sf-checks-lg">
          <Check>正常阅读</Check>
          <Check>正常讨论</Check>
          <Check>未发现异常</Check>
        </ul>
        <p className="sf-panel-note">这一阶段的「没有告警」本身就是结论：不把常规动作判成违规。</p>
      </div>
    </div>

    <div className="sf-panel">
      <div className="sf-panel-title">VLM 判定明细 · 本阶段典型动作</div>
      <table className="sf-table">
        <thead>
          <tr><th>画面动作</th><th>判定</th><th>判定依据</th></tr>
        </thead>
        <tbody>
          {PRELIM_CASES.map(item => <tr key={item.action}>
            <td>{item.action}</td>
            <td className={item.tone === 'ok' ? 'sf-state-ok' : 'sf-warn'}>{item.verdict}</td>
            <td>{item.basis}</td>
          </tr>)}
        </tbody>
      </table>
      <p className="sf-panel-note">四类动作里三条判正常、一条标「需关注」——不是所有动作都要报警，这才是可用的判定。</p>
    </div>
  </div>
}

export function BlockDetailed() {
  const items = [
    { tag: 'S02', title: '可见手机使用', img: '/stage/placeholder.svg', note: '检测到：手机使用' },
    { tag: 'S03', title: '越界离岗', img: '/stage/placeholder.svg', note: '检测到：工位无人' },
    { tag: 'S06', title: '倾向性施压录音', wave: true, note: '检测到：倾向性发言' },
  ]
  return <div className="sf-stack">
    <div className="sf-triple">
      {items.map(item => <div key={item.tag} className="sf-triple-card">
        <div className="sf-triple-head">
          <span className="sf-triple-tag">{item.tag}</span>
          <span className="sf-triple-name">{item.title}</span>
        </div>
        <div className="sf-triple-media">
          {item.wave
            ? <div className="sf-wave">{Array.from({ length: 34 }).map((_, i) =>
              <i key={i} style={{ height: `${18 + Math.abs(Math.sin(i * 1.7)) * 30}%` }} />)}</div>
            : <img className="sf-triple-img" src={item.img} alt={item.title} />}
          <em className="sf-triple-badge">异常</em>
        </div>
        <div className="sf-triple-foot"><span>{item.note}</span></div>
      </div>)}
    </div>

    <TranscriptPanel
      source="S06"
      title="S06 真实录音转写 · 现场拾音，原文未改写"
      riskTerms={['领导比较关注', '往上靠', '靠一靠', '改到七', '别调出前两名', '你们心里有数']}
      riskLabel="倾向性表达"
      note="红标只说明这句话落进了倾向性表达词表，是否构成施压还要看它有没有影响打分——第 54 秒那段正好接在改分动作上。"
    />
  </div>
}

export function BlockClarification() {
  return <div className="sf-stack">
    <div className="sf-grid">
      <div className="sf-panel">
        <div className="sf-panel-title">澄清记录（电子评审系统）</div>
        <div className="sf-kv-list">
          <Kv label="申请人" value="投标人 C" />
          <Kv label="接收对象" value="评标委员会" />
          <Kv label="话题" value="交付周期 60 天 / 90 天" />
          <Kv label="书面记录" value="已形成（编号 Q-01）" strong />
        </div>
      </div>
      <div className="sf-panel">
        <div className="sf-panel-title">同一段录音，两种定性</div>
        <ul className="sf-checks">
          <Check>01:07 前：对内澄清，对象与话题均合规</Check>
          <Check>01:07 后：通话转给投标人，名次与报价外泄</Check>
        </ul>
        <ResultCard tone="bad" title="识别到泄露评审信息" note="对象越权，且要求“别往外说”" />
        <p className="sf-panel-note">判定只看两件事：接收对象是否在授权范围、话题是否属未公开评审信息。同一句话换个对象，结论就反过来。</p>
      </div>
    </div>

    <TranscriptPanel
      source="S07"
      title="S07 真实录音转写 · 一段通话里同时存在合规澄清与泄密"
      riskTerms={['总分最高', '排第', '低八万', '别往外说', '拍给你', '先别跟']}
      riskLabel="敏感表达"
      divider={{ atMs: 67000, text: '01:07 · 澄清结束，通话对象换成投标人代表' }}
      note="同一句“排名第二、报价低八万”，在委员会内部是工作信息，说给投标人就是泄密。"
    />
  </div>
}

const SCORE_ITEMS = [
  { name: '技术方案完整性', weight: '25%', scores: [92, 93, 91] },
  { name: '实施与服务能力', weight: '20%', scores: [88, 90, 89] },
  { name: '项目团队配置', weight: '15%', scores: [85, 86, 84] },
  { name: '类似业绩', weight: '15%', scores: [90, 78, 91], outlier: true },
  { name: '商务响应', weight: '10%', scores: [86, 85, 87] },
  { name: '投标报价', weight: '15%', scores: [82, 83, 82] },
]

export function BlockScoring() {
  return <div className="sf-stack">
    <div className="sf-panel">
      <div className="sf-panel-title">评分一致性校验 · 评委 3 人 · 客观项</div>
      <table className="sf-table">
        <thead>
          <tr>
            <th>评审项</th><th>权重</th>
            <th>评委 1</th><th>评委 2</th><th>评委 3</th>
            <th>极差</th><th>一致性</th>
          </tr>
        </thead>
        <tbody>
          {SCORE_ITEMS.map(item => {
            const diff = Math.max(...item.scores) - Math.min(...item.scores)
            return <tr key={item.name} className={item.outlier ? 'is-outlier' : undefined}>
              <td>{item.name}</td>
              <td>{item.weight}</td>
              {item.scores.map((score, index) => <td key={index}>{score}</td>)}
              <td className={diff > 5 ? 'sf-warn' : undefined}>{diff}</td>
              <td className={diff > 5 ? 'sf-warn' : 'sf-rate'}>
                {diff > 5 ? <><ExclamationCircleFilled />偏差偏大</> : <><CheckCircleFilled />一致</>}
              </td>
            </tr>
          })}
        </tbody>
      </table>
      <div className="sf-pass"><CheckCircleFilled />加权得分 86.4 · 计算结果校验通过，无算术偏差</div>
    </div>

    <div className="sf-grid">
      <div className="sf-panel sf-panel-alert">
        <div className="sf-panel-title">需人工复核</div>
        <div className="sf-gap">
          <ExclamationCircleFilled />
          <div><b>类似业绩 · 极差 13 分</b><small>评委 2 给出 78，其余两位 90 / 91</small></div>
        </div>
        <p className="sf-panel-note">系统只提示偏差，不改分、不定性；是否复核由评标委员会决定。</p>
      </div>
      <div className="sf-panel">
        <div className="sf-panel-title">本阶段定位</div>
        <ul className="sf-checks">
          <Check>评分来自评审系统，不由视频推断</Check>
          <Check>视频仅提供过程见证</Check>
          <Check>偏差提示留痕，可回溯</Check>
        </ul>
        <p className="sf-panel-note">该环节依赖评审数据，检测系统不参与打分。</p>
      </div>
    </div>
  </div>
}

const GAP_LOG = [
  { time: '14:22', cam: 'cam3', type: '画面遮挡', duration: '10 秒', action: '已写入归档' },
  { time: '15:08', cam: 'cam7', type: '视频断流', duration: '6 秒', action: '已写入归档' },
]

export function BlockReport() {
  return <div className="sf-stack">
    <div className="sf-grid">
      <div className="sf-panel">
        <div className="sf-panel-title">报告与签署</div>
        <div className="sf-kv-list">
          <div className="sf-kv"><span>评标报告</span><b className="sf-state-ok">已生成</b></div>
          <div className="sf-kv"><span>电子签署</span><b className="sf-state-ok">已签署（3/3）</b></div>
          <div className="sf-kv"><span>资料交接</span><b className="sf-state-ok">已完成</b></div>
        </div>
      </div>
      <div className="sf-panel sf-panel-alert">
        <div className="sf-panel-title">S10 见证缺口记录</div>
        <div className="sf-gap">
          <ExclamationCircleFilled />
          <div><b>本场共 2 处见证缺口</b><small>合计 16 秒 · 均已写入归档</small></div>
        </div>
        <p className="sf-panel-note">缺口如实记录，不因影响观感而隐去。</p>
      </div>
    </div>

    <div className="sf-panel">
      <div className="sf-panel-title">见证缺口明细 · 与录像同归档</div>
      <table className="sf-table">
        <thead>
          <tr><th>时间</th><th>机位</th><th>类型</th><th>时长</th><th>处理</th></tr>
        </thead>
        <tbody>
          {GAP_LOG.map(row => <tr key={row.time}>
            <td>{row.time}</td>
            <td>{row.cam}</td>
            <td className="sf-warn">{row.type}</td>
            <td>{row.duration}</td>
            <td className="sf-state-ok">{row.action}</td>
          </tr>)}
        </tbody>
      </table>
      <p className="sf-panel-note">缺口本身不是问题，隐瞒才是——记录随报告一并归档，供事后复核。</p>
    </div>
  </div>
}

const QA_RULES = [
  { name: 'S02 手机使用', version: 'v1.2', hits: '8', state: '已确认' },
  { name: 'S03 异常离席', version: 'v1.1', hits: '11', state: '已确认' },
  { name: 'S06 倾向性发言', version: 'v1.0', hits: '3', state: '待复核' },
  { name: 'S10 画面异常', version: 'v1.1', hits: '1', state: '已确认' },
]

export function BlockOfflineQa() {
  return <div className="sf-stack">
    <div className="sf-stats">
      <div className="sf-stat"><b>6.5</b><span>小时录像</span></div>
      <div className="sf-stat"><b>4</b><span>路机位</span></div>
      <div className="sf-stat"><b>23</b><span>处发现</span></div>
      <div className="sf-stat is-ok"><b>19</b><span>已确认</span></div>
      <div className="sf-stat is-warn"><b>4</b><span>待复核</span></div>
    </div>

    <div className="sf-grid">
      <div className="sf-panel">
        <div className="sf-panel-title">规则与模型版本 · 命中统计</div>
        <table className="sf-table">
          <thead>
            <tr><th>规则</th><th>版本</th><th>命中</th><th>确认状态</th></tr>
          </thead>
          <tbody>
            {QA_RULES.map(rule => <tr key={rule.name}>
              <td>{rule.name}</td>
              <td>{rule.version}</td>
              <td>{rule.hits}</td>
              <td className={rule.state === '待复核' ? 'sf-warn' : 'sf-state-ok'}>{rule.state}</td>
            </tr>)}
          </tbody>
        </table>
      </div>
      <div className="sf-panel">
        <div className="sf-panel-title">证据完整性校验</div>
        <div className="sf-kv-list">
          <Kv label="证据文件" value="23 个" />
          <Kv label="SHA-256 一致" value="23 / 23" strong />
          <Kv label="拍摄时间连续" value="无缺口" />
          <Kv label="最近校验" value="2026-10-31 17:20" />
        </div>
        <p className="sf-panel-note">逐个文件比对入库哈希，任一处被改动即校验失败。</p>
      </div>
    </div>

    <div className="sf-qa-list">
      <div className="sf-qa-item"><span className="sf-qa-no">1</span><span className="sf-qa-name">项目时间轴</span><span className="sf-qa-state"><CheckCircleFilled />已生成</span></div>
      <div className="sf-qa-item"><span className="sf-qa-no">2</span><span className="sf-qa-name">人工确认清单</span><span className="sf-qa-state"><CheckCircleFilled />已生成</span></div>
      <div className="sf-qa-item"><span className="sf-qa-no">3</span><span className="sf-qa-name">规则与模型版本</span><span className="sf-qa-state"><CheckCircleFilled />已生成</span></div>
      <div className="sf-qa-item"><span className="sf-qa-no">4</span><span className="sf-qa-name">证据校验报告</span><span className="sf-qa-state"><CheckCircleFilled />已生成</span></div>
    </div>
  </div>
}
