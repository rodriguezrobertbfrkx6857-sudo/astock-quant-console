import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, EmptyState, MetricStrip, Notice, Panel, PageHeader, StatusChip, TechnicalDetails, button, fmt, newId, pct, postJson, secondaryButton } from '../ui'

function Sparkline({ values }: { values: number[] }) {
  if (values.length < 2) return <div className="sparkline-empty">暂无真实 NAV 序列</div>
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  const chartWidth = 100
  const points = values.map((value, index) => String((index / (values.length - 1)) * chartWidth) + ',' + String(46 - ((value - min) / range) * 38)).join(' ')
  return <svg className="sparkline" viewBox="0 0 100 52" preserveAspectRatio="none" role="img" aria-label="策略净值 sparkline"><polyline points={points} fill="none" stroke="currentColor" strokeWidth="1.8" vectorEffect="non-scaling-stroke" /></svg>
}

export default function Strategies({ embedded = false }: { embedded?: boolean }) {
  const [data, setData] = useState<any>(null)
  const [selected, setSelected] = useState<any>(null)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [watching, setWatching] = useState<Record<string, boolean>>({})
  const [viewMode, setViewMode] = useState<'eligible' | 'v2' | 'legacy'>('eligible')

  const load = () => {
    setError('')
    fetch('/api/v2/research/strategies').then(response => response.json()).then(setData).catch((reason: Error) => setError(reason.message))
  }
  useEffect(() => { load() }, [])

  const rows = data?.strategies || []
  const legacy = rows.filter((item: any) => item.provenance === 'LEGACY_EVIDENCE')
  const v2Rows = rows.filter((item: any) => item.provenance !== 'LEGACY_EVIDENCE')
  const eligible = v2Rows.filter((item: any) => item.paper_eligible)
  const qualified = eligible.length
  const shownRows = viewMode === 'eligible' ? eligible : viewMode === 'legacy' ? legacy : v2Rows
  const selectedMetrics = useMemo(() => selected?.evaluation?.metrics || selected?.metrics || {}, [selected])

  const open = (item: any) => {
    setSelected(item)
    fetch('/api/v2/research/strategies/' + encodeURIComponent(item.strategy_id))
      .then(response => response.json()).then(result => setSelected(result.strategy || result)).catch(() => {})
  }

  const evaluate = async (item: any) => {
    setMessage('正在提交 ' + item.strategy_id + ' 的 V2 评估请求…')
    try {
      const result = await postJson('/api/v2/actions/evaluate-strategy', { strategy_id: item.strategy_id, idempotency_key: newId('ui-evaluate-strategy') })
      setMessage(item.strategy_id + ': ' + (result.state || result.status || '已记录') + ' · ' + (result.reason || result.message || ''))
    } catch (reason: any) {
      setMessage(item.strategy_id + ': ' + reason.message)
    }
  }

  if (error) return <Notice tone="danger" title="策略目录加载失败">{error}<button type="button" className="text-button" onClick={load}>重新加载</button></Notice>
  if (!data) return <div className="page-loading">正在读取策略目录…</div>

  return (
    <div>
      {!embedded && <PageHeader eyebrow="RESEARCH / STRATEGIES" title="策略目录" description="V2 canonical 策略、评估结果与 PAPER 资格。Legacy evidence 仅供历史复核。" actions={<button type="button" style={secondaryButton} onClick={load}>刷新目录</button>} />}
      <MetricStrip items={[
        { label: '策略总数', value: fmt(rows.length, 0), hint: '真实返回' },
        { label: 'V2 canonical', value: fmt(v2Rows.length, 0), hint: '当前目录' },
        { label: 'Legacy evidence', value: fmt(legacy.length, 0), hint: '历史只读' },
        { label: 'PAPER eligible', value: fmt(qualified, 0), hint: '资格链通过' },
      ]} />
      {message && <div className="action-message">{message}</div>}
      <div className="section-header"><div><div className="eyebrow">STRATEGY MARKETPLACE</div><h2>{viewMode === 'eligible' ? '当前可用策略' : viewMode === 'legacy' ? 'Legacy 历史策略' : 'V2 策略目录'}</h2><p>当前可用仅显示真实 PAPER qualification 通过项；其他目录仍保留在筛选中。</p></div><div className="action-row strategy-filters"><button type="button" style={viewMode === 'eligible' ? button : secondaryButton} aria-pressed={viewMode === 'eligible'} onClick={() => setViewMode('eligible')}>当前可用 {eligible.length}</button><button type="button" style={viewMode === 'v2' ? button : secondaryButton} aria-pressed={viewMode === 'v2'} onClick={() => setViewMode('v2')}>V2 全部 {v2Rows.length}</button>{legacy.length > 0 && <button type="button" style={viewMode === 'legacy' ? button : secondaryButton} aria-pressed={viewMode === 'legacy'} onClick={() => setViewMode('legacy')}>Legacy 只读 {legacy.length}</button>}</div></div>
      {shownRows.length ? <div className="card-grid strategy-grid">{shownRows.map((item: any) => {
        const metrics = item.evaluation?.metrics || item.metrics || {}
        const curve = item.nav_series || item.evaluation?.nav_series || item.equity_curve || []
        const strategyId = item.strategy_id
        const isLegacy = item.provenance === 'LEGACY_EVIDENCE'
        return <article className={'strategy-card' + (isLegacy ? ' legacy-card' : '')} key={strategyId}>
          <div className="strategy-card-head"><div><div className="eyebrow">{item.category || item.family_id || item.type || 'STRATEGY'}</div><div className="strategy-name" title={item.name || item.strategy_name || strategyId}>{item.name || item.strategy_name || strategyId}</div><div className="strategy-id">{strategyId}</div></div><StatusChip value={isLegacy ? 'LEGACY_READONLY' : item.rating || item.registry_status || item.research_status || 'RESEARCH_ONLY'} /></div>
          <div className="strategy-description">{item.description || item.summary || (isLegacy ? '历史证据，只读浏览，不进入当前执行。' : '当前策略目录中的真实定义，详情展示 qualification 与依赖。')}</div>
          <div className="mini-metrics">
            <div className="mini-metric"><div className="mini-metric-label">RETURN</div><div className="mini-metric-value market-up">{pct(metrics.total_return ?? item.total_return)}</div></div>
            <div className="mini-metric"><div className="mini-metric-label">CAGR</div><div className="mini-metric-value">{pct(metrics.cagr ?? item.cagr)}</div></div>
            <div className="mini-metric"><div className="mini-metric-label">SHARPE</div><div className="mini-metric-value">{fmt(metrics.sharpe ?? item.sharpe, 2)}</div></div>
            <div className="mini-metric"><div className="mini-metric-label">MAX DD</div><div className="mini-metric-value market-down">{pct(metrics.max_drawdown ?? metrics.max_dd ?? item.max_drawdown)}</div></div>
            <div className="mini-metric"><div className="mini-metric-label">TRADES</div><div className="mini-metric-value">{fmt(metrics.n_trades ?? item.n_trades, 0)}</div></div>
            <div className="mini-metric"><div className="mini-metric-label">PAPER</div><div className="mini-metric-value">{item.paper_eligible == null ? '—' : item.paper_eligible ? 'YES' : 'NO'}</div></div>
          </div>
          <Sparkline values={Array.isArray(curve) ? curve.map((point: any) => Number(point.nav ?? point.value ?? point)).filter(Number.isFinite) : []} />
          <div className="card-footer-meta"><span>Updated {item.updated_at || item.evaluated_at || '—'}</span><span>{item.block_reasons?.[0] || item.reasons?.[0] || '—'}</span></div>
          <div className="strategy-card-actions"><button type="button" className="button-link" onClick={() => open(item)}>详情</button><Link className="button-link secondary" to={'/backtests?strategy=' + encodeURIComponent(strategyId)}>Backtest</Link><button type="button" className="button-link secondary" onClick={() => setWatching(previous => ({ ...previous, [strategyId]: !previous[strategyId] }))}>{watching[strategyId] ? '已关注' : '加入观察'}</button>{item.executable && !isLegacy && <button type="button" className="button-link secondary" onClick={() => evaluate(item)}>评估</button>}</div>
        </article>
      })}</div> : <EmptyState icon="layers" title={viewMode === 'eligible' ? '当前没有资格通过的策略' : '暂无策略定义'} detail={viewMode === 'eligible' ? '未通过或数据阻断的策略仍保留在 V2 全部视图，不会被当作当前可用。' : 'API 没有返回策略目录，不用占位卡片填充页面。'} />}

      {selected && <Panel eyebrow="STRATEGY PROFILE" title={'策略详情 · ' + (selected.name || selected.strategy_id)} className="detail-panel">
        <div className="detail-grid"><div>Strategy ID <strong>{selected.strategy_id || '—'}</strong></div><div>来源 <StatusChip value={selected.provenance || selected.source_kind || '—'} raw /></div><div>评级 <StatusChip value={selected.evaluation?.verdict || selected.rating || '—'} raw /></div><div>PAPER <StatusChip value={selected.paper_eligible ? 'YES' : 'NO'} raw /></div><div>Return <strong>{pct(selectedMetrics.total_return ?? selected.total_return)}</strong></div><div>Trades <strong>{fmt(selectedMetrics.n_trades ?? selected.n_trades, 0)}</strong></div></div>
        <TechnicalDetails title="Qualification / dependency evidence"><div className="detail-grid"><div>publication <strong>{selected.publication_id || '—'}</strong></div><div>updated <strong>{selected.updated_at || '—'}</strong></div></div><pre className="json-block">{JSON.stringify({ dependency_set: selected.dependency_set, block_reasons: selected.block_reasons, source: selected.source, evaluation: selected.evaluation }, null, 2)}</pre></TechnicalDetails>
      </Panel>}
    </div>
  )
}
