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

function strategyLabel(version: string): string {
  const map: Record<string, string> = {
    'SHORT_TREND_PULLBACK_V1': '趋势回踩',
    'SHORT_BREAKOUT_CONFIRM_V1': '突破确认',
    'SHORT_REVERSAL_V1': '短期反转',
    'SHORT_TREND_PULLBACK_V2': '趋势回踩 V2',
    'SHORT_BREAKOUT_CONFIRM_V2': '突破确认 V2',
    'SHORT_REVERSAL_V2': '短期反转 V2',
    'SHORT_GAP_MOMENTUM_V1': '跳空动量',
    'SHORT_RELATIVE_STRENGTH_V1': '相对强度',
  }
  return map[version] || version
}

function strategyDescription(version: string): string {
  const map: Record<string, string> = {
    'SHORT_TREND_PULLBACK_V1': '均线多头排列，短期回调至支撑区间',
    'SHORT_BREAKOUT_CONFIRM_V1': '放量突破前高，量价配合确认',
    'SHORT_REVERSAL_V1': '连续下跌后企稳，量能未萎缩',
    'SHORT_TREND_PULLBACK_V2': 'V2：回调缩量确认',
    'SHORT_BREAKOUT_CONFIRM_V2': 'V2：从整理区间突破',
    'SHORT_REVERSAL_V2': 'V2：量能高潮确认',
    'SHORT_GAP_MOMENTUM_V1': '放量跳空延续，机构资金流入',
    'SHORT_RELATIVE_STRENGTH_V1': '弱势市场中的强势股',
  }
  return map[version] || '—'
}

function validationChip(level: string): string {
  const map: Record<string, string> = {
    'ENGINEERING_ONLY': '工程版本',
    'ENGINEERING_ONLY_V2': '改进版 V2',
    'ENGINEERING_ONLY_NEW': '新策略',
    'CANDIDATE': '已验证',
  }
  return map[level] || level || '工程版本'
}

