import { FormEvent, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { EmptyState, MetricStrip, Notice, PageHeader, Panel, StatusChip, Table, fmt, secondaryButton } from '../ui'

type Json = Record<string, any>

async function api(path: string, init?: RequestInit) {
  const response = await fetch(path, init)
  const body = await response.json()
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`)
  return body
}

export default function Holdings() {
  const [holdings, setHoldings] = useState<Json[]>([])
  const [plans, setPlans] = useState<Json[]>([])
  const [overview, setOverview] = useState<Json | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [csvText, setCsvText] = useState('asset_id,shares,sellable_shares,today_bought_shares,cost_price,risk_profile\n')
  const [preview, setPreview] = useState<Json[]>([])
  const [form, setForm] = useState({ asset_id: '', shares: '', sellable_shares: '', today_bought_shares: '0', cost_price: '', risk_profile: 'BALANCED' })
  const [expandedPlan, setExpandedPlan] = useState<string | null>(null)

  const load = async () => {
    setError('')
    try {
      const [h, p, o] = await Promise.all([
        api('/api/v2/short-term/holdings'),
        api('/api/v2/short-term/plans?limit=100'),
        api('/api/v2/short-term/overview'),
      ])
      setHoldings(h.holdings || [])
      setPlans(p.plans || [])
      setOverview(o)
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

  const holdingPlans = useMemo(() => plans.filter(item => item.plan_type === 'HOLDING'), [plans])
  const baseTPlans = useMemo(() => plans.filter(item => item.plan_type === 'BASE_T'), [plans])

  return <div>
    <PageHeader
      eyebrow="我的持仓"
      title="持仓管理与次日计划"
      description="录入或更新持仓，生成次日行动计划。持仓不连接券商账户，不写入 PAPER 账本。计划不可覆盖旧版本，修改会生成新版本。"
      meta={<div>LOCAL ONLY · 不读取券商</div>}
      actions={<>
        <button type="button" style={secondaryButton} onClick={load}>刷新</button>
        <Link className="button-link" to="/opportunities">查看明日机会</Link>
      </>}
    />

    {error && <Notice tone="danger" title="操作未完成">{error}</Notice>}

    <Notice tone="info" title="持仓录入须知">
      {'必须填写证券代码、总股数、今日可卖、今日买入和成本价。系统验证「可卖+今日买入 ≤ 总股数」。今日新买入不会被当成可自由卖出。成本只用于盈亏参考，不以回本为目标。'}
    </Notice>

    <MetricStrip items={[
      { label: '持仓数', value: fmt(holdings.length, 0), hint: '手工录入' },
      { label: '持仓计划', value: fmt(holdingPlans.length, 0), hint: '不可覆盖版本' },
      { label: '做 T 计划', value: baseTPlans.length > 0 ? fmt(baseTPlans.length, 0) : '未启用', hint: '分钟验收前禁用' },
      { label: '数据日期', value: overview?.latest_scan?.data_date || '—', hint: '扫描数据截止' },
    ]} />

    <div className="home-grid">
      <Panel eyebrow="录入持仓" title="手工录入">
        <form onSubmit={saveHolding} className="form-grid">
          <label>证券代码<input value={form.asset_id} onChange={e => setForm({ ...form, asset_id: e.target.value.toUpperCase() })} placeholder="600000.SH" required /></label>
          <label>总股数<input type="number" min="0" value={form.shares} onChange={e => setForm({ ...form, shares: e.target.value })} required /></label>
          <label>今日可卖<input type="number" min="0" value={form.sellable_shares} onChange={e => setForm({ ...form, sellable_shares: e.target.value })} required /></label>
          <label>今日买入<input type="number" min="0" value={form.today_bought_shares} onChange={e => setForm({ ...form, today_bought_shares: e.target.value })} required /></label>
          <label>成本价<input type="number" min="0.01" step="0.01" value={form.cost_price} onChange={e => setForm({ ...form, cost_price: e.target.value })} required /></label>
          <label>风险偏好<select value={form.risk_profile} onChange={e => setForm({ ...form, risk_profile: e.target.value })}><option>CONSERVATIVE</option><option>BALANCED</option><option>AGGRESSIVE</option></select></label>
          <button className="button-link" type="submit" disabled={!!busy}>{busy === 'holding' ? '保存中…' : '保存持仓修订'}</button>
        </form>
        <div className="panel-lede">保存后会立即生成一个不可覆盖的修订版本。重复提交相同输入是幂等的。</div>
      </Panel>

      <Panel eyebrow="批量导入" title="CSV 预览（不保存）">
        <textarea value={csvText} onChange={e => setCsvText(e.target.value)} rows={8} style={{ width: '100%' }} aria-label="持仓 CSV" />
        <div className="panel-actions">
          <button type="button" style={secondaryButton} onClick={previewCsv} disabled={!!busy}>{busy === 'csv' ? '校验中…' : '仅预览，不保存'}</button>
        </div>
        <div className="panel-lede">预览不会进入正式持仓或前向统计。确认格式正确后，请逐笔正式保存。</div>
        {preview.length > 0 && <pre className="json-block">{JSON.stringify(preview, null, 2)}</pre>}
      </Panel>
    </div>

    <Panel eyebrow="当前持仓" title={`持仓列表 · ${holdings.length} 条`}>
      {holdings.length ? <Table headers={['证券', '总股数', '可卖', '今日买入', '成本', '风险偏好', '录入时间', '分析']} minWidth={820}>
        {holdings.map(item => <tr key={item.revision_id}>
          <td><strong>{item.asset_id}</strong></td>
          <td>{item.shares}</td>
          <td>{item.sellable_shares}</td>
          <td>{item.today_bought_shares}</td>
          <td>¥{item.cost_price}</td>
          <td><StatusChip value={item.risk_profile} raw /></td>
          <td>{item.as_of}</td>
          <td><Link className="table-link" to={`/market?code=${item.asset_id}`}>行情分析 →</Link></td>
        </tr>)}
      </Table> : <EmptyState icon="briefcase" title="尚未录入持仓" detail="系统不会读取 PAPER 或券商账户。请手工录入或先用 CSV 预览格式。录入持仓后可生成次日行动计划。" action={<Link className="button-link" to="/opportunities">先看明日机会</Link>} />}
    </Panel>

    <Panel eyebrow="次日行动计划" title={`持仓计划 · ${holdingPlans.length} 条`}>
      <div className="panel-actions" style={{ marginTop: 0, paddingTop: 0, borderTop: 'none', marginBottom: '12px' }}>
        <button className="button-link" type="button" onClick={() => post('/api/v2/actions/short-term/generate-holding-plans', {}, 'plans')} disabled={!!busy || holdings.length === 0}>
          {busy === 'plans' ? '生成中…' : '生成持仓计划'}
        </button>
      </div>
      <div className="panel-lede">计划依据最新日线数据生成，包含持有/减仓建议、高平低开情景、观察区间和风险线。计划不会进入 PAPER 账本。</div>
      {holdingPlans.length ? holdingPlans.slice(0, 20).map(item => {
        const isExpanded = expandedPlan === item.plan_id
        return <div className="data-row" key={item.plan_id} style={{ cursor: 'pointer', flexDirection: 'column', alignItems: 'stretch' }} onClick={() => setExpandedPlan(isExpanded ? null : item.plan_id)}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', width: '100%' }}>
            <div>
              <div className="list-item-title">{item.asset_id} · {item.rules?.action || '—'}</div>
              <div className="list-item-meta">可卖上限 {item.rules?.max_sell_quantity} · 观察 {item.entry_low}–{item.entry_high} · 风险 {item.stop_price}</div>
            </div>
            <StatusChip value={item.status} raw />
          </div>
          {isExpanded && <div style={{ marginTop: '12px', padding: '12px', background: 'var(--q-bg-0)', borderRadius: '6px', border: '1px solid var(--q-border)' }}>
            <div className="detail-grid">
              <div>观察买入区间<strong>¥{item.entry_low} – ¥{item.entry_high}</strong></div>
              <div>目标/减仓<strong>¥{item.target_low}</strong></div>
              <div>风险线<strong>¥{item.stop_price}</strong></div>
              <div>验证<strong>{item.validation}</strong></div>
            </div>
            {item.trigger && <div style={{ marginTop: '8px', color: 'var(--q-text-muted)', fontSize: '11px' }}>
              触发条件：{item.trigger.enter_only_if || '—'} · 盘中触发：{item.trigger.live_trigger_enabled ? '已启用' : '未启用'}
            </div>}
            {item.invalidation && <div style={{ marginTop: '4px', color: 'var(--q-text-muted)', fontSize: '11px' }}>
              失效条件：追高 &gt; ¥{item.invalidation.chase_above} · 风险 &lt; ¥{item.invalidation.risk_below} · 过期 {item.invalidation.expires_after}
            </div>}
          </div>}
        </div>
      }) : <EmptyState icon="calendar" title="暂无持仓计划" detail={'保存真实持仓后点击「生成持仓计划」。如果数据有缺口，计划会保持观察状态，不会变绿。'} />}
    </Panel>

    <Panel eyebrow="底仓做 T" title={`做 T 提示 · 当前未启用`}>
      <Notice tone="warn" title="盘中提示未启用">
        做 T 需要完整分钟历史、连续性与恢复验收。日线不能确定同分钟触发先后，不会生成虚拟成交。待分钟数据验收通过后方可启用。
      </Notice>
      {baseTPlans.length > 0 && baseTPlans.slice(0, 5).map(item => (
        <div className="data-row" key={item.plan_id}>
          <div>
            <div className="list-item-title">{item.asset_id}</div>
            <div className="list-item-meta">可卖上限 {item.trigger?.sellable_cap} · 同分钟采用拒绝/保守路径</div>
          </div>
          <StatusChip value={item.status} raw />
        </div>
      ))}
    </Panel>
  </div>
}
