import { useEffect, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import { Link } from 'react-router-dom'
import { Badge, ChartFrame, EmptyState, Icon, MetricStrip, Notice, PageHeader, Panel, StatusChip, Table, TechnicalDetails, fmt, newId, pct, postJson, secondaryButton } from '../ui'

interface Position {
  ts_code: string
  name?: string
  shares: number
  avg_cost: number
  last_price?: number
  available_shares: number
}

interface Account {
  strategy_id: string
  status: string
  account_id?: string
  generation_id?: string | null
  data_profile?: string | null
  cash: number
  nav: number
  initial_cash: number
  total_return: number | null
  updated_at?: string
  started_at?: string
  last_trade_date?: string | null
  positions: Position[]
  nav_series: { trade_date: string; nav: number }[]
  provenance?: string
  legacy_status?: string
  execution_allowed?: boolean
  block_reasons?: string[]
  signals?: any[]
  orders?: any[]
  fills?: any[]
  ledger?: any[]
  reconciliation_status?: string
}

type PaperData = {
  accounts: Account[]
  state?: string
  current_paper_status?: string
  eligibility_message?: string
  paper_engine_ready?: boolean
  paper_usable?: boolean
  execution_data_freshness?: string
  execution_data_status?: string
  latest_execution_date?: string | null
  legacy_accounts?: Account[]
  v2_accounts?: Account[]
  eligible_strategies?: any[]
  current_paper_plans?: any[]
  current_data_scope?: any
  latest_supported_date?: string | null
  operation_policy?: any
  account_summary?: { v2_operational?: number; v2_audit_only?: number }
}

function AccountChart({ account }: { account: Account }) {
  const rows = Array.isArray(account.nav_series) ? account.nav_series : []
  if (!rows.length) return <EmptyState icon="chart" title="暂无 NAV 序列" detail="不使用其他账户或回测曲线替代当前账户。" />
  return <ReactECharts notMerge style={{ height: 250, width: '100%' }} option={{
    animation: false,
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', backgroundColor: '#171e27', borderColor: '#3a4654', textStyle: { color: '#f2f4f7' } },
    grid: { left: 52, right: 16, top: 16, bottom: 32 },
    xAxis: { type: 'category', data: rows.map(row => row.trade_date), axisLabel: { color: '#8c97a6', fontSize: 10 }, axisLine: { lineStyle: { color: '#27313d' } } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: '#8c97a6', fontSize: 10 }, splitLine: { lineStyle: { color: '#27313d' } } },
    series: [{ type: 'line', data: rows.map(row => row.nav), showSymbol: rows.length < 30, symbolSize: 5, lineStyle: { width: 2, color: '#f2c94c' }, itemStyle: { color: '#f2c94c' }, areaStyle: { color: 'rgba(242,201,76,.08)' } }],
  }} />
}

