import { FormEvent, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { EmptyState, MetricStrip, Notice, PageHeader, Panel, StatusChip, Table, TechnicalDetails, fmt, secondaryButton } from '../ui'

type Json = Record<string, any>

async function api(path: string, init?: RequestInit) {
  const response = await fetch(path, init)
  const body = await response.json()
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`)
  return body
}

export default function ShortTermWorkbench() {
  const [overview, setOverview] = useState<Json | null>(null)
  const [holdings, setHoldings] = useState<Json[]>([])
  const [scan, setScan] = useState<Json | null>(null)
  const [plans, setPlans] = useState<Json[]>([])
  const [research, setResearch] = useState<Json | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [csvText, setCsvText] = useState('asset_id,shares,sellable_shares,today_bought_shares,cost_price,risk_profile\n')
  const [preview, setPreview] = useState<Json[]>([])
  const [form, setForm] = useState({ asset_id: '', shares: '', sellable_shares: '', today_bought_shares: '0', cost_price: '', risk_profile: 'BALANCED' })

  const load = async () => {
    setError('')
    try {
      const [o, h, c, p, r] = await Promise.all([
        api('/api/v2/short-term/overview'), api('/api/v2/short-term/holdings'), api('/api/v2/short-term/candidates'), api('/api/v2/short-term/plans?limit=100'), api('/api/v2/short-term/research'),
      ])
      setOverview(o); setHoldings(h.holdings || []); setScan(c.scan); setPlans(p.plans || []); setResearch(r.research)
    } catch (reason) { setError((reason as Error).message) }
  }
  useEffect(() => { load() }, [])

  const post = async (path: string, body: Json, label: string) => {
    setBusy(label); setError('')
    try { await api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); await load() }
    catch (reason) { setError((reason as Error).message) }
    finally { setBusy('') }
  }

  const saveHolding = (event: FormEvent) => {
    event.preventDefault()
    post('/api/v2/actions/short-term/holding', { ...form, shares: Number(form.shares), sellable_shares: Number(form.sellable_shares), today_bought_shares: Number(form.today_bought_shares), cost_price: Number(form.cost_price), active: true }, 'holding')
  }
  const previewCsv = async () => {
    setBusy('csv'); setError('')
    try { const result = await api('/api/v2/actions/short-term/import-holdings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ csv_text: csvText, preview_only: true }) }); setPreview(result.rows || []) }
    catch (reason) { setError((reason as Error).message) }
    finally { setBusy('') }
  }

  const candidates = scan?.candidates || []
  const holdingPlans = useMemo(() => plans.filter(item => item.plan_type === 'HOLDING'), [plans])
  const baseT = useMemo(() => plans.filter(item => item.plan_type === 'BASE_T'), [plans])
  const coverage = scan?.coverage || overview?.latest_scan?.coverage || {}
  const watch = overview?.watch || {}

  if (!overview && !error) return <div className="page-loading">正在读取短线工作台账本…</div>
  return <div>
    <PageHeader eyebrow="SHORT-TERM / DECISION SUPPORT" title="短线工作台" description="盘后候选、手工持仓次日计划与底仓做 T 研究。仅作观察辅助，不连接账户、不下单、不保证收益。此页面保留兼容访问，日常操作请使用左侧导航的新入口。" meta={<div>LOCAL ONLY · LIVE_TRADING=FALSE</div>} actions={<div style={{ display: 'flex', gap: '8px' }}><Link className="button-link" to="/holdings">我的持仓</Link><Link className="button-link secondary" to="/opportunities">明日机会</Link><button type="button" style={secondaryButton} onClick={load}>刷新</button></div>} />
    <Notice tone="info" title="新版入口已启用">
      日常操作请从左侧导航进入：「我的持仓」「明日机会」「盘中观察」「复盘记录」。本页保留完整功能，可继续直接使用。
    </Notice>
    {error && <Notice tone="danger" title="操作未完成">{error}</Notice>}
    <Notice tone={coverage.missing_open_sessions?.length ? 'warn' : 'info'} title={coverage.missing_open_sessions?.length ? '候选仅供观察：真实数据仍有缺口' : '决策辅助边界'}>
      数据 through {scan?.data_date || '—'}；计划日 {scan?.target_session || '—'}。{coverage.missing_open_sessions?.length ? `缺少交易日 ${coverage.missing_open_sessions.join('、')}；当前停牌/涨跌停状态为 ${coverage.current_suspend_state}/${coverage.current_limit_state}。` : '排名不是上涨概率，所有计划都有失效条件。'}
    </Notice>
    <MetricStrip items={[
      { label: '候选', value: fmt(candidates.length, 0), hint: scan?.status || '尚未扫描' },
      { label: '手工持仓', value: fmt(holdings.length, 0), hint: '不读取 PAPER / 券商' },
      { label: '持仓计划', value: fmt(holdingPlans.length, 0), hint: '不可覆盖版本' },
      { label: '做 T', value: <StatusChip value={baseT.length ? 'DISABLED' : 'NOT_READY'} raw />, hint: '分钟验收前禁用' },
      { label: 'Watch', value: <StatusChip value={watch.status || 'PENDING_VENDOR'} raw />, hint: `${watch.validated_sample_count || 0} samples` },
      { label: '交易', value: 'OFF', hint: '无订单路径' },
    ]} />

    <div className="home-grid">
      <Panel eyebrow="MY HOLDINGS" title="我的持仓">
        <form onSubmit={saveHolding} className="form-grid">
          <label>证券代码<input value={form.asset_id} onChange={e => setForm({ ...form, asset_id: e.target.value.toUpperCase() })} placeholder="600000.SH" required /></label>
          <label>总股数<input type="number" min="0" value={form.shares} onChange={e => setForm({ ...form, shares: e.target.value })} required /></label>
          <label>今日可卖<input type="number" min="0" value={form.sellable_shares} onChange={e => setForm({ ...form, sellable_shares: e.target.value })} required /></label>
          <label>今日买入<input type="number" min="0" value={form.today_bought_shares} onChange={e => setForm({ ...form, today_bought_shares: e.target.value })} required /></label>
          <label>成本价<input type="number" min="0.01" step="0.01" value={form.cost_price} onChange={e => setForm({ ...form, cost_price: e.target.value })} required /></label>
          <label>风险偏好<select value={form.risk_profile} onChange={e => setForm({ ...form, risk_profile: e.target.value })}><option>CONSERVATIVE</option><option>BALANCED</option><option>AGGRESSIVE</option></select></label>
          <button className="button-link" type="submit" disabled={!!busy}>{busy === 'holding' ? '保存中…' : '保存持仓修订'}</button>
        </form>
        <div className="panel-lede">必须明确今日可卖数量；今日新买入部分不会被当成可自由卖出。</div>
        {holdings.length ? <Table headers={['证券', '总股数', '可卖', '今日买入', '成本', '风险', '时间']} minWidth={760}>{holdings.map(item => <tr key={item.revision_id}><td>{item.asset_id}</td><td>{item.shares}</td><td>{item.sellable_shares}</td><td>{item.today_bought_shares}</td><td>{item.cost_price}</td><td>{item.risk_profile}</td><td>{item.as_of}</td></tr>)}</Table> : <EmptyState icon="layers" title="尚未录入持仓" detail="系统不会读取 PAPER 或券商账户。可手工录入或先预览 CSV。" />}
      </Panel>
      <Panel eyebrow="CSV / ISOLATED PREVIEW" title="批量预览">
        <textarea value={csvText} onChange={e => setCsvText(e.target.value)} rows={8} style={{ width: '100%' }} aria-label="持仓 CSV" />
        <div className="panel-actions"><button type="button" style={secondaryButton} onClick={previewCsv} disabled={!!busy}>{busy === 'csv' ? '校验中…' : '仅预览，不保存'}</button></div>
        {preview.length > 0 && <pre className="json-block">{JSON.stringify(preview, null, 2)}</pre>}
      </Panel>
    </div>

    <Panel eyebrow="TOMORROW OPPORTUNITIES" title={`明日机会 · ${scan?.target_session || '未生成'}`}>
      <div className="panel-actions"><button className="button-link" type="button" onClick={() => post('/api/v2/actions/short-term/scan', {}, 'scan')} disabled={!!busy}>{busy === 'scan' ? '扫描中…' : '运行真实数据扫描'}</button><button type="button" style={secondaryButton} onClick={() => post('/api/v2/actions/short-term/generate-holding-plans', {}, 'plans')} disabled={!!busy}>{busy === 'plans' ? '生成中…' : '生成持仓计划'}</button></div>
      {candidates.length ? <Table headers={['#', '证券', '策略', '观察买入区间', '目标/减仓', '风险线', '状态']} minWidth={940}>{candidates.map((item: Json) => <tr key={item.plan_id}><td>{item.rank}</td><td>{item.asset_id}</td><td>{item.strategy_version}</td><td>{item.entry_low} – {item.entry_high}</td><td>{item.target_low}</td><td>{item.stop_price}</td><td><StatusChip value={item.status} raw /></td></tr>)}</Table> : <EmptyState icon="activity" title="没有候选" detail="零候选是合法结果。运行扫描不会修改策略参数或消耗新 untouched OOS。" />}
    </Panel>

    <div className="home-grid">
      <Panel eyebrow="HOLDING PLAN" title="次日行动计划">
        {holdingPlans.length ? holdingPlans.slice(0, 20).map(item => <div className="data-row" key={item.plan_id}><div><div className="list-item-title">{item.asset_id} · {item.rules?.action}</div><div className="list-item-meta">可卖上限 {item.rules?.max_sell_quantity} · 观察 {item.entry_low}–{item.entry_high} · 风险 {item.stop_price}</div></div><StatusChip value={item.status} raw /></div>) : <EmptyState icon="watch" title="暂无持仓计划" detail="保存真实手工持仓后再生成；计划不会进入 PAPER 账本。" />}
      </Panel>
      <Panel eyebrow="BASE T" title="底仓做 T">
        <Notice tone="warn" title="盘中提示未启用">完整分钟历史、连续性与恢复验收尚未通过。日线不能决定同分钟触发先后，也不会生成虚拟成交。</Notice>
        {baseT.slice(0, 10).map(item => <div className="data-row" key={item.plan_id}><div><div className="list-item-title">{item.asset_id}</div><div className="list-item-meta">可卖上限 {item.trigger?.sellable_cap} · 同分钟采用拒绝/保守路径</div></div><StatusChip value={item.status} raw /></div>)}
      </Panel>
    </div>

    <Panel eyebrow="RESEARCH / FORWARD" title="研究验证与计划复盘">
      <div className="detail-grid"><div>research <strong>{research?.status || 'NOT_RUN'}</strong></div><div>OOS <strong>KNOWN ONLY</strong></div><div>live trigger <strong>DISABLED</strong></div><div>settlement <strong>REAL COMPLETED DATA ONLY</strong></div></div>
      <div className="panel-lede">新股票策略不继承 ETF PAPER 资格。研究被拒绝或数据不足都是合法结果，不能调参强行通过。</div>
    </Panel>
    <TechnicalDetails title="Snapshot / provenance"><pre className="json-block">{JSON.stringify({ scan_id: scan?.scan_id, snapshot_hash: scan?.snapshot_hash, coverage, boundaries: overview?.boundaries, watch }, null, 2)}</pre></TechnicalDetails>
  </div>
}
