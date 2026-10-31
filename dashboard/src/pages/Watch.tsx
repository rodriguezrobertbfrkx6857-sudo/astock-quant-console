import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { EmptyState, MetricStrip, Notice, PageHeader, Panel, StatusChip, Table, TechnicalDetails, fmt, pct, secondaryButton } from '../ui'

function firstArray(data: any, names: string[]) {
  for (const name of names) if (Array.isArray(data?.[name])) return data[name]
  return []
}

export default function Watch() {
  const [data, setData] = useState<any>(null)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)

  const load = () => {
    setRefreshing(true)
    fetch('/api/v2/watch?limit=100').then(response => response.json()).then(setData).catch((reason: Error) => setError(reason.message)).finally(() => setRefreshing(false))
  }
  useEffect(() => { load() }, [])

  const ticks = useMemo(() => firstArray(data, ['ticks', 'quotes', 'watchlist', 'watch_list']), [data])
  const signals = useMemo(() => firstArray(data, ['signals', 'signal_matrix', 'latest_signals']), [data])
  const alerts = useMemo(() => firstArray(data, ['alerts']), [data])
  const monitoredPlans = useMemo(() => firstArray(data, ['monitored_plans']), [data])
  if (error) return <Notice tone="danger" title="Watch 状态加载失败">{error}<button type="button" className="text-button" onClick={load}>重新加载</button></Notice>
  if (!data) return <div className="page-loading">正在读取 Watch 和 Provider 状态…</div>

  const replay = data.historical_replay || {}
  const feedState = data.live_feed_status || replay.live_feed_status || data.provider_status || 'PENDING_VENDOR'
  const marketPhase = data.market_phase || data.market_status || replay.market_phase || '非交易时段 / 待验证'
  const ai = data.ai || {}

  return (
    <div>
      <PageHeader eyebrow="WATCH & AI / SIGNAL MONITOR" title="监控与 AI" description="实时行情、分钟图、信号矩阵和解释摘要分开表达；历史 replay 不会伪装成 live tick。" meta={<div>{data.as_of || data.updated_at || '—'}</div>} actions={<button type="button" style={secondaryButton} onClick={load} disabled={refreshing}>{refreshing ? '刷新中…' : '刷新状态'}</button>} />
      <MetricStrip items={[
        { label: 'Feed', value: <StatusChip value={feedState} raw />, hint: 'Provider' },
        { label: 'Market phase', value: marketPhase, hint: '真实时段状态' },
        { label: 'Last Tick', value: data.last_tick || data.last_tick_at || '—', hint: ticks.length ? '真实 tick' : '未返回 tick' },
        { label: 'Signals', value: fmt(signals.length || data.signal_count, 0), hint: 'API 返回' },
        { label: 'Alerts', value: fmt(alerts.length, 0), hint: '当前 alert' },
        { label: 'Trading', value: data.live_trading ? 'ON' : 'OFF', hint: '本地研究边界' },
      ]} />

      {(feedState !== 'READY' && feedState !== 'REAL_READY' && feedState !== 'PASS') && <Notice tone="warn" title={String(feedState).includes('DEGRADED') ? '实时供应商降级' : '实时能力等待验证'}>{data.realtime?.detail || data.message || replay.message || '非交易时段不伪造 Last Tick；日线功能仍可独立使用。'}<span className="inline-link">{marketPhase}</span></Notice>}

      <Panel eyebrow="PLAN-LINKED SCOPE" title="短线计划监控范围">
        <div className="panel-lede">只包含手工持仓与当前候选。完整实时验收通过前，计划触发保持禁用；陈旧、重复或乱序数据不会触发提示。</div>
        {monitoredPlans.length ? <Table headers={['Plan', 'Type', 'Instrument', 'Strategy', 'Target', 'State']} minWidth={900}>{monitoredPlans.slice(0, 40).map((item: any) => <tr key={item.plan_id}><td>{item.plan_id}</td><td>{item.plan_type}</td><td>{item.asset_id}</td><td>{item.strategy_version}</td><td>{item.target_session}</td><td><StatusChip value={data.realtime?.live_plan_triggers_enabled ? item.status : 'TRIGGERS_DISABLED'} raw /></td></tr>)}</Table> : <EmptyState icon="watch" title="没有当前计划监控项" detail="先在短线工作台生成候选或持仓计划；Watch 不会扩展到无关全市场标的。" />}
      </Panel>

      <div className="watch-grid">
        <Panel eyebrow="WATCH LIST" title="关注标的">
          {ticks.length ? <div className="watch-list">{ticks.slice(0, 20).map((item: any, index: number) => <div className="data-row" key={item.ts_code || item.symbol || index}><div><div className="list-item-title">{item.name || item.ts_code || item.symbol || 'Instrument'}</div><div className="list-item-meta">{item.ts_code || item.symbol || '—'} · {item.timestamp || item.updated_at || item.trade_date || '—'}</div></div><div className="watch-list-value"><strong>{item.price == null ? '—' : fmt(item.price, 3)}</strong><span className={Number(item.change_pct ?? item.pct_chg) >= 0 ? 'market-up' : 'market-down'}>{item.change_pct == null && item.pct_chg == null ? '—' : pct(item.change_pct ?? item.pct_chg)}</span></div></div>)}</div> : <EmptyState icon="watch" title="暂无真实 Watch List / tick" detail="当前响应没有返回实时序列，不用日线或历史 replay 填充关注列表。" action={<Link className="button-link secondary" to="/market">查看 EOD Market</Link>} />}
        </Panel>

        <Panel eyebrow="REALTIME / MINUTE" title="实时图表">
          {ticks.length && ticks.some((item: any) => item.timestamp || item.time) ? <EmptyState icon="chart" title="当前响应提供行情行但没有可绘制 OHLC 序列" detail="保留真实数据边界；接入合法分钟数据后由该区域渲染。" /> : <EmptyState icon="activity" title="等待交易时段与 Provider 验证" detail="日线不能冒充 tick。真实 feed 通过后再展示 Last Tick、断线和恢复。" />}
        </Panel>

        <Panel eyebrow="AI ADVISORY" title="解释摘要">
          <div className="ai-summary"><div className="ai-summary-icon">AI</div><div><div className="list-item-title">{ai.summary || ai.message || data.ai_summary || '当前没有 AI 摘要'}</div><div className="list-item-meta">Provider {ai.provider || data.ai_provider || 'DeepSeek'} · Model {ai.model || data.ai_model || '—'}</div></div></div>
          <div className="panel-lede">AI 只解释已存在的信号、事件和数据异常，不修改 Canonical Truth、正式策略或 PAPER 账本。</div>
          <Link className="inline-link" to="/system">查看 AI 与 Provider 状态 →</Link>
        </Panel>
      </div>

      <Panel eyebrow="ONE SCREEN MATRIX" title="信号矩阵" className="signal-panel">
        {signals.length ? <Table headers={['Strategy', 'Signal', 'Instrument', 'Signal time', 'Entry / reference', 'Current', 'PnL', 'Updated']} minWidth={1040}>{signals.slice(0, 100).map((item: any, index: number) => <tr key={item.signal_id || index}><td>{item.strategy_name || item.strategy_id || '—'}</td><td><StatusChip value={item.signal || item.action || item.direction || '—'} raw /></td><td>{item.ts_code || item.asset_id || item.symbol || '—'}</td><td>{item.signal_time || item.created_at || item.trade_date || '—'}</td><td>{fmt(item.entry_price ?? item.reference_price, 3)}</td><td>{fmt(item.current_price ?? item.price, 3)}</td><td className={Number(item.pnl ?? item.return) >= 0 ? 'market-up' : 'market-down'}>{item.pnl == null && item.return == null ? '—' : pct(item.pnl ?? item.return)}</td><td>{item.updated_at || '—'}</td></tr>)}</Table> : <EmptyState icon="activity" title="当前没有可展示的信号" detail="空信号与实时未验证是不同状态；原始 replay / capability 信息可在技术详情查看。" />}
      </Panel>

      <Panel eyebrow="RISK / ALERTS" title={'异常与风险 · ' + alerts.length + ' 项'} className="signal-panel">
        {alerts.length ? <Table headers={['Time', 'Instrument', 'Type', 'Severity', 'Message']} minWidth={800}>{alerts.map((item: any, index: number) => <tr key={item.alert_id || index}><td>{item.created_at || item.trade_date || '—'}</td><td>{item.ts_code || '—'}</td><td>{item.kind || item.alert_type || '—'}</td><td><StatusChip value={item.severity || item.status || 'INFO'} raw /></td><td>{item.message || item.detail || '—'}</td></tr>)}</Table> : <EmptyState icon="shield" title="暂无监控异常" detail="历史日线数据未写入 tick store。" />}
      </Panel>

      <TechnicalDetails title="Watch semantics and capabilities">
        <div className="detail-grid"><div>engine <strong>{data.engine_ready ? 'READY' : '—'}</strong></div><div>initial samples <strong>{data.realtime?.validated_sample_count || 0}</strong></div><div>30m continuity <strong>{data.realtime?.continuity_window_verified ? 'PASS' : 'PENDING'}</strong></div><div>live plan triggers <strong>{data.realtime?.live_plan_triggers_enabled ? 'ENABLED' : 'DISABLED'}</strong></div><div>replay <strong>{replay.mode || replay.status || '—'}</strong></div><div>provider <strong>{feedState}</strong></div></div>
        <div className="capability-list">{(data.capabilities || []).map((item: string) => <span className="capability" key={item}>{item}</span>)}</div>
      </TechnicalDetails>
    </div>
  )
}
