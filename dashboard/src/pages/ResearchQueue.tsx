import { useEffect, useState } from 'react'
import { Badge, EmptyState, MetricStrip, Notice, PageHeader, Panel, StatusChip, Table, fmt, newId, postJson, secondaryButton } from '../ui'

export default function ResearchQueue({ embedded = false }: { embedded?: boolean }) {
  const [data, setData] = useState<any>(null)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const load = () => fetch('/api/v2/research/queue').then(response => response.json()).then(setData).catch((reason: Error) => setError(reason.message))
  useEffect(() => { load() }, [])

  const act = async (item: any, action: string) => {
    const id = item.queue_item_id || item.item_id
    setMessage(id + ': 正在执行 ' + action + '…')
    try {
      const result = await postJson('/api/v2/actions/queue/' + encodeURIComponent(id), { action, idempotency_key: newId('ui-queue-' + action) })
      setMessage(id + ': ' + (result.state || result.status || '已记录') + ' · ' + (result.reason || result.message || ''))
      if (action === 'refresh') load()
    } catch (reason: any) {
      setMessage(id + ': ' + reason.message)
    }
  }

  if (error) return <Notice tone="danger" title="研究队列加载失败">{error}<button type="button" className="text-button" onClick={load}>重新加载</button></Notice>
  if (!data) return <div className="page-loading">正在读取研究队列…</div>

  const v2 = data.v2_queue || []
  const legacy = data.legacy_queue || []
  const ready = v2.filter((item: any) => ['READY', 'QUEUED', 'PENDING'].includes(String(item.status || '').toUpperCase())).length
  const blocked = v2.filter((item: any) => String(item.status || '').toUpperCase().includes('BLOCK')).length

  return (
    <div>
      {!embedded && <PageHeader eyebrow="RESEARCH / QUEUE" title="研究队列" description="V2 queue 可执行范围与 Legacy 历史记录分层展示，禁止跨层直接运行。" actions={<button type="button" style={secondaryButton} onClick={load}>刷新队列</button>} />}
      <MetricStrip items={[{ label: 'V2 queue', value: fmt(v2.length, 0), hint: '当前队列' }, { label: 'Ready / queued', value: fmt(ready, 0), hint: '可操作项' }, { label: 'Blocked', value: fmt(blocked, 0), hint: '有阻断原因' }, { label: 'Legacy', value: fmt(legacy.length, 0), hint: '只读历史' }]} />
      {message && <div className="action-message">{message}</div>}

      <Panel eyebrow="CANONICAL QUEUE" title={'V2 Queue · ' + v2.length + ' 项'} className="queue-panel">
        {v2.length ? <Table headers={['Queue item', 'Protocol', 'Family', 'Priority', 'Status', 'Attempt', 'Action']} minWidth={920}>{v2.map((item: any) => <tr key={item.queue_item_id}><td className="mono">{item.queue_item_id}<div className="table-subtitle">{item.strategy_version_id || '—'}</div></td><td>{item.protocol_id || '—'}</td><td>{item.family_id || '—'}</td><td>{fmt(item.priority, 0)}</td><td><StatusChip value={item.status || '—'} raw /></td><td>{fmt(item.attempt, 0)}</td><td><div className="table-actions"><button type="button" className="button-link secondary small" onClick={() => act(item, 'refresh')}>刷新证据</button><button type="button" className="button-link small" onClick={() => act(item, 'run')}>运行</button></div></td></tr>)}</Table> : <EmptyState icon="queue" title="暂无 V2 queue" detail="没有队列项时保持空态，不创建模拟任务。" />}
      </Panel>

      <Panel eyebrow="LEGACY READ-ONLY" title={'历史队列 · ' + legacy.length + ' 项'} className="queue-panel">
        {legacy.length ? <Table headers={['Item', 'Target', 'Type', 'Historical status', 'Current step', 'V2 qualification', 'Action']} minWidth={1040}>{legacy.map((item: any) => <tr key={item.item_id} className="legacy-row"><td className="mono">{item.item_id}</td><td>{item.target || '—'}</td><td>{item.kind || '—'}</td><td><StatusChip value={item.status || '—'} raw /></td><td>{item.current_step || 'READONLY_IMPORT'}</td><td className="table-subtitle">{(item.block_reasons || []).join(' · ') || '—'}</td><td><button type="button" className="button-link secondary small" disabled={!item.run_allowed} onClick={() => act(item, 'run')}>只读 · 不运行</button></td></tr>)}</Table> : <EmptyState icon="database" title="暂无 Legacy queue" detail="Legacy 数据保持只读，不会进入 V2 执行队列。" />}
        <div className="panel-footnote"><Badge value="READ_ONLY" /> Legacy queue 只用于追溯，不参与当前资格和执行。</div>
      </Panel>
    </div>
  )
}
