import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, EmptyState, Icon, MetricStrip, Notice, PageHeader, Panel, StatusChip, Table, TechnicalDetails, fmt, secondaryButton } from '../ui'

export default function SystemStatus() {
  const [sys, setSys] = useState<any>(null)
  const [error, setError] = useState('')
  const [includeHistory, setIncludeHistory] = useState(false)
  const [includeFullMarket, setIncludeFullMarket] = useState(false)

  const load = () => {
    const query = new URLSearchParams({ include_history: String(includeHistory), include_full_market: String(includeFullMarket) })
    setError('')
    fetch('/api/v2/system?' + query).then(response => response.json()).then(setSys).catch((reason: Error) => setError(reason.message))
  }
  useEffect(() => { load() }, [includeHistory, includeFullMarket])

  if (error) return <Notice tone="danger" title="System 状态加载失败">{error}<button type="button" className="text-button" onClick={load}>重新加载</button></Notice>
  if (!sys) return <div className="page-loading">正在读取 Runtime、Provider 和安全边界…</div>

  const counts = sys.runtime_status_counts || {}
  const operations = sys.operational_status || []
  const schedule = sys.scheduler
  const aiBudget = sys.ai_budget || {}
  const currentScope = sys.current_data_scope || {}

  return (
    <div>
      <PageHeader eyebrow="SYSTEM / RUNTIME & EVIDENCE" title="系统状态" description="工程、数据和审计信息的次级入口。这里的健康状态不会替代业务日期或 PAPER 资格。" actions={<button type="button" style={secondaryButton} onClick={load}><Icon name="refresh" size={14} /> 刷新</button>} meta={<div>{sys.as_of || sys.updated_at || '—'}</div>} />
      <MetricStrip items={[
        { label: 'System mode', value: sys.system_mode || '—', hint: '本地运行模式' },
        { label: 'Persisted jobs', value: fmt(sys.persisted_job_count, 0), hint: 'Runtime snapshot' },
        { label: 'Current ETF EOD', value: currentScope.latest_complete_eod || '—', hint: '真实完整日' },
        { label: 'Live Watch', value: <StatusChip value={sys.live_watch_feed || 'PENDING_VENDOR'} raw />, hint: 'Provider 独立状态' },
        { label: 'Current PAPER', value: <StatusChip value={currentScope.current_paper_lookback_ready ? 'READY' : 'WAITING_DATA'} />, hint: '资格 / 回看窗口' },
        { label: 'AI', value: <StatusChip value={sys.deepseek || 'PENDING_KEY'} raw />, hint: sys.ai_model || 'DeepSeek' },
      ]} />

      <Notice tone="ok" title="安全边界">REAL_TRADING_ENABLED=FALSE · REAL_ORDER_ENABLED=FALSE · BROKER_ACCESS=FALSE · ACCOUNT_ACCESS=FALSE · LOCAL_ONLY=TRUE</Notice>

      <div className="system-link-grid">
        <Link className="system-link" to="/data-health"><Icon name="database" size={18} /><div><strong>Data Health</strong><span>published coverage / freshness</span></div></Link>
        <Link className="system-link" to="/data"><Icon name="layers" size={18} /><div><strong>Data Provider</strong><span>catalog / samples / provenance</span></div></Link>
        <Link className="system-link" to="/events"><Icon name="events" size={18} /><div><strong>Events</strong><span>PIT event store / research stats</span></div></Link>
        <a className="system-link" href="#technical-details"><Icon name="shield" size={18} /><div><strong>Evidence</strong><span>runtime and publication details</span></div></a>
      </div>

      <div className="two-column">
        <Panel eyebrow="SCHEDULER" title="运行调度">
          {schedule ? <div className="detail-grid"><div>Phase <StatusChip value={schedule.phase || '—'} raw /></div><div>Local time <strong>{schedule.local_time || '—'}</strong></div><div>Watch window <strong>{schedule.watch_enabled ? '09:30–11:30 / 13:00–15:00' : 'PAUSE / OVERNIGHT / EOD'}</strong></div><div>Feed <StatusChip value={schedule.live_feed_status || '—'} raw /></div><div>Calendar <StatusChip value={schedule.calendar?.known ? 'VERIFIED' : 'BLOCKED_DATA'} /></div><div>Calendar source <strong>{schedule.calendar?.source || '—'}</strong></div><div>Calendar through <strong>{schedule.calendar?.latest_date || '—'}</strong></div><div>Snapshot <strong>{schedule.calendar?.manifest_hash?.slice(0, 16) || '—'}</strong></div></div> : <EmptyState icon="calendar" title="尚无 Scheduler 快照" detail="启动 Runtime 后由现有服务写入。" />}
        </Panel>
        <Panel eyebrow="RUNTIME" title="状态分布">
          {Object.keys(counts).length ? <><div className="runtime-counts">{Object.entries(counts).map(([name, count]) => <div className="runtime-count" key={name}><StatusChip value={name} raw /><strong>{String(count)}</strong></div>)}</div><div className="panel-lede">当前视图绑定 {sys.runtime_scope?.business_date || '—'} / {sys.runtime_scope?.snapshot?.slice(0, 16) || '—'}；历史累计 {fmt(sys.persisted_job_count, 0)} 项仅在技术详情保留。</div></> : <EmptyState icon="activity" title="当前快照暂无 Runtime 状态" detail="历史任务未被删除，可在技术详情查看累计分布。" />}
        </Panel>
      </div>

      <Panel eyebrow="OPERATIONAL COMPONENTS" title="服务组件" className="system-operations">
        <div className="action-row"><button type="button" style={secondaryButton} onClick={() => setIncludeHistory(value => !value)}>{includeHistory ? '隐藏历史审计' : '显示历史审计'}</button><button type="button" style={secondaryButton} onClick={() => setIncludeFullMarket(value => !value)}>{includeFullMarket ? '隐藏全市场分支' : '显示全市场分支'}</button></div>
        {operations.length ? <Table headers={['Component / scope', 'Status', 'Severity', 'Reason / impact / next action', 'Freshness', 'Evidence']} minWidth={1180}>{operations.map((row: any) => <tr key={row.component}><td><strong>{row.component}</strong><div className="list-item-meta">{row.scope}</div></td><td><StatusChip value={row.status || '—'} raw /></td><td><StatusChip value={row.severity || 'INFO'} raw /></td><td><strong>{row.reason || '—'}</strong><div>{row.detail || '—'}</div><div className="list-item-meta">Impact: {row.impact || '—'} · Next: {row.next_action || '—'}</div></td><td><StatusChip value={row.freshness || '—'} raw /><div className="list-item-meta">{row.updated_at || '—'}</div></td><td className="mono">{row.evidence_ref || '—'}</td></tr>)}</Table> : <EmptyState icon="system" title="当前筛选下没有组件" detail="历史审计和全市场可选分支默认不作为当前告警。" />}
      </Panel>

      <Panel eyebrow="AI ADVISORY" title="DeepSeek 状态">
        <div className="detail-grid"><div>Provider <strong>{sys.ai_provider || 'deepseek'}</strong></div><div>Model <strong>{sys.ai_model || '—'}</strong></div><div>Status <StatusChip value={sys.deepseek || 'PENDING_KEY'} raw /></div><div>Calls today <strong>{aiBudget.calls_today ?? '—'} / {aiBudget.remaining_calls_today ?? '—'}</strong></div></div>
        <div className="panel-lede">AI 只提供解释性文本，不写入 Canonical、研究结论、PAPER 或订单；Key 不可用只影响 AI 功能。</div>
      </Panel>

      <TechnicalDetails title="Manifest, evidence and technical details" open={false}>
        <div id="technical-details"><pre className="json-block">{JSON.stringify({ system: sys, runtime_status_counts_current: counts, runtime_status_counts_all_history: sys.runtime_status_counts_all_history, current_data_scope: currentScope }, null, 2)}</pre></div>
      </TechnicalDetails>
    </div>
  )
}
