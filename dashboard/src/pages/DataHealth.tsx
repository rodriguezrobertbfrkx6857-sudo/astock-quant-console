import { useEffect, useState } from 'react'
import { Badge, EmptyState, MetricStrip, Notice, PageHeader, Panel, StatusChip, Table, TechnicalDetails, fmt, secondaryButton } from '../ui'

export default function DataHealth() {
  const [health, setHealth] = useState<any>(null)
  const [error, setError] = useState('')
  const load = () => fetch('/api/v2/data-health').then(response => response.json()).then(setHealth).catch((reason: Error) => setError(reason.message))
  useEffect(() => { load() }, [])

  if (error) return <Notice tone="danger" title="Data Health 加载失败">{error}<button type="button" className="text-button" onClick={load}>重新加载</button></Notice>
  if (!health) return <div className="page-loading">正在读取数据发布和覆盖状态…</div>
  const coverage = Object.entries(health.coverage || {}) as [string, any][]

  return (
    <div>
      <PageHeader eyebrow="SYSTEM / DATA HEALTH" title="数据健康" description="V2 REAL publication 与 Legacy 只读证据分层展示；只改变呈现方式，不改变数据质量门禁。" actions={<button type="button" style={secondaryButton} onClick={load}>刷新</button>} />
      <MetricStrip items={[{ label: 'REAL_READY manifests', value: fmt(health.real_ready_manifests, 0), hint: '已发布清单' }, { label: 'REAL rows', value: fmt(health.real_ready_rows, 0), hint: '真实行数' }, { label: 'Published datasets', value: fmt(health.published_dataset_count, 0), hint: '当前指针' }, { label: 'Legacy datasets', value: fmt(Object.keys(health.legacy_coverage || {}).length, 0), hint: '只读历史' }, { label: 'Provenance', value: <StatusChip value={health.overall_source || health.provenance_status || '—'} raw />, hint: '来源' }]} />
      <Notice tone="info" title="范围和来源">优先查看真实已发布数据；Legacy 只读可浏览，不能写入 V2 canonical 或自动取得资格。</Notice>
      <Panel eyebrow="PUBLISHED DATASETS" title={'已发布数据 · ' + coverage.length + ' 项'}>
        {coverage.length ? <Table headers={['Dataset', 'Status', 'Source', 'Coverage', 'Rows', 'Hash', 'Freshness']} minWidth={1040}>{coverage.map(([name, value]) => <tr key={name}><td>{name}</td><td><StatusChip value={value.status || '—'} raw /></td><td>{value.source || '—'}</td><td>{value.coverage?.start || '—'} → {value.coverage?.end || '—'}</td><td>{fmt(value.row_count, 0)}</td><td className="mono">{value.manifest_hash ? value.manifest_hash.slice(0, 16) + '…' : '—'}</td><td>{value.freshness || '—'}</td></tr>)}</Table> : <EmptyState icon="database" title="暂无已发布数据集" detail="保持真实空态，不用目录数量代替发布状态。" />}
        <div className="panel-footnote">Published 指针数：{health.published_dataset_count ?? '—'} · 首选数据存储：{health.provenance_status || '—'}</div>
      </Panel>
      <TechnicalDetails title="Data health evidence"><pre className="json-block">{JSON.stringify(health, null, 2)}</pre></TechnicalDetails>
    </div>
  )
}
