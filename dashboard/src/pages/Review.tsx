import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { EmptyState, MetricStrip, Notice, PageHeader, Panel, StatusChip, Table, fmt, secondaryButton } from '../ui'

type Json = Record<string, any>

async function api(path: string, init?: RequestInit) {
  const response = await fetch(path, init)
  const body = await response.json()
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`)
  return body
}

export default function Review() {
  const [plans, setPlans] = useState<Json[]>([])
  const [overview, setOverview] = useState<Json | null>(null)
  const [research, setResearch] = useState<Json | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [settleDate, setSettleDate] = useState('')

  const load = async () => {
    setError('')
    try {
      const [p, o, r] = await Promise.all([
        api('/api/v2/short-term/plans?limit=100'),
        api('/api/v2/short-term/overview'),
        api('/api/v2/short-term/research'),
      ])
      setPlans(p.plans || [])
      setOverview(o)
      setResearch(r.research)
    } catch (reason) { setError((reason as Error).message) }
  }

  useEffect(() => { load() }, [])

  const settle = async () => {
    if (!settleDate) return
    setBusy('settle'); setError('')
    try { await api(`/api/v2/actions/short-term/settle`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target_session: settleDate }) }); await load() }
    catch (reason) { setError((reason as Error).message) }
    finally { setBusy('') }
  }

  const settledPlans = plans.filter(p => p.settlement)
  const pendingPlans = plans.filter(p => !p.settlement && p.target_session)

  return <div>
    <PageHeader
      eyebrow="复盘记录"
      title="计划复盘与验证"
      description="对比事前计划和实际结果。结算只在目标交易日的真实完整日线到达后进行。历史回测和真实前向分开展示。"
      meta={<div>REAL DATA ONLY · 不追填虚构前向成绩</div>}
      actions={<button type="button" style={secondaryButton} onClick={load}>刷新</button>}
    />

    {error && <Notice tone="danger" title="操作未完成">{error}</Notice>}

    <Notice tone="info" title="复盘说明">
      结算按真实已完成数据判断触发/未触发/无法判断。日线只能判断区间是否触及；若同一日同时触及入场与风险线而日内顺序未知，结果会标记为无法确定。
    </Notice>

    <MetricStrip items={[
      { label: '已结算', value: fmt(settledPlans.length, 0), hint: '有结算记录的计划' },
      { label: '待结算', value: fmt(pendingPlans.length, 0), hint: '目标日已过但未结算' },
      { label: '研究状态', value: <StatusChip value={research?.status || 'NOT_RUN'} raw />, hint: '策略验证' },
      { label: 'OOS', value: 'KNOWN ONLY', hint: '不消耗新区间' },
    ]} />

    <Panel eyebrow="结算" title="运行结算">
      <div className="panel-lede">
        输入目标交易日（YYYYMMDD），系统会按该日真实日线数据结算所有对应计划。如果数据未完成，返回 BLOCKED_INCOMPLETE_SESSION。
      </div>
      <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
        <input
          type="text"
          value={settleDate}
          onChange={e => setSettleDate(e.target.value)}
          placeholder="20260914"
          style={{ width: '160px', padding: '7px 9px', color: 'var(--q-text)', background: 'var(--q-bg-0)', border: '1px solid var(--q-border-strong)', borderRadius: '6px', fontSize: '12px' }}
        />
        <button className="button-link" type="button" onClick={settle} disabled={!!busy || !settleDate}>
          {busy === 'settle' ? '结算中…' : '运行结算'}
        </button>
        {overview?.latest_scan?.target_session && (
          <button type="button" style={secondaryButton} onClick={() => setSettleDate(overview.latest_scan.target_session)}>
            填入 {overview.latest_scan.target_session}
          </button>
        )}
      </div>
    </Panel>

    <Panel eyebrow="已结算计划" title={`${settledPlans.length} 条结算记录`}>
      {settledPlans.length ? <Table headers={['证券', '类型', '策略', '目标日', '结算结果', '数据日期']} minWidth={760}>
        {settledPlans.map(item => <tr key={item.plan_id}>
          <td><strong>{item.asset_id}</strong></td>
          <td>{item.plan_type}</td>
          <td>{item.strategy_version}</td>
          <td>{item.target_session}</td>
          <td><StatusChip value={item.settlement?.outcome || '—'} raw /></td>
          <td>{item.settlement?.data_date || '—'}</td>
        </tr>)}
      </Table> : <EmptyState icon="calendar" title="暂无结算记录" detail="结算需要目标交易日的完整日线数据。请先运行扫描生成计划，等目标日数据到达后再结算。" action={<Link className="button-link" to="/opportunities">查看候选</Link>} />}
    </Panel>

    <Panel eyebrow="待结算计划" title={`${pendingPlans.length} 条待结算`}>
      {pendingPlans.length ? <Table headers={['证券', '类型', '策略', '目标日', '观察区间', '风险线', '状态']} minWidth={760}>
        {pendingPlans.slice(0, 20).map(item => <tr key={item.plan_id}>
          <td><strong>{item.asset_id}</strong></td>
          <td>{item.plan_type}</td>
          <td>{item.strategy_version}</td>
          <td>{item.target_session}</td>
          <td>¥{item.entry_low} – ¥{item.entry_high}</td>
          <td>¥{item.stop_price}</td>
          <td><StatusChip value={item.status} raw /></td>
        </tr>)}
      </Table> : <EmptyState icon="activity" title="暂无待结算计划" detail="所有计划都已结算或尚未生成。" />}
    </Panel>

    <Panel eyebrow="研究验证" title="策略研究状态">
      <div className="detail-grid">
        <div>研究状态<strong>{research?.status || 'NOT_RUN'}</strong></div>
        <div>OOS 范围<strong>KNOWN ONLY</strong></div>
        <div>盘中触发<strong>DISABLED</strong></div>
        <div>结算方式<strong>REAL COMPLETED DATA ONLY</strong></div>
      </div>
      <div className="panel-lede" style={{ marginTop: '12px' }}>
        四个策略族（趋势回踩、突破确认、短期反转、底仓做T）注册为工程版本。研究因缺少认证历史 PIT 身份、公司行动标签和分钟顺序证据而 BLOCKED_DATA。
        这是正确的研究结果，不是软件故障。不能调参强行通过。
      </div>
      <div style={{ marginTop: '12px' }}>
        <div style={{ color: 'var(--q-text-muted)', fontSize: '11px', marginBottom: '8px' }}>验证要求</div>
        <div style={{ color: 'var(--q-text)', fontSize: '12px', lineHeight: '1.8' }}>
          · 研究必须比较简单基线、扣除费用和滑点后的结果<br />
          · 报告回撤、换手、覆盖率、不同阶段表现<br />
          · 做 T 须报告相对持有不动的增量收益、卖飞和回补失败<br />
          · 日线不能证明日内买卖先后顺序；缺分钟证据不宣称做 T 已验证<br />
          · 遵守 T+1、手数、涨跌停和停牌约束
        </div>
      </div>
    </Panel>
  </div>
}
