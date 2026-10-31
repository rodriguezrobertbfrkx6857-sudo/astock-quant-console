import { useEffect, useState } from 'react'
import { Badge, EmptyState, MetricStrip, Notice, PageHeader, Panel, StatusChip, Table, TechnicalDetails, fmt, newId, postJson, secondaryButton } from '../ui'

export default function Factors({ embedded = false }: { embedded?: boolean }) {
  const [data, setData] = useState<any>(null)
  const [selected, setSelected] = useState<any>(null)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const load = () => {
    setError('')
    fetch('/api/v2/research/factors?limit=200').then(response => response.json()).then(setData).catch((reason: Error) => setError(reason.message))
  }
  useEffect(() => { load() }, [])

  const open = (item: any) => {
    setSelected(item)
    fetch('/api/v2/research/factors/' + encodeURIComponent(item.factor_id))
      .then(response => response.json()).then(result => setSelected(result.factor || result)).catch(() => {})
  }

  const evaluate = async (item: any) => {
    setMessage('正在提交 ' + item.factor_id + ' 的 V2 因子评估请求…')
    try {
      const result = await postJson('/api/v2/actions/evaluate-factor', { factor_id: item.factor_id, idempotency_key: newId('ui-evaluate-factor') })
      setMessage(item.factor_id + ': ' + (result.state || result.status || '已记录') + ' · ' + (result.reason || result.message || ''))
    } catch (reason: any) {
      setMessage(item.factor_id + ': ' + reason.message)
    }
  }

  if (error) return <Notice tone="danger" title="因子目录加载失败">{error}<button type="button" className="text-button" onClick={load}>重新加载</button></Notice>
  if (!data) return <div className="page-loading">正在读取因子目录…</div>

  const rows = data.factors || []
  const legacy = rows.filter((item: any) => item.provenance === 'LEGACY_EVIDENCE')
  const canonical = rows.filter((item: any) => item.provenance !== 'LEGACY_EVIDENCE')
  const current = canonical.filter((item: any) => item.executable !== false)
  const superseded = canonical.filter((item: any) => item.superseded || item.lifecycle === 'SUPERSEDED_AUDIT_ONLY')

  return (
    <div>
      {!embedded && <PageHeader eyebrow="RESEARCH / FACTORS" title="因子研究" description="展示 IC、RankIC、覆盖率、换手和当前研究状态；Legacy 仅供历史证据复核。" actions={<button type="button" style={secondaryButton} onClick={load}>刷新目录</button>} />}
      <MetricStrip items={[
        { label: '因子总数', value: fmt(rows.length, 0), hint: '真实返回' },
        { label: 'V2 current', value: fmt(current.length, 0), hint: '可提交评估' },
        { label: 'V2 superseded', value: fmt(superseded.length, 0), hint: '审计只读' },
        { label: 'Legacy evidence', value: fmt(legacy.length, 0), hint: '历史只读' },
      ]} />
      {message && <div className="action-message">{message}</div>}
      <div className="section-header"><div><div className="eyebrow">FACTOR REGISTER</div><h2>因子目录</h2><p>没有真实 Stability 字段时保持 —，不从 IC 序列擅自推导。</p></div>{legacy.length > 0 && <Badge value="LEGACY READ-ONLY" />}</div>
      {rows.length ? <Table headers={['Factor', 'Category', 'Source', 'IC', 'RankIC', 'Coverage', 'Stability', 'Turnover', 'Status', 'Action']} minWidth={1080}>{rows.map((item: any) => {
        const stats = item.stats || {}
        const isLegacy = item.provenance === 'LEGACY_EVIDENCE'
        const isSuperseded = item.superseded || item.lifecycle === 'SUPERSEDED_AUDIT_ONLY'
        const dependencyBlocked = item.dependency_ready === false
        const status = isSuperseded ? 'SUPERSEDED_AUDIT_ONLY' : item.accepted ? 'ACCEPTED' : item.research_status || 'RESEARCH_ONLY'
        return <tr key={item.factor_id} className={isLegacy || isSuperseded ? 'legacy-row' : ''}><td><button type="button" className="table-link" onClick={() => open(item)}>{item.factor_id}</button><div className="table-subtitle">{item.name || '—'}</div></td><td>{item.category || '—'}</td><td><StatusChip value={item.provenance || item.source_kind || '—'} raw /></td><td className="mono">{fmt(stats.ic_mean, 4)}</td><td className="mono">{fmt(stats.rank_ic_mean, 4)}</td><td>{stats.coverage_display || '—'}</td><td>{stats.stability == null ? '—' : fmt(stats.stability, 4)}</td><td className="mono">{stats.turnover == null ? '—' : fmt(stats.turnover, 4)}</td><td><StatusChip value={status} raw /><div className="table-subtitle">{item.block_reasons?.[0] || ''}</div></td><td><div className="table-actions"><button type="button" className="button-link secondary small" onClick={() => open(item)}>详情</button>{!isLegacy && !isSuperseded && !dependencyBlocked && <button type="button" className="button-link small" onClick={() => evaluate(item)}>评估</button>}{dependencyBlocked && <span className="table-subtitle">等待依赖</span>}</div></td></tr>
      })}</Table> : <EmptyState icon="layers" title="暂无因子定义" detail="API 没有返回因子目录，不使用空白卡片制造数量。" />}
      {selected && <Panel eyebrow="FACTOR PROFILE" title={'因子详情 · ' + selected.factor_id} className="detail-panel">
        <div className="detail-grid"><div>来源 <StatusChip value={selected.provenance || selected.source_kind || '—'} raw /></div><div>IC mean <strong>{fmt(selected.stats?.ic_mean, 5)}</strong></div><div>RankIC mean <strong>{fmt(selected.stats?.rank_ic_mean, 5)}</strong></div><div>覆盖行数 <strong>{fmt(selected.coverage?.rows, 0)}</strong></div><div>换手 <strong>{fmt(selected.stats?.turnover, 5)}</strong></div><div>IC 序列 <strong>{fmt(selected.ic_daily?.length, 0)}</strong></div></div>
        <TechnicalDetails title="Factor technical evidence"><pre className="json-block">{JSON.stringify({ params: selected.params, stats: selected.stats, evaluations: selected.evaluations, evaluation: selected.evaluation, stats_history: selected.stats_history, coverage: selected.coverage, block_reasons: selected.block_reasons, source: selected.source }, null, 2)}</pre></TechnicalDetails>
      </Panel>}
    </div>
  )
}
