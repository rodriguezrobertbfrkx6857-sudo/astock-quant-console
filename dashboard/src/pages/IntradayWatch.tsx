import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { EmptyState, MetricStrip, Notice, PageHeader, Panel, StatusChip, fmt } from '../ui'

type Json = Record<string, any>

async function api(path: string, init?: RequestInit) {
  const response = await fetch(path, init)
  const body = await response.json()
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`)
  return body
}

export default function IntradayWatch() {
  const [overview, setOverview] = useState<Json | null>(null)
  const [plans, setPlans] = useState<Json[]>([])
  const [error, setError] = useState('')

  const load = async () => {
    setError('')
    try {
      const [o, p] = await Promise.all([
        api('/api/v2/short-term/overview'),
        api('/api/v2/short-term/plans?limit=100'),
      ])
      setOverview(o)
      setPlans(p.plans || [])
    } catch (reason) { setError((reason as Error).message) }
  }

  useEffect(() => { load() }, [])

  const watch = overview?.watch || {}
  const allPlans = plans.filter(p => p.status !== 'INVALIDATED')
  const watchStatus = watch.status || 'PENDING_VENDOR'
  const triggersDisabled = watch.live_triggers_enabled === false || watchStatus !== 'WATCH_FULLY_VERIFIED'

  return <div>
    <PageHeader
      eyebrow="盘中观察"
      title="条件监控与触发"
      description="监控已有持仓计划和候选的条件是否满足。盘中提示不是交易指令。数据来源、新鲜度和连续性都通过验收后才会启用自动触发。"
      meta={<div>LOCAL ONLY · 不自动下单</div>}
      actions={<button type="button" onClick={load}>刷新</button>}
    />

    {error && <Notice tone="danger" title="加载失败">{error}</Notice>}

    <Notice tone={triggersDisabled ? 'warn' : 'ok'} title={triggersDisabled ? '盘中触发当前关闭' : '盘中触发已启用'}>
      {triggersDisabled
        ? '实时行情供应商返回的数据未满足新鲜度、连续性、阶段窗口和恢复门槛。日线数据不会冒充 tick。在验收通过前，所有计划保持 TRIGGERS_DISABLED 状态。'
        : '行情数据已通过验收，条件触发已启用。请注意提示不是交易指令。'}
    </Notice>

    <MetricStrip items={[
      { label: 'Watch 状态', value: <StatusChip value={watchStatus} raw />, hint: watch.reason || '实时行情验收' },
      { label: '验证样本', value: fmt(watch.validated_sample_count || 0, 0), hint: '满足新鲜度的样本' },
      { label: '监控计划', value: fmt(allPlans.length, 0), hint: '当前相关计划' },
      { label: '自动触发', value: triggersDisabled ? 'OFF' : 'ON', hint: triggersDisabled ? '验收前关闭' : '已启用' },
    ]} />

    <Panel eyebrow="验收分项" title="Watch 验收进度">
      <div className="panel-lede">完整通过需要以下所有分项都满足。日线或盘后数据不能替代 tick/分钟验收。</div>
      <div className="detail-grid">
        <div>凭据路径<strong>{watchStatus === 'WATCH_FULLY_VERIFIED' ? '通过' : '待验证'}</strong></div>
        <div>实时行情<strong>{watchStatus === 'WATCH_FULLY_VERIFIED' ? '通过' : '待验证'}</strong></div>
        <div>分钟数据<strong>{watchStatus === 'WATCH_FULLY_VERIFIED' ? '通过' : '待验证'}</strong></div>
        <div>连续性 30 分钟<strong>{watchStatus === 'WATCH_FULLY_VERIFIED' ? '通过' : '待验证'}</strong></div>
        <div>恢复验收<strong>{watchStatus === 'WATCH_FULLY_VERIFIED' ? '通过' : '待验证'}</strong></div>
        <div>信号链<strong>{watchStatus === 'WATCH_FULLY_VERIFIED' ? '通过' : '待验证'}</strong></div>
      </div>
      <div style={{ marginTop: '16px', padding: '12px', background: 'var(--q-bg-0)', borderRadius: '6px', border: '1px solid var(--q-border)' }}>
        <div style={{ color: 'var(--q-text-muted)', fontSize: '11px', marginBottom: '4px' }}>最小本地验收步骤</div>
        <div style={{ color: 'var(--q-text)', fontSize: '12px', lineHeight: '1.7' }}>
          1. 在交易时段（09:30-11:30 或 13:00-15:00）启动系统<br />
          2. 确保已授权行情凭据已正确配置<br />
          3. 运行 realtime_feed_check.py 检查实时样本<br />
          4. 验证连续性窗口和阶段窗口<br />
          5. 注入断连测试恢复路径
        </div>
      </div>
    </Panel>

    <Panel eyebrow="当前计划" title={`监控列表 · ${allPlans.length} 条`}>
      {allPlans.length ? allPlans.slice(0, 30).map(item => (
        <div className="data-row" key={item.plan_id}>
          <div>
            <div className="list-item-title">{item.asset_id} · {item.plan_type}</div>
            <div className="list-item-meta">
              观察 {item.entry_low}–{item.entry_high} · 风险 {item.stop_price} · 目标 {item.target_low}
            </div>
          </div>
          <StatusChip value={triggersDisabled ? 'TRIGGERS_DISABLED' : item.status} raw />
        </div>
      )) : <EmptyState icon="watch" title="暂无监控计划" detail="生成持仓计划或运行候选扫描后，相关计划会出现在这里。当前触发关闭时，所有计划显示 TRIGGERS_DISABLED。" action={<><Link className="button-link small" to="/holdings">管理持仓</Link><Link className="button-link small secondary" to="/opportunities" style={{ marginLeft: '8px' }}>查看候选</Link></>} />}
    </Panel>

    <Panel eyebrow="安全规则" title="触发安全约束">
      <div className="panel-lede">
        以下规则在任何触发生效前执行：
      </div>
      <div style={{ color: 'var(--q-text)', fontSize: '12px', lineHeight: '1.8' }}>
        · 数据过期超过 90 秒 → 立即停止新触发<br />
        · 未知状态 → 不生成操作提示<br />
        · 同一分钟内多个价位触及 → 拒绝或保守路径，不选最有利顺序<br />
        · 重复告警去重 → 同一计划/触发类型/时间戳只产生一次信号<br />
        · 断线恢复 → 新数据须在 120 秒内恢复且无重复接收<br />
        · 日线不冒充 tick → 盘后日线数据不触发盘中操作
      </div>
    </Panel>
  </div>
}