export default function Opportunities() {
  const [scan, setScan] = useState<Json | null>(null)
  const [overview, setOverview] = useState<Json | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [expandedCandidate, setExpandedCandidate] = useState<string | null>(null)

  const load = async () => {
    setError('')
    try {
      const [c, o] = await Promise.all([
        api('/api/v2/short-term/candidates'),
        api('/api/v2/short-term/overview'),
      ])
      setScan(c.scan)
      setOverview(o)
    } catch (reason) { setError((reason as Error).message) }
  }

  useEffect(() => { load() }, [])

  const runScan = async () => {
    setBusy('scan'); setError('')
    try { await api('/api/v2/actions/short-term/scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); await load() }
    catch (reason) { setError((reason as Error).message) }
    finally { setBusy('') }
  }

  const candidates = scan?.candidates || []
  const coverage = scan?.coverage || overview?.latest_scan?.coverage || {}
  const hasGap = coverage.missing_open_sessions?.length > 0

  return <div>
    <PageHeader
      eyebrow="明日机会"
      title={`候选扫描 · ${scan?.target_session || '未生成'}`}
      description="从较大股票池筛选短线观察候选。排名不是上涨概率，候选只是工程观察名单，不是已验证买入建议。允许零候选。"
      meta={<div>数据 through {scan?.data_date || '—'} · 目标 {scan?.target_session || '—'}</div>}
      actions={<>
        <button type="button" style={secondaryButton} onClick={load}>刷新</button>
        <button className="button-link" type="button" onClick={runScan} disabled={!!busy}>
          {busy === 'scan' ? '扫描中…' : '运行真实数据扫描'}
        </button>
      </>}
    />

    {error && <Notice tone="danger" title="操作未完成">{error}</Notice>}

    {hasGap ? (
      <Notice tone="warn" title="候选仅供观察：真实数据仍有缺口">
        缺少交易日 {coverage.missing_open_sessions?.join('、')}；当前停牌/涨跌停状态为 {coverage.current_suspend_state}/{coverage.current_limit_state}。候选未通过完整数据验证，只能用于观察和核对。
      </Notice>
    ) : (
      <Notice tone="info" title="决策辅助边界">
        候选排名不是上涨概率。区间是观察计划，不是保证成交或预知高低点。所有计划都有失效条件。
      </Notice>
    )}

    <MetricStrip items={[
      { label: '候选数', value: fmt(candidates.length, 0), hint: scan?.status || '尚未扫描' },
      { label: '数据截止', value: scan?.data_date || '—', hint: '日线 through' },
      { label: '计划目标', value: scan?.target_session || '—', hint: '下一个未观察交易日' },
      { label: 'V1/V2/新增', value: (() => {
        const v1 = candidates.filter((c: Json) => c.strategy_version?.endsWith('_V1') && !c.strategy_version?.includes('GAP') && !c.strategy_version?.includes('RELATIVE')).length
        const v2 = candidates.filter((c: Json) => c.strategy_version?.endsWith('_V2')).length
        const nw = candidates.filter((c: Json) => c.strategy_version?.includes('GAP') || c.strategy_version?.includes('RELATIVE')).length
        return `${v1}/${v2}/${nw}`
      })(), hint: '策略版本分布' },
    ]} />

    <Panel eyebrow="候选列表" title={`${candidates.length} 个观察候选`}>
      <div className="panel-lede">
        点击候选行展开查看策略细节、入选理由和失效条件。候选来自 {scan?.snapshot_hash ? '真实不可变日线数据' : '待扫描'}。
        最多 12 个候选，每策略族最多 4 个。扫描不修改策略参数。
      </div>

      {candidates.length ? <Table headers={['#', '证券', '策略', '验证级别', '入选理由', '状态', '分析']} minWidth={900}>
        {candidates.map((item: Json) => {
          const isExpanded = expandedCandidate === item.plan_id
          return <tr key={item.plan_id} style={{ cursor: 'pointer' }} onClick={() => setExpandedCandidate(isExpanded ? null : item.plan_id)} className={isExpanded ? 'selected-row' : ''}>
            <td>{item.rank}</td>
            <td><strong>{item.asset_id}</strong></td>
            <td>
              <div>{strategyLabel(item.strategy_version)}</div>
              <div className="table-subtitle">{strategyDescription(item.strategy_version)}</div>
            </td>
            <td><StatusChip value={item.reason?.validation_level || item.validation_level || 'ENGINEERING_ONLY'} label={validationChip(item.reason?.validation_level || 'ENGINEERING_ONLY')} raw /></td>
            <td>
              <div>买入 ¥{item.entry_low} – ¥{item.entry_high}</div>
              <div className="table-subtitle">目标 ¥{item.target_low} · 风险 ¥{item.stop_price}</div>
            </td>
            <td><StatusChip value={item.status} raw /></td>
            <td onClick={e => e.stopPropagation()}><Link className="table-link" to={`/market?code=${item.asset_id}`}>行情分析 →</Link></td>
          </tr>
        })}
      </Table> : <EmptyState icon="activity" title="没有候选" detail="零候选是合法结果。可能原因：数据有缺口、无标的满足条件、或尚未运行扫描。运行扫描不会修改策略参数或消耗新 OOS。" action={<button className="button-link" type="button" onClick={runScan} disabled={!!busy}>{busy === 'scan' ? '扫描中…' : '运行扫描'}</button>} />}

      {candidates.length > 0 && expandedCandidate && (() => {
        const item = candidates.find((c: Json) => c.plan_id === expandedCandidate)
        if (!item) return null
        return <div style={{ marginTop: '16px', padding: '16px', background: 'var(--q-bg-0)', borderRadius: '6px', border: '1px solid var(--q-border)' }}>
          <h4 style={{ margin: '0 0 12px', color: 'var(--q-text-strong)', fontSize: '14px' }}>
            {item.asset_id} · {strategyLabel(item.strategy_version)} 详细
          </h4>
          <div className="detail-grid">
            <div>观察买入区间<strong>¥{item.entry_low} – ¥{item.entry_high}</strong></div>
            <div>目标/减仓<strong>¥{item.target_low}</strong></div>
            <div>风险线<strong>¥{item.stop_price}</strong></div>
            <div>数据日期<strong>{scan?.data_date || '—'}</strong></div>
          </div>
          {item.invalidation && <div style={{ marginTop: '12px' }}>
            <div style={{ color: 'var(--q-text-muted)', fontSize: '11px', marginBottom: '4px' }}>失效条件</div>
            <div style={{ color: 'var(--q-danger)', fontSize: '12px' }}>
              追高放弃 &gt; ¥{item.invalidation.chase_above} · 风险退出 &lt; ¥{item.invalidation.risk_below} · 过期 {item.invalidation.expires_after}
            </div>
          </div>}
          {item.trigger && <div style={{ marginTop: '8px' }}>
            <div style={{ color: 'var(--q-text-muted)', fontSize: '11px', marginBottom: '4px' }}>触发条件</div>
            <div style={{ color: 'var(--q-text)', fontSize: '12px' }}>
              {item.trigger.enter_only_if || '—'} · 盘中自动触发：{item.trigger.live_trigger_enabled ? '已启用' : '未启用（Watch 验收前关闭）'}
            </div>
          </div>}
          {item.reason && <div style={{ marginTop: '8px' }}>
            <div style={{ color: 'var(--q-text-muted)', fontSize: '11px', marginBottom: '4px' }}>入选理由</div>
            <div style={{ color: 'var(--q-text)', fontSize: '12px' }}>
              {item.reason.name || '—'} · 排名非概率 · 数据日期 {item.reason.data_date || '—'}
            </div>
          </div>}
          <div style={{ marginTop: '12px', display: 'flex', gap: '8px' }}>
            <Link className="button-link small" to={`/market?code=${item.asset_id}`}>查看行情分析</Link>
            <Link className="button-link small secondary" to="/intraday">加入盘中观察</Link>
          </div>
        </div>
      })()}
    </Panel>

    <Panel eyebrow="数据覆盖" title="扫描池状态">
      <div className="detail-grid">
        <div>身份数据<strong>{coverage.identity_count || '—'}</strong></div>
        <div>市场资产<strong>{coverage.market_asset_count || '—'}</strong></div>
        <div>交叉覆盖<strong>{coverage.intersection_count || '—'}</strong></div>
        <div>缺失交易日<strong>{coverage.missing_open_sessions?.join(', ') || '无'}</strong></div>
        <div>停牌状态<strong>{coverage.current_suspend_state || '—'}</strong></div>
        <div>涨跌停状态<strong>{coverage.current_limit_state || '—'}</strong></div>
      </div>
      <div className="panel-lede" style={{ marginTop: '12px' }}>
        当前覆盖沪市主板、深市主板、创业板和科创板。排除 ST/退市/次新/极低流动性标的。扫描池与历史回测 universe 分开，不用今天存续股票回测全部历史。
      </div>
    </Panel>

    <Panel eyebrow="说明" title="验证级别含义">
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '12px' }}>
        <div style={{ padding: '12px', background: 'var(--q-bg-0)', borderRadius: '6px', border: '1px solid var(--q-border)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
            <StatusChip value="ENGINEERING_ONLY" label="工程版本" raw />
          </div>
          <div style={{ color: 'var(--q-text-muted)', fontSize: '11px', lineHeight: '1.5' }}>
            V1 原版策略。基于市场技术分析逻辑实现，但未经历史数据回测验证。仅供观察。
          </div>
        </div>
        <div style={{ padding: '12px', background: 'var(--q-bg-0)', borderRadius: '6px', border: '1px solid var(--q-border)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
            <StatusChip value="ENGINEERING_ONLY_V2" label="改进版 V2" raw />
          </div>
          <div style={{ color: 'var(--q-text-muted)', fontSize: '11px', lineHeight: '1.5' }}>
            在 V1 基础上增加一个改进（成交量确认/整理过滤/量能高潮）。改进基于市场微观结构逻辑，未经历史验证。
          </div>
        </div>
        <div style={{ padding: '12px', background: 'var(--q-bg-0)', borderRadius: '6px', border: '1px solid var(--q-border)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
            <StatusChip value="ENGINEERING_ONLY_NEW" label="新策略" raw />
          </div>
          <div style={{ color: 'var(--q-text-muted)', fontSize: '11px', lineHeight: '1.5' }}>
            跳空动量和相对强度策略。与现有策略有实质区别（跳空事件/市场对比），未经历史验证。
          </div>
        </div>
        <div style={{ padding: '12px', background: 'var(--q-bg-0)', borderRadius: '6px', border: '1px solid var(--q-border)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
            <StatusChip value="CANDIDATE" label="已验证" raw />
          </div>
          <div style={{ color: 'var(--q-text-muted)', fontSize: '11px', lineHeight: '1.5' }}>
            通过历史回测验证（仅 ETF 策略）。可用于 PAPER 模拟验证。
          </div>
        </div>
      </div>
      <div className="panel-lede" style={{ marginTop: '12px', color: 'var(--q-warning)' }}>
        所有短线候选都是工程版本，仅供研究观察，不是已验证的买入建议。排名不是上涨概率，风险线不是保证成交价。
      </div>
    </Panel>
  </div>
}
