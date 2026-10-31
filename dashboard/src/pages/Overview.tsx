import { useEffect, useMemo, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import { Link } from 'react-router-dom'
import { ChartFrame, EmptyState, Icon, MetricStrip, Notice, PageHeader, Panel, StatusChip, TechnicalDetails, fmt, metricScalar, pct } from '../ui'

type AnyRecord = Record<string, any>

function firstDefined(...values: any[]) {
  return values.find(value => value !== undefined && value !== null && value !== '')
}

function safeArray(value: unknown): any[] {
  return Array.isArray(value) ? value : []
}

function seriesFrom(data: AnyRecord): any[] {
  const candidates = [
    data.current_paper?.nav_series,
    data.paper_summary?.nav_series,
    data.paper?.nav_series,
    data.nav_series,
  ]
  return candidates.find(candidate => Array.isArray(candidate) && candidate.length > 0) || []
}

function latestValue(series: any[], field: string) {
  const row = series[series.length - 1]
  return row ? firstDefined(row[field], row.value, row.nav) : undefined
}

function HomeChart({ series }: { series: any[] }) {
  const rows = series
    .map(row => ({ date: row.trade_date || row.on || row.date, value: Number(row.nav ?? row.value ?? row.close) }))
    .filter(row => row.date && Number.isFinite(row.value))
  if (!rows.length) {
    return <EmptyState icon="chart" title="当前没有可用的 PAPER NAV 序列" detail="首页不会用历史回测或旧账户替代当前前向 PAPER。" action={<Link className="button-link" to="/paper">打开 PAPER</Link>} />
  }
  return <ReactECharts notMerge style={{ height: 360, width: '100%' }} option={{
    animation: false,
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', backgroundColor: '#171e27', borderColor: '#3a4654', textStyle: { color: '#f2f4f7' } },
    grid: { left: 52, right: 18, top: 18, bottom: 32 },
    xAxis: { type: 'category', data: rows.map(row => row.date), boundaryGap: false, axisLabel: { color: '#8c97a6', fontSize: 10 }, axisLine: { lineStyle: { color: '#27313d' } }, axisTick: { show: false } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: '#8c97a6', fontSize: 10 }, splitLine: { lineStyle: { color: '#27313d' } } },
    series: [{ type: 'line', data: rows.map(row => row.value), smooth: 0.12, showSymbol: rows.length < 32, symbolSize: 5, lineStyle: { color: '#f2c94c', width: 2 }, itemStyle: { color: '#f2c94c' }, areaStyle: { color: 'rgba(242, 201, 76, 0.09)' } }],
  }} />
}

function SummaryRow({ label, value, detail }: { label: string; value: React.ReactNode; detail?: React.ReactNode }) {
  return <div className="data-row"><span className="data-row-label">{label}</span><span className="data-row-value">{value}{detail && <span className="metric-hint">{detail}</span>}</span></div>
}

function dataReadinessStatus(data: AnyRecord): { label: string; tone: 'ok' | 'warn' | 'danger' | 'info'; detail: string } {
  const scope = data.current_data_scope || {}
  const shortTerm = data.short_term || {}
  const hasGap = shortTerm.latest_scan?.coverage?.missing_open_sessions?.length > 0
  if (!scope.current_eod_ready) return { label: '等待数据', tone: 'warn', detail: '当前范围仍有数据依赖' }
  if (hasGap) return { label: '数据有缺口', tone: 'warn', detail: `缺少 ${shortTerm.latest_scan.coverage.missing_open_sessions.join(', ')}` }
  return { label: '可用', tone: 'ok', detail: `数据 through ${scope.latest_complete_eod || '—'}` }
}

