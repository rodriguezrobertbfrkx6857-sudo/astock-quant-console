import { useEffect, useState } from 'react'
import { Badge, EmptyState, Notice, PageHeader, Panel, StatusChip, Table, TechnicalDetails, button, fmt, secondaryButton } from '../ui'

export default function DataExplorer() {
  const [catalog, setCatalog] = useState<any>(null)
  const [code, setCode] = useState('510300.SH')
  const [sample, setSample] = useState<any>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => { fetch('/api/v2/data/catalog').then(response => response.json()).then(setCatalog).catch((reason: Error) => setError(reason.message)) }, [])
  const loadSample = () => {
    setLoading(true)
    fetch('/api/v2/data/sample?ts_code=' + encodeURIComponent(code) + '&limit=25').then(response => response.json()).then(setSample).catch((reason: Error) => setError(reason.message)).finally(() => setLoading(false))
  }
  useEffect(() => { loadSample() }, [])

  if (error && !catalog) return <Notice tone="danger" title="数据目录加载失败">{error}</Notice>
  if (!catalog) return <div className="page-loading">正在加载 V2 / Legacy 数据目录…</div>
  const rows = catalog.datasets || []

  return (
    <div>
      <PageHeader eyebrow="SYSTEM / DATA PROVIDER" title="数据浏览" description="默认读取 V2 canonical 样本；仅在 canonical 不存在时按需回退 Legacy 只读证据。" meta={<div>{rows.length} datasets · lazy Legacy {catalog.lazy_legacy ? 'ON' : 'OFF'}</div>} actions={<button type="button" style={secondaryButton} onClick={loadSample}>重新读取样本</button>} />
      <Panel eyebrow="SAMPLE QUERY" title="证券样本">
        <div className="search-bar"><input className="search-input" value={code} onChange={event => setCode(event.target.value)} onKeyDown={event => event.key === 'Enter' && loadSample()} aria-label="证券代码" /><button type="button" style={button} onClick={loadSample}>{loading ? '查询中…' : '读取样本'}</button></div>
        {sample ? <><div className="provenance-line">状态 <StatusChip value={sample.state || '—'} raw /> · 来源 {sample.provenance || sample.source?.source_kind || '—'} · 返回 {sample.count || 0} 行</div>{sample.sample?.length ? <Table headers={['Code', 'Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'Source']} minWidth={900}>{sample.sample.map((row: any, index: number) => <tr key={row.ts_code + '-' + row.trade_date + '-' + index}><td>{row.ts_code}</td><td>{row.trade_date}</td><td>{fmt(row.open, 3)}</td><td>{fmt(row.high, 3)}</td><td>{fmt(row.low, 3)}</td><td>{fmt(row.close, 3)}</td><td>{fmt(row.vol, 0)}</td><td><StatusChip value={row.provenance || row.source_table || '—'} raw /></td></tr>)}</Table> : <EmptyState icon="database" title="样本为空" detail="API 没有返回样本行。" />}</> : <EmptyState icon="database" title="尚未读取样本" />}
      </Panel>
      <Panel eyebrow="CATALOG" title={'已发布与只读数据目录 · ' + rows.length + ' 项'}>
        {rows.length ? <Table headers={['Dataset / Table', 'Source', 'Status', 'Rows', 'Codes', 'Date coverage', 'Version / note']} minWidth={1120}>{rows.map((row: any, index: number) => { const manifest = row.manifest || row; const coverage = row.coverage || {}; return <tr key={(row.dataset || row.name || index) + '-' + index}><td><strong>{row.dataset || row.name || '—'}</strong><div className="table-subtitle">{row.table || row.source_table || ''}</div></td><td><StatusChip value={row.source_kind || row.source || '—'} raw /></td><td><StatusChip value={manifest.readiness || row.status || row.publication_state || '—'} raw /></td><td>{fmt(manifest.row_count ?? row.row_count ?? row.total_rows, 0)}</td><td>{fmt(manifest.unique_codes ?? row.unique_codes, 0)}</td><td>{manifest.earliest_date || row.earliest_date || coverage.start || '—'} → {manifest.latest_date || row.latest_date || coverage.end || '—'}</td><td className="table-subtitle">{manifest.version || row.manifest_id || row.detail || '—'}</td></tr> })}</Table> : <EmptyState icon="database" title="目录为空" />}
        <div className="panel-footnote">Canonical datasets：{catalog.canonical_datasets?.length || 0} · Legacy datasets：{catalog.legacy_datasets?.length || 0} · Legacy databases：{catalog.legacy_databases?.length || 0}</div>
      </Panel>
      {error && <div className="action-message">{error}</div>}
      {sample?.source && <TechnicalDetails title="Sample provenance"><pre className="json-block">{JSON.stringify(sample.source, null, 2)}</pre></TechnicalDetails>}
    </div>
  )
}