function AccountCard({ account, details, onDetails }: { account: Account; details?: any; onDetails: () => void }) {
  const returnTone = account.total_return == null ? 'metric-neutral' : account.total_return >= 0 ? 'metric-up' : 'metric-down'
  return (
    <Panel className="account-card">
      <div className="account-card-head"><div><div className="eyebrow">{account.data_profile === 'ETF3_FORWARD_CURRENT' ? 'FORWARD PAPER' : 'HISTORICAL PAPER'}</div><div className="account-title">{account.strategy_id}</div><div className="account-id">{account.account_id || account.generation_id || '—'}</div></div><div className="account-status"><StatusChip value={account.status || '—'} raw /><span className="metric-hint">{account.updated_at || '—'}</span></div></div>
      {account.data_profile !== 'ETF3_FORWARD_CURRENT' && <Notice tone="warn" title="历史代际 / 只读">不追赶当前日期，不参与当前 PAPER 总览，也不能续跑。</Notice>}
      <MetricStrip items={[{ label: 'NAV', value: account.nav == null ? '—' : fmt(account.nav, 2), mono: true }, { label: 'Return', value: pct(account.total_return), tone: returnTone === 'metric-up' ? 'up' : returnTone === 'metric-down' ? 'down' : 'neutral' }, { label: 'Cash', value: account.cash == null ? '—' : fmt(account.cash, 2), mono: true }, { label: 'Positions', value: fmt(account.positions?.length, 0), mono: true }]} />
      <ChartFrame eyebrow="ACCOUNT CURVE" title="PAPER NAV" meta={<>{account.last_trade_date || '—'} · {account.nav_series?.length || 0} points</>}><AccountChart account={account} /></ChartFrame>
      {account.positions?.length ? <div className="account-positions"><div className="subsection-title">Positions</div><Table headers={['Code', 'Name', 'Shares', 'Avg cost', 'Last price', 'Available']} minWidth={700}>{account.positions.map((position, index) => <tr key={position.ts_code + '-' + index}><td>{position.ts_code}</td><td>{position.name || '—'}</td><td>{fmt(position.shares, 0)}</td><td>{fmt(position.avg_cost, 3)}</td><td>{fmt(position.last_price, 3)}</td><td>{fmt(position.available_shares, 0)}</td></tr>)}</Table></div> : <div className="account-empty">当前空仓 · 等待真实信号</div>}
      <div className="account-meta-row"><span>Data through {account.last_trade_date || account.updated_at || '—'}</span><span>Reconciliation {account.reconciliation_status || '—'}</span></div>
      {(account.block_reasons || []).length > 0 && <div className="risk-box"><div className="risk-box-title">执行阻断</div><div className="risk-box-detail">{account.block_reasons?.join(' · ')}</div></div>}
      <div className="account-actions"><button type="button" className="button-link secondary" onClick={onDetails}><Icon name="layers" size={14} /> 查看 Signals / Orders / Fills / Ledger</button></div>
      {details && <TechnicalDetails title="Account details" open><pre className="json-block">{JSON.stringify(details, null, 2)}</pre></TechnicalDetails>}
    </Panel>
  )
}