export default function Overview() {
  const [data, setData] = useState<AnyRecord | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    fetch('/api/v2/overview')
      .then(response => response.json())
      .then(setData)
      .catch((reason: Error) => setError(reason.message))
  }, [])

  const series = useMemo(() => data ? seriesFrom(data) : [], [data])
  if (error) return <Notice tone="danger" title="首页数据加载失败">{error}<button type="button" className="text-button" onClick={() => window.location.reload()}>重新加载</button></Notice>
  if (!data) return <div className="page-loading"><Icon name="activity" size={22} />正在读取状态…</div>

  const scope = data.current_data_scope || {}
  const paper = data.current_paper || {}
  const paperSummary = data.paper_summary || {}
  const strategySummary = data.strategy_summary || {}
  const watch = data.watch || {}
  const ai = data.ai || {}
  const shortTerm = data.short_term || {}
  const paperState = firstDefined(paper.status, data.current_paper_status, data.paper_runnable ? 'READY' : 'WAITING_DATA')
  const paperNav = firstDefined(paper.nav, paperSummary.nav, latestValue(series, 'nav'))
  const paperReturn = firstDefined(paper.total_return, paperSummary.total_return, data.current_paper_return)
  const positions = firstDefined(paper.positions?.length, paperSummary.positions_count, paperSummary.position_count)
  const signalCount = firstDefined(watch.signal_count, watch.signals?.length, data.signal_count)
  const latestDate = firstDefined(scope.latest_complete_eod, data.latest_complete_market_date, data.latest_data_date)
  const businessDate = firstDefined(scope.business_date, data.current_business_date, data.business_date)
  const scopeReady = Boolean(scope.current_eod_ready)
  const paperReady = Boolean(paper.usable || data.paper_runnable)
  const paperWaiting = !paperReady || /WAITING|PREPARED/i.test(String(paperState))
  const aiStatus = firstDefined(ai.status, ai.state, ai.deepseek, data.deepseek, 'PENDING_KEY')
  const dataReadiness = dataReadinessStatus(data)
  const scanStatus = shortTerm.latest_scan?.status || '未扫描'
  const candidateCount = shortTerm.latest_scan?.candidates?.length || 0
  const holdingCount = shortTerm.holding_count || 0
  const targetSession = shortTerm.latest_scan?.target_session || '—'
  const watchStatus = shortTerm.watch?.status || watch.live_feed_status || watch.status || 'PENDING_VENDOR'
  const triggersOn = shortTerm.live_triggers_enabled === true

  return (
    <div>
      <PageHeader
        eyebrow="今日工作台"
        title="每日决策总览"
        description="先确认数据和运行状态，再进入持仓和候选。回答：今天数据能用吗？我现在该做什么？持仓需要关注什么？"
        meta={<><div>AS OF {data.as_of || data.timestamp || '—'}</div><div className="page-meta-inline">本地运行 · 不连接券商</div></>}
        actions={<><Link className="button-link" to="/holdings">我的持仓</Link><Link className="button-link secondary" to="/opportunities">明日机会</Link></>}
      />

      {/* 核心问题回答区 */}
      <div className="daily-questions-strip">
        <div className={'daily-question daily-question-' + dataReadiness.tone}>
          <div className="daily-question-icon">
            <Icon name={dataReadiness.tone === 'ok' ? 'shield' : 'activity'} size={18} />
          </div>
          <div className="daily-question-body">
            <div className="daily-question-label">今天数据能用吗？</div>
            <div className="daily-question-answer">{dataReadiness.label}</div>
            <div className="daily-question-detail">{dataReadiness.detail}</div>
          </div>
        </div>
        <div className="daily-question daily-question-info">
          <div className="daily-question-icon"><Icon name="briefcase" size={18} /></div>
          <div className="daily-question-body">
            <div className="daily-question-label">持仓需要关注吗？</div>
            <div className="daily-question-answer">{holdingCount > 0 ? `${holdingCount} 条持仓` : '无持仓'}</div>
            <div className="daily-question-detail">{holdingCount > 0 ? '查看次日行动计划' : '可手工录入或导入'}</div>
          </div>
        </div>
        <div className="daily-question daily-question-info">
          <div className="daily-question-icon"><Icon name="activity" size={18} /></div>
          <div className="daily-question-body">
            <div className="daily-question-label">明日候选验证了吗？</div>
            <div className="daily-question-answer">{candidateCount} 个候选</div>
            <div className="daily-question-detail">{scanStatus === 'OBSERVATION_ONLY_DATA_GAP' ? '仅供观察，数据有缺口' : scanStatus === 'NO_CANDIDATES' ? '零候选是合法结果' : `目标 ${targetSession}`}</div>
          </div>
        </div>
        <div className={'daily-question daily-question-' + (triggersOn ? 'ok' : 'warn')}>
          <div className="daily-question-icon"><Icon name="watch" size={18} /></div>
          <div className="daily-question-body">
            <div className="daily-question-label">盘中触发可用吗？</div>
            <div className="daily-question-answer">{triggersOn ? '已启用' : '未启用'}</div>
            <div className="daily-question-detail">{watchStatus === 'DEGRADED_VENDOR' ? '行情供应商降级' : watchStatus === 'WATCH_FULLY_VERIFIED' ? '全验收通过' : '分钟验收未完成'}</div>
          </div>
        </div>
      </div>

      {/* 下一步操作建议 */}
      <Panel eyebrow="下一步" title="现在该做什么">
        <div className="next-steps-grid">
          {holdingCount === 0 && <Link className="next-step" to="/holdings">
            <Icon name="briefcase" size={20} />
            <div><div className="next-step-title">录入持仓</div><div className="next-step-detail">手工录入或 CSV 预览后保存，生成次日计划</div></div>
          </Link>}
          {holdingCount > 0 && <Link className="next-step" to="/holdings">
            <Icon name="briefcase" size={20} />
            <div><div className="next-step-title">查看持仓计划</div><div className="next-step-detail">{holdingCount} 条持仓的次日行动计划和风险线</div></div>
          </Link>}
          <Link className="next-step" to="/opportunities">
            <Icon name="activity" size={20} />
            <div><div className="next-step-title">查看明日机会</div><div className="next-step-detail">{candidateCount > 0 ? `${candidateCount} 个观察候选` : '运行扫描寻找候选'}</div></div>
          </Link>
          {!scopeReady && <Link className="next-step" to="/system">
            <Icon name="system" size={20} />
            <div><div className="next-step-title">检查数据状态</div><div className="next-step-detail">数据未就绪，查看详情</div></div>
          </Link>}
          {paperWaiting && <Link className="next-step" to="/paper">
            <Icon name="paper" size={20} />
            <div><div className="next-step-title">PAPER 等待中</div><div className="next-step-detail">等待首个前向交易日</div></div>
          </Link>}
          <Link className="next-step" to="/review">
            <Icon name="calendar" size={20} />
            <div><div className="next-step-title">复盘记录</div><div className="next-step-detail">对比计划与实际结果</div></div>
          </Link>
        </div>
      </Panel>

      {/* 当前不可用功能说明 */}
      {(!scopeReady || paperWaiting || !triggersOn) && (
        <Panel eyebrow="当前限制" title="暂不可用功能">
          <div className="limitations-list">
            {!scopeReady && <div className="limitation-item">
              <StatusChip value="WAITING_DATA" />
              <div><strong>数据未就绪</strong><span>{scope.message || '当前范围仍有数据依赖，市场页和部分指标暂不可用'}</span></div>
            </div>}
            {paperWaiting && <div className="limitation-item">
              <StatusChip value="PREPARED_WAITING" />
              <div><strong>PAPER 等待首个前向会话</strong><span>账户已建立，等待目标交易日 EOD 数据到达后才会形成 NAV</span></div>
            </div>}
            {!triggersOn && <div className="limitation-item">
              <StatusChip value="TRIGGERS_DISABLED" />
              <div><strong>盘中触发关闭</strong><span>行情供应商返回陈旧数据（最新 {watchStatus === 'DEGRADED_VENDOR' ? '2026-08-31' : '—'}），分钟验收未完成</span></div>
            </div>}
            {data.short_term?.latest_scan?.coverage?.missing_open_sessions?.length > 0 && <div className="limitation-item">
              <StatusChip value="OBSERVATION_ONLY_DATA_GAP" />
              <div><strong>数据缺口</strong><span>缺少 {data.short_term.latest_scan.coverage.missing_open_sessions.join('、')} 交易日数据，候选仅供观察</span></div>
            </div>}
          </div>
        </Panel>
      )}

      <div className="home-scope-strip">
        <div className="scope-item"><span>最新完整 EOD</span><strong>{latestDate || '—'}</strong><small>{latestDate ? '按真实发布数据' : '未提供'}</small></div>
        <div className="scope-item"><span>当前业务日</span><strong>{businessDate || '—'}</strong><small>{data.historical_replay ? '历史回放上下文' : '当前运行上下文'}</small></div>
        <div className="scope-item"><span>短线计划目标</span><strong>{targetSession}</strong><small>{scanStatus}</small></div>
        <div className="scope-item"><span>Watch</span><strong><StatusChip value={watchStatus} /></strong><small>{shortTerm.watch?.validated_sample_count || 0} 验证样本</small></div>
      </div>

      <MetricStrip className="home-metrics" items={[
        { label: 'PAPER NAV', value: paperNav == null ? '—' : fmt(paperNav, 2), hint: paperReady ? (paper.latest_execution_date || '当前账户') : '等待首个会话', mono: true },
        { label: '最近收益', value: paperReturn == null ? '—' : pct(paperReturn), hint: paperReturn == null ? '数据未提供' : '当前账户口径', tone: paperReturn == null ? 'neutral' : Number(metricScalar(paperReturn)) >= 0 ? 'up' : 'down' },
        { label: '当前仓位', value: positions == null ? '—' : fmt(positions, 0), hint: '当前有效账户', mono: true },
        { label: '持仓', value: fmt(holdingCount, 0), hint: '手工录入', mono: true },
        { label: '候选', value: fmt(candidateCount, 0), hint: scanStatus === 'OBSERVATION_ONLY_DATA_GAP' ? '仅供观察' : '扫描结果' },
        { label: '风险', value: firstDefined(paper.risk_status, paperSummary.risk_status, data.risk?.status, '—') === '—' ? '—' : <StatusChip value={firstDefined(paper.risk_status, paperSummary.risk_status, data.risk?.status, '—')} />, hint: '未提供独立风险值' },
      ]} />

      <div className="home-grid home-main-grid">
        <ChartFrame eyebrow="表现" title={series.length ? 'PAPER NAV' : 'PAPER NAV · 等待中'} meta={<>{paper.latest_execution_date || latestDate || '—'} · {series.length || 0} 点</>}>
          <HomeChart series={series} />
        </ChartFrame>
        <Panel eyebrow="当前范围" title="系统状态">
          <SummaryRow label="数据状态" value={<StatusChip value={dataReadiness.tone === 'ok' ? 'READY' : 'WAITING_DATA'} />} detail={dataReadiness.detail} />
          <SummaryRow label="PAPER" value={<StatusChip value={paperState} />} detail={paper.data_date || paper.latest_execution_date || '—'} />
          <SummaryRow label="Watch" value={<StatusChip value={watchStatus} />} detail={`${shortTerm.watch?.validated_sample_count || 0} 样本`} />
          <SummaryRow label="AI / DeepSeek" value={<StatusChip value={aiStatus} />} />
          <SummaryRow label="盘中触发" value={triggersOn ? '已启用' : '未启用'} detail="分钟验收前关闭" />
          <div className="panel-actions"><Link className="inline-link" to="/system">打开技术状态 →</Link></div>
        </Panel>
      </div>

      <div className="home-grid">
        <Panel eyebrow="策略摘要" title="当前策略" className="home-list-panel">
          {safeArray(strategySummary.items || strategySummary.strategies).slice(0, 5).map((item: AnyRecord, index: number) => (
            <div className="data-row" key={item.strategy_id || item.id || index}>
              <div><div className="list-item-title">{item.name || item.strategy_name || item.strategy_id || '策略'}</div><div className="list-item-meta">{item.strategy_id || item.type || '—'}</div></div>
              <div className="list-row-right"><StatusChip value={item.status || item.qualification || item.research_status || '—'} /><span className={Number(metricScalar(item.total_return)) >= 0 ? 'market-up' : 'market-down'}>{item.total_return == null ? '—' : pct(item.total_return)}</span></div>
            </div>
          ))}
          {!safeArray(strategySummary.items || strategySummary.strategies).length && <EmptyState icon="layers" title="暂无首页策略摘要" detail="进入策略研究查看真实注册和评估状态。" action={<Link className="button-link secondary" to="/research">打开策略研究</Link>} />}
          <div className="panel-actions"><Link className="inline-link" to="/research">查看全部策略 →</Link></div>
        </Panel>
        <Panel eyebrow="AI 解释" title="DeepSeek 辅助" className="home-list-panel">
          <div className="ai-summary">
            <div className="ai-summary-icon"><Icon name="spark" size={19} /></div>
            <div><div className="list-item-title">{ai.summary || ai.message || ai.advisory || 'AI 仅提供解释，不写入正式策略或账本。'}</div><div className="list-item-meta">Provider: {ai.provider || data.ai_provider || 'DeepSeek'} · Model: {ai.model || data.ai_model || '—'}</div></div>
          </div>
          <div className="panel-actions"><Link className="inline-link" to="/watch">查看 Watch & AI →</Link></div>
        </Panel>
      </div>

      <Panel eyebrow="服务概览" title="模块状态" className="system-pulse-panel">
        <div className="module-grid">
          {Object.entries(data.module_status || {}).filter(([name]) => name !== 'PRODUCT').slice(0, 8).map(([name, item]: [string, any]) => <div className="module-card" key={name}><div className="module-card-title">{name.replace(/_/g, ' ')}</div><StatusChip value={item.status || '—'} /><div className="module-card-meta">{item.latest_execution_date || item.latest_date || item.freshness || '无日期型输入'}</div></div>)}
        </div>
        <TechnicalDetails title="Technical Details">
          <div className="detail-grid"><div>publication <strong>{data.publication_id || '—'}</strong></div><div>snapshot <strong>{scope.publication_snapshot || '—'}</strong></div><div>scope <strong>{scope.scope || data.current_data_scope?.status || '—'}</strong></div><div>historical replay <strong>{data.historical_replay ? 'TRUE' : 'FALSE'}</strong></div></div>
          <pre className="json-block">{JSON.stringify({ evidence_refs: data.evidence_refs, boundaries: data.boundaries, daily_operation: data.daily_operation }, null, 2)}</pre>
        </TechnicalDetails>
      </Panel>
    </div>
  )
}
