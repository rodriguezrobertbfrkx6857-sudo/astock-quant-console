import { useEffect, useMemo, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import { useSearchParams } from 'react-router-dom'
import { Badge, ChartFrame, EmptyState, Icon, MetricStrip, Notice, PageHeader, Panel, StatusChip, Table, Tabs, TechnicalDetails, fmt, metricScalar, newId, pct, postJson, secondaryButton } from '../ui'

const PROFILES = ['ETF3_BOUNDED_V1']
const STRATEGIES = ['etf_rotation.v1', 'etf_lowvol.v1', 'etf_trend_defensive.v1', 'etf_momentum_20.v1', 'etf_risk_adjusted_60.v1']

function Chart({ rows }: { rows: any[] }) {
  if (!rows.length) return <EmptyState icon="chart" title="该结果没有可用曲线" detail="保持真实结果空态，不从其他回测或 PAPER 账户复制数据。" />
  const dates = rows.map(row => row.trade_date || row.on || row.date || '—')
  const values = rows.map(row => row.nav ?? row.value ?? row.close)
  return <ReactECharts notMerge style={{ height: 360, width: '100%' }} option={{
    animation: false,
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', backgroundColor: '#171e27', borderColor: '#3a4654', textStyle: { color: '#f2f4f7' } },
    grid: { left: 55, right: 20, top: 18, bottom: 32 },
    xAxis: { type: 'category', data: dates, axisLabel: { color: '#8c97a6', fontSize: 10 }, axisLine: { lineStyle: { color: '#27313d' } }, axisTick: { show: false } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: '#8c97a6', fontSize: 10 }, splitLine: { lineStyle: { color: '#27313d' } } },
    series: [{ type: 'line', data: values, showSymbol: rows.length < 30, symbolSize: 5, smooth: 0.1, lineStyle: { color: '#f2c94c', width: 2 }, itemStyle: { color: '#f2c94c' }, areaStyle: { color: 'rgba(242, 201, 76, 0.08)' } }],
  }} />
}

function renderOrders(orders: any[]) {
  if (!orders.length) return <EmptyState icon="paper" title="无订单记录" />
  return (
    <Table headers={['Date', 'Asset', 'Side', 'Quantity', 'Order type', 'Status']} minWidth={760}>
      {orders.slice(0, 100).map((order, index) => (
        <tr key={order.order_id || index}><td>{order.trade_date || order.on || '—'}</td><td>{order.asset_id || order.ts_code || '—'}</td><td><StatusChip value={order.side || '—'} raw /></td><td>{fmt(order.shares ?? order.quantity, 0)}</td><td>{order.order_type || order.type || '—'}</td><td><StatusChip value={order.status || order.state || '—'} raw /></td></tr>
      ))}
    </Table>
  )
}

function renderFills(fills: any[]) {
  if (!fills.length) return <EmptyState icon="paper" title="无成交记录" />
  return (
    <Table headers={['Date', 'Asset', 'Side', 'Quantity', 'Price', 'Fee', 'Rule']} minWidth={820}>
      {fills.slice(0, 100).map((fill, index) => (
        <tr key={fill.fill_id || index}><td>{fill.trade_date || fill.on || '—'}</td><td>{fill.asset_id || fill.ts_code || '—'}</td><td><StatusChip value={fill.side || '—'} raw /></td><td>{fmt(fill.shares ?? fill.quantity, 0)}</td><td>{fmt(fill.price, 4)}</td><td>{fmt(fill.fee, 2)}</td><td>{fill.rule_id || fill.engine || '—'}</td></tr>
      ))}
    </Table>
  )
}

