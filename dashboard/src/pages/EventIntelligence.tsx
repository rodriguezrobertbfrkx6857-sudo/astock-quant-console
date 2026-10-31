import { useEffect, useState } from 'react'
import { Badge, EmptyState, MetricStrip, Notice, PageHeader, Panel, StatusChip, Table, fmt, secondaryButton } from '../ui'

interface KindStat {
  kind: string
  count: number
  car_5: number | null
  car_10: number | null
  car_20: number | null
  hit_rate: number | null
}

const KIND_LABEL: Record<string, string> = {
  MA: '并购重组',
  SELLDOWN: '股东减持',
  BUYBACK: '股份回购',
  LEGAL: '法律诉讼',
  CONTRACT: '重大合同',
  DIVIDEND: '分红送转',
  INCENTIVE: '股权激励',
  EARN_PREANN_POS: '业绩预喜',
  EARN_PREANN_NEG: '业绩预亏',
  DELIST_RISK: '退市风险',
}

const kindLabel = (kind: string) => KIND_LABEL[kind] || kind

export default function EventIntelligence() {
  const [events, setEvents] = useState<any[]>([])
  const [stats, setStats] = useState<KindStat[]>([])
  const [kind, setKind] = useState('')
  const [total, setTotal] = useState(0)
  const [sourceState, setSourceState] = useState('PENDING_VENDOR')
  const [error, setError] = useState('')

  const load = (selectedKind: string) => {
    setError('')
    const url = '/api/v2/events?limit=50' + (selectedKind ? '&kind=' + encodeURIComponent(selectedKind) : '')
    fetch(url).then(response => response.json()).then(result => {
      setEvents(result.events || [])
      setStats(result.kind_stats || [])
      setTotal(result.count || 0)
      setSourceState(result.state || 'PENDING_VENDOR')
    }).catch((reason: Error) => setError(reason.message))
  }
  useEffect(() => { load('') }, [])

  const fmtPct = (value: number | null | undefined) => value == null ? '—' : value.toFixed(2) + '%'
  return (
    <div>
      <PageHeader eyebrow="MARKET / EVENTS" title="事件情报" description="PIT 事件、公告来源和研究窗口。事件能力独立于价格策略，不会因为供应商状态改变行情查看。" actions={<button type="button" style={secondaryButton} onClick={() => load(kind)}>刷新</button>} meta={<div>{total.toLocaleString()} events</div>} />
      <Notice tone="warn" title="事件来源状态">EVENT ENGINE = READY · LIVE VENDOR = <StatusChip value={sourceState} raw /> · 当前数据仅用于研究参考。</Notice>
      <MetricStrip items={[{ label: 'Events', value: fmt(total, 0), hint: '当前返回' }, { label: 'Kinds', value: fmt(stats.length, 0), hint: '事件类型' }, { label: 'PIT', value: 'ON', hint: '研究口径' }, { label: 'Source', value: <StatusChip value={sourceState} raw />, hint: '供应商状态' }]} />
      {error && <div className="action-message">{error}</div>}
      <div className="event-filters">{[<button type="button" key="all" className={kind === '' ? 'filter-chip active' : 'filter-chip'} onClick={() => { setKind(''); load('') }}>全部 {total.toLocaleString()}</button>, ...stats.map(stat => <button type="button" key={stat.kind} className={kind === stat.kind ? 'filter-chip active' : 'filter-chip'} onClick={() => { setKind(stat.kind); load(stat.kind) }}>{kindLabel(stat.kind)} {stat.count}</button>)]}</div>
      <Panel eyebrow="EVENT FEED" title={'最近事件 · ' + events.length + ' 项'}>
        {events.length ? <div className="event-list">{events.map(event => <div className="event-card" key={event.event_id}><div className="event-card-head"><div className="list-item-title">{event.title || kindLabel(event.event_type)}</div><span className="list-item-meta">{event.published_at || '—'}</span></div><div className="event-card-meta"><StatusChip value={event.event_type || '—'} label={kindLabel(event.event_type || '—')} /><span>{event.ts_code || '—'}</span><span className={Number(event.score) >= 0 ? 'market-up' : 'market-down'}>{event.score == null ? '—' : (event.score > 0 ? '+' : '') + event.score}</span></div></div>)}</div> : <EmptyState icon="events" title="该类型暂无事件" detail="空事件不代表策略故障，保持 PIT 研究口径。" />}
      </Panel>
      {stats.length > 0 && <Panel eyebrow="RESEARCH EFFECT" title="事件效果统计"><Table headers={['Event type', 'Samples', 'CAR 5d', 'CAR 10d', 'CAR 20d', 'Hit rate']} minWidth={720}>{stats.map(stat => <tr key={stat.kind} onClick={() => { setKind(stat.kind); load(stat.kind) }}><td><button type="button" className="table-link">{kindLabel(stat.kind)}</button><div className="table-subtitle">{stat.kind}</div></td><td>{fmt(stat.count, 0)}</td><td className={stat.car_5 == null ? '' : stat.car_5 >= 0 ? 'market-up' : 'market-down'}>{fmtPct(stat.car_5)}</td><td className={stat.car_10 == null ? '' : stat.car_10 >= 0 ? 'market-up' : 'market-down'}>{fmtPct(stat.car_10)}</td><td className={stat.car_20 == null ? '' : stat.car_20 >= 0 ? 'market-up' : 'market-down'}>{fmtPct(stat.car_20)}</td><td>{stat.hit_rate == null ? '—' : stat.hit_rate.toFixed(1) + '%'}</td></tr>)}</Table></Panel>}
    </div>
  )
}