export default function PaperPortfolio() {
  const [data, setData] = useState<PaperData | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState('')
  const [tradeDates, setTradeDates] = useState<Record<string, string>>({})
  const [details, setDetails] = useState<Record<string, any>>({})
  const [view, setView] = useState<'current' | 'historical'>('current')

  const load = () => fetch('/api/v2/paper/portfolio').then(response => response.json()).then(setData).catch((reason: Error) => setError(reason.message))
  useEffect(() => { load() }, [])

  const runAction = async (strategyId: string, action: string) => {
    const plan = data?.current_paper_plans?.find(item => item.strategy_id === strategyId)
    const readiness = plan?.readiness || {}
    const defaultStart = readiness.requested_start_session || data?.current_data_scope?.next_forward_session || data?.latest_supported_date || ''
    const tradeDate = tradeDates[strategyId] || defaultStart
    setBusy(strategyId + ':' + action)
    setMessage('正在执行 ' + strategyId + ' · ' + action + ' …')
    try {
      const result = await postJson('/api/v2/actions/paper', {
        strategy_id: strategyId,
        action,
        trade_date: action === 'step' ? tradeDate.replace(/-/g, '') : '',
        start_date: action === 'create_generation' ? tradeDate.replace(/-/g, '') : '',
        idempotency_key: newId('ui-paper-' + action),
      })
      setMessage(strategyId + ': ' + (result.status || result.state || '已完成') + ' · ' + (result.reason || result.message || ''))
      await load()
    } catch (reason: any) {
      setMessage(strategyId + ': ' + reason.message)
    } finally {
      setBusy('')
    }
  }

  const loadDetails = async (strategyId: string) => {
    try {
      const types = ['signals', 'orders', 'fills', 'ledger']
      const values = await Promise.all(types.map(type => fetch('/api/v2/paper/' + encodeURIComponent(strategyId) + '/' + type).then(response => response.json())))
      setDetails(previous => ({ ...previous, [strategyId]: Object.fromEntries(types.map((type, index) => [type, values[index].values || []])) }))
    } catch (reason: any) {
      setMessage(strategyId + ': 明细加载失败 · ' + reason.message)
    }
  }

  if (error) return <Notice tone="danger" title="PAPER 状态加载失败">{error}<button type="button" className="text-button" onClick={load}>重新加载</button></Notice>
  if (!data) return <div className="page-loading">正在读取 PAPER 账户和资格…</div>

  const accounts = data.accounts || []
  const currentAccounts = accounts.filter(account => account.data_profile === 'ETF3_FORWARD_CURRENT')
  const historicalAccounts = accounts.filter(account => account.data_profile !== 'ETF3_FORWARD_CURRENT')
  const eligible = data.eligible_strategies || []
  const visibleAccounts = view === 'current' ? currentAccounts : historicalAccounts
  const currentAccount = currentAccounts[0]

  return (
    <div>
      <PageHeader eyebrow="PAPER / SIMULATED PORTFOLIO" title="PAPER 模拟组合" description="研究用途模拟盘；不连接券商、不访问账户。当前前向账户与历史代际分开显示。" meta={<div>Latest execution {data.latest_execution_date || '—'}</div>} actions={<button type="button" style={secondaryButton} onClick={load}><Icon name="refresh" size={14} /> 刷新</button>} />
      <Notice tone="ok" title="执行边界已锁定">LIVE_TRADING=FALSE · BROKER_ACCESS=FALSE · ACCOUNT_ACCESS=FALSE · 信号仅研究参考 · T+1 纪律</Notice>
      <div className="paper-status-bar"><div><span className="status-label">PAPER STATUS</span><StatusChip value={data.current_paper_status || data.state || 'WAITING_DATA'} raw /></div><div><span className="status-label">Data Through</span><strong>{data.latest_supported_date || data.current_data_scope?.latest_complete_eod || '—'}</strong></div><div><span className="status-label">Latest execution</span><strong>{data.latest_execution_date || '—'}</strong></div><div><span className="status-label">Reconciliation</span><StatusChip value={currentAccount?.reconciliation_status || '—'} raw /></div></div>
      <MetricStrip items={[{ label: 'NAV', value: currentAccount?.nav == null ? '—' : fmt(currentAccount.nav, 2), hint: currentAccount ? currentAccount.strategy_id : '当前账户尚未创建', mono: true }, { label: 'PnL / Return', value: currentAccount ? pct(currentAccount.total_return) : '—', tone: currentAccount?.total_return == null ? 'neutral' : currentAccount.total_return >= 0 ? 'up' : 'down' }, { label: 'Cash', value: currentAccount?.cash == null ? '—' : fmt(currentAccount.cash, 2), mono: true }, { label: 'Position %', value: currentAccount ? fmt(currentAccount.positions?.length, 0) : '—', hint: '持仓标的数' }, { label: 'Drawdown', value: currentAccount ? pct((currentAccount as any).drawdown ?? (currentAccount as any).max_drawdown) : '—' }, { label: 'Running', value: <StatusChip value={currentAccount?.status || data.current_paper_status || 'WAITING_DATA'} /> }]} />

      {data.current_paper_status && data.current_paper_status !== 'READY' && <Notice tone="warn" title="当前 PAPER 不是可执行状态">{data.eligibility_message || data.execution_data_status || '请先满足真实资格、回看窗口和数据条件。'}<Link className="inline-link" to="/research">查看资格链 →</Link></Notice>}
      {message && <div className="action-message">{message}</div>}

      {eligible.length > 0 && <Panel eyebrow="PAPER CONTROL" title="当前有效策略" className="paper-control-panel">
        <div className="panel-lede">只允许当前 V2 资格链通过的策略。新账户采用当前有效时点，不把历史缺口填成今天。</div>
        <div className="paper-plan-list">{eligible.map((item: any) => {
          const account = item.account_id ? accounts.find(value => value.account_id === item.account_id) : currentAccounts.find(value => value.strategy_id === item.strategy_id)
          const plan = data.current_paper_plans?.find(value => value.strategy_id === item.strategy_id)
          const readiness = plan?.readiness || item.current_paper || {}
          const dateValue = tradeDates[item.strategy_id] ?? readiness.requested_start_session ?? data.current_data_scope?.next_forward_session ?? data.latest_supported_date ?? ''
          return <div className="paper-plan" key={item.strategy_id}><div className="paper-plan-main"><div className="list-item-title">{item.strategy_id}</div><div className="list-item-meta">资格 {item.qualification?.qualification_id?.slice(0, 14) || '—'} · {readiness.state || '—'}</div></div><StatusChip value={account?.status || readiness.status || 'WAITING_DATA'} raw /><input className="inline-input paper-date" value={dateValue} onChange={event => setTradeDates(previous => ({ ...previous, [item.strategy_id]: event.target.value }))} placeholder="YYYYMMDD" aria-label={item.strategy_id + ' trade date'} />{!account && <button type="button" style={buttonStyle} disabled={busy !== ''} onClick={() => runAction(item.strategy_id, 'create_generation')}>建立当前 PAPER</button>}{account?.status === 'ACTIVE' && account.data_profile === 'ETF3_FORWARD_CURRENT' && <button type="button" style={secondaryButton} disabled={busy !== ''} onClick={() => runAction(item.strategy_id, 'pause')}>暂停新风险</button>}{account?.status === 'PAUSED' && account.data_profile === 'ETF3_FORWARD_CURRENT' && <button type="button" style={buttonStyle} disabled={busy !== ''} onClick={() => runAction(item.strategy_id, 'resume')}>恢复</button>}{account?.data_profile === 'ETF3_FORWARD_CURRENT' && <><button type="button" style={buttonStyle} disabled={busy !== '' || !dateValue} onClick={() => runAction(item.strategy_id, 'step')}>{busy === item.strategy_id + ':step' ? '执行中…' : '运行日步'}</button><button type="button" style={secondaryButton} disabled={busy !== ''} onClick={() => runAction(item.strategy_id, 'reconcile')}>对账</button><button type="button" style={secondaryButton} disabled={busy !== ''} onClick={() => loadDetails(item.strategy_id)}>明细</button></>}</div>
        })}</div>
      </Panel>}

      <div className="scope-tabs"><button type="button" className={view === 'current' ? 'scope-tab active' : 'scope-tab'} onClick={() => setView('current')}>Forward PAPER <span>{currentAccounts.length}</span></button><button type="button" className={view === 'historical' ? 'scope-tab active' : 'scope-tab'} onClick={() => setView('historical')}>Historical / read-only <span>{historicalAccounts.length + (data.legacy_accounts?.length || 0)}</span></button></div>
      {view === 'current' && !currentAccounts.length && <EmptyState icon="paper" title="当前前向 PAPER 尚未开始" detail={data.eligibility_message || data.state || '没有真实合格策略或当前执行数据仍在等待。'} action={<Link className="button-link secondary" to="/research">查看策略资格</Link>} />}
      {visibleAccounts.map(account => <AccountCard key={account.account_id || account.strategy_id} account={account} details={details[account.strategy_id]} onDetails={() => loadDetails(account.strategy_id)} />)}
      {view === 'historical' && data.legacy_accounts?.length ? <Panel eyebrow="LEGACY PAPER" title="Legacy 账户只读"><div className="panel-lede">Legacy lab.db 账户可用于复核信号、净值、订单和成交，但不能被 V2 admit、step、pause 或 resume，也不能转化为当前资格。</div>{data.legacy_accounts.map(account => <div className="data-row" key={account.account_id || account.strategy_id}><div><div className="list-item-title">{account.strategy_id}</div><div className="list-item-meta">{account.account_id || '—'} · {account.last_trade_date || account.updated_at || '—'}</div></div><StatusChip value="LEGACY_READONLY_PAPER" raw /></div>)}</Panel> : null}
      {data.operation_policy && <TechnicalDetails title="PAPER operation policy"><pre className="json-block">{JSON.stringify(data.operation_policy, null, 2)}</pre></TechnicalDetails>}
    </div>
  )
}

const buttonStyle = { background: 'var(--q-gold)', border: '1px solid var(--q-gold)', color: '#14110a', borderRadius: 6, padding: '8px 12px', cursor: 'pointer', fontSize: 12, fontWeight: 700 }