export default function BacktestCenter() {
  const [params] = useSearchParams()
  const [data, setData] = useState<any>(null)
  const [selectedId, setSelectedId] = useState('')
  const [detail, setDetail] = useState<any>(null)
  const [nav, setNav] = useState<any>(null)
  const [orders, setOrders] = useState<any[]>([])
  const [fills, setFills] = useState<any[]>([])
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [strategyId, setStrategyId] = useState(params.get('strategy') || 'etf_rotation.v1')
  const [profileId, setProfileId] = useState('ETF3_BOUNDED_V1')
  const [startDate, setStartDate] = useState('20190626')
  const [endDate, setEndDate] = useState('20241231')
  const [initialCash, setInitialCash] = useState('1000000')
  const [activeTab, setActiveTab] = useState('summary')

  const load = () => {
    setError('')
    fetch('/api/v2/backtests?limit=100')
      .then(response => response.json())
      .then(result => {
        setData(result)
        const first = (result.backtests || [])[0]
        if (first) setSelectedId(first.backtest_id || first.bt_id)
      })
      .catch((reason: Error) => setError(reason.message))
  }
  useEffect(() => { load() }, [])

  useEffect(() => {
    if (!selectedId) return
    Promise.all([
      fetch('/api/v2/backtests/' + encodeURIComponent(selectedId)).then(response => response.json()),
      fetch('/api/v2/backtests/' + encodeURIComponent(selectedId) + '/nav').then(response => response.json()),
      fetch('/api/v2/backtests/' + encodeURIComponent(selectedId) + '/orders').then(response => response.json()),
      fetch('/api/v2/backtests/' + encodeURIComponent(selectedId) + '/fills').then(response => response.json()),
    ]).then(([detailResult, navResult, ordersResult, fillsResult]) => {
      setDetail(detailResult.backtest || detailResult)
      setNav(navResult)
      setOrders(ordersResult.orders || [])
      setFills(fillsResult.fills || [])
    }).catch((reason: Error) => setMessage(reason.message))
  }, [selectedId])

  const runReplay = async () => {
    setMessage('正在运行 ' + strategyId + ' 的真实 ETF3 Formal/Reference 回测…')
    try {
      const cash = Number(initialCash)
      if (!Number.isFinite(cash) || cash < 10000) throw new Error('初始资金必须是不小于 10000 的有效数字。')
      const result = await postJson('/api/v2/actions/run-backtest', { strategy_id: strategyId, profile_id: profileId, start_date: startDate, end_date: endDate, initial_cash: cash, idempotency_key: newId('ui-backtest') })
      setMessage((result.state || result.status || '已完成') + ' · ' + (result.message || result.reason || ''))
      load()
    } catch (reason: any) {
      setMessage(reason.message)
    }
  }

  const rows = data?.backtests || []
  const navRows = nav?.series || nav?.rows || []
  const latest = navRows[navRows.length - 1]
  const metric = (key: string, fallback?: string) => {
    const canonicalKey = key === 'cagr' ? 'annualized_return' : key === 'max_dd' ? 'max_drawdown' : key
    return detail?.metrics?.canonical?.[canonicalKey] ?? detail?.metrics?.[canonicalKey] ?? detail?.[canonicalKey] ?? fallback
  }
  const turnover = detail?.metrics?.turnover ?? detail?.turnover
  const turnoverHint = turnover && typeof turnover === 'object'
    ? `${fmt(turnover.rebalance_count, 0)} rebalances · ${fmt(turnover.filled_orders, 0)} filled orders`
    : undefined
  const detailStatus = detail?.stage10_status || detail?.legacy_status || detail?.status || detail?.scope
  const tabs = useMemo(() => [{ value: 'summary', label: 'Summary' }, { value: 'trades', label: 'Trades', count: fills.length || undefined }, { value: 'positions', label: 'Positions' }, { value: 'orders', label: 'Orders/Fills', count: orders.length || undefined }, { value: 'evidence', label: 'Evidence' }], [fills.length, orders.length])

  if (error) return <Notice tone="danger" title="回测目录加载失败">{error}<button type="button" className="text-button" onClick={load}>重新加载</button></Notice>
  if (!data) return <div className="page-loading">正在读取回测结果…</div>

  return (
    <div>
      <PageHeader eyebrow="BACKTEST / HISTORICAL REPLAY" title="回测工作区" description="使用真实已发布数据查看或运行已冻结协议 replay；结果、订单、成交与证据按 tabs 分层。" meta={<div>{rows.length} stored results</div>} actions={<button type="button" style={secondaryButton} onClick={load}><Icon name="refresh" size={14} /> 刷新</button>} />
      <div className="command-panel">
        <div className="field"><label>Strategy</label><select value={strategyId} onChange={event => setStrategyId(event.target.value)}>{STRATEGIES.map(item => <option key={item} value={item}>{item}</option>)}</select></div>
        <div className="field"><label>Profile</label><select value={profileId} onChange={event => setProfileId(event.target.value)}>{PROFILES.map(item => <option key={item} value={item}>{item}</option>)}</select></div>
        <div className="field"><label>Start date</label><input value={startDate} onChange={event => setStartDate(event.target.value)} placeholder="YYYYMMDD" /></div>
        <div className="field"><label>End date</label><input value={endDate} onChange={event => setEndDate(event.target.value)} placeholder="YYYYMMDD" /></div>
        <div className="field"><label>Initial cash</label><input value={initialCash} onChange={event => setInitialCash(event.target.value)} inputMode="decimal" /></div>
        <div className="field action"><button type="button" style={buttonStyle} onClick={runReplay}>运行真实回测</button></div>
      </div>
      <div className="provenance-line">当前五条可执行策略的冻结研究区间为 20190626 → 20241231；超出或缩短该区间会被明确拒绝，不会静默截断或改变 Stage09 协议。</div>
      {message && <div className="action-message">{message}</div>}

      <div className="split-pane">
        <Panel eyebrow="RESULTS" title={'已保存回测 · ' + rows.length} className="split-list-panel">
          <div className="split-list">{rows.map((item: any) => {
            const id = item.backtest_id || item.bt_id
            return <button type="button" className={'list-item' + (id === selectedId ? ' active' : '')} key={id} onClick={() => { setSelectedId(id); setActiveTab('summary') }}>
              <div className="list-item-title">{item.strategy_name || item.strategy_id || 'Backtest'}</div>
              <div className="list-item-meta">{item.start || '—'} → {item.end || '—'} · {item.provenance || item.source_kind || '—'}</div>
              <div className="list-item-meta"><StatusChip value={item.stage10_status || item.legacy_status || item.status || '—'} raw /> <span className="list-item-number">{item.total_return == null ? '—' : pct(item.total_return)}</span></div>
            </button>
          })}</div>
          {!rows.length && <EmptyState icon="backtest" title="暂无已保存回测" detail="可以使用上方参数运行已有策略；不会自动扩大日期或 OOS 范围。" />}
        </Panel>

        <div className="section-stack">
          {detail ? <>
            <MetricStrip items={[
              { label: 'Total Return', value: pct(metric('total_return')), tone: Number(metricScalar(metric('total_return'))) >= 0 ? 'up' : 'down' },
              { label: 'CAGR', value: pct(metric('cagr')) },
              { label: 'Sharpe', value: fmt(metric('sharpe'), 2) },
              { label: 'Max Drawdown', value: pct(metric('max_drawdown') ?? metric('max_dd')), tone: 'down' },
              { label: 'Turnover', value: turnover && typeof turnover !== 'object' ? fmt(turnover, 2) : '—', hint: turnoverHint || 'API 未提供换手率' },
              { label: 'Trades', value: fmt(metric('n_trades') ?? detail.n_trades ?? fills.length, 0) },
            ]} />
            <ChartFrame eyebrow="NAV CURVE" title="回测净值" meta={<><StatusChip value={detailStatus || '—'} raw /> <span className="chart-meta-spacer">{latest ? latest.trade_date || latest.on : '—'}</span></>}>
              <Chart rows={navRows} />
              {latest && <div className="provenance-line">曲线点数 {navRows.length} · 最新 {latest.trade_date || latest.on || '—'} · NAV {fmt(latest.nav, 2)} · {nav?.state || ''}</div>}
            </ChartFrame>
            <Panel className="detail-tabs-panel">
              <Tabs items={tabs} value={activeTab} onChange={setActiveTab} />
              <div className="tab-panel">
                {activeTab === 'summary' && <div className="detail-grid"><div>Backtest <strong>{detail.backtest_id || detail.bt_id || selectedId}</strong></div><div>Strategy <strong>{detail.strategy_id || detail.strategy_name || '—'}</strong></div><div>Scope <strong>{detail.scope || detail.profile_id || '—'}</strong></div><div>Source <StatusChip value={detail.provenance || detail.source_kind || '—'} raw /></div><div>Stage10 <StatusChip value={detail.stage10_status || detail.legacy_status || '—'} raw /></div><div>Detail state <strong>{detail.detail_state || '—'}</strong></div></div>}
                {activeTab === 'trades' && renderFills(fills)}
                {activeTab === 'positions' && (Array.isArray(detail.positions) && detail.positions.length ? <Table headers={['Asset', 'Shares', 'Avg cost', 'Last price']} minWidth={620}>{detail.positions.map((position: any, index: number) => <tr key={position.ts_code || index}><td>{position.ts_code || position.asset_id || '—'}</td><td>{fmt(position.shares, 0)}</td><td>{fmt(position.avg_cost, 3)}</td><td>{fmt(position.last_price, 3)}</td></tr>)}</Table> : <EmptyState icon="paper" title="当前 API 未提供持仓明细" detail="不从订单或成交反推持仓。" />)}
                {activeTab === 'orders' && <div className="section-stack"><div><div className="subsection-title">Orders · {orders.length}</div>{renderOrders(orders)}</div><div><div className="subsection-title">Fills · {fills.length}</div>{renderFills(fills)}</div></div>}
                {activeTab === 'evidence' && <TechnicalDetails title="Backtest technical evidence" open><pre className="json-block">{JSON.stringify({ detail, nav_state: nav?.state, nav_metadata: nav?.metadata }, null, 2)}</pre></TechnicalDetails>}
              </div>
            </Panel>
          </> : <EmptyState icon="backtest" title="选择一个回测结果" detail="从左侧打开真实结果；如果没有结果，使用参数区运行已有策略。" />}
        </div>
      </div>
    </div>
  )
}

const buttonStyle = { background: 'var(--q-gold)', border: '1px solid var(--q-gold)', color: '#14110a', borderRadius: 6, padding: '8px 12px', cursor: 'pointer', fontSize: 12, fontWeight: 700 }
