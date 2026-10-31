import type { CSSProperties, ReactNode } from 'react'

export const panel: CSSProperties = {
  background: 'var(--q-surface-1)',
  border: '1px solid var(--q-border)',
  borderRadius: 8,
  padding: 16,
}

export const muted: CSSProperties = {
  color: 'var(--q-text-muted)',
  fontSize: 12,
}

export const button: CSSProperties = {
  background: 'var(--q-gold)',
  color: '#14110a',
  border: '1px solid var(--q-gold)',
  padding: '8px 13px',
  borderRadius: 6,
  cursor: 'pointer',
  fontSize: 12,
  fontWeight: 650,
  lineHeight: 1.2,
}

export const secondaryButton: CSSProperties = {
  ...button,
  background: 'var(--q-surface-2)',
  border: '1px solid var(--q-border-strong)',
  color: 'var(--q-text)',
}

export function tone(value: unknown): string {
  const text = String(value ?? '').toUpperCase()
  if (
    ['PASS', 'READY', 'SUCCEEDED', 'REAL_READY', 'OK', 'TRUE', 'ACTIVE', 'RUNNING', 'IDLE', 'VERIFIED', 'YES', 'ACCEPTED', 'READY_DECISION_SUPPORT', 'READY_NO_SCAN', 'TRIGGERED_DAILY_OBSERVATION_ONLY', 'SETTLED_OBSERVATIONS'].includes(text)
  ) return 'var(--q-system-ok)'
  if (text.includes('BLOCKED_DATA') || text.includes('OBSERVATION_ONLY') || text === 'BLOCKED_MINUTE_DATA') return 'var(--q-warning)'
  if (
    text.includes('BLOCK') ||
    ['FAIL', 'REJECT', 'FAILED', 'ERROR', 'DENIED'].some(item => text === item || text.startsWith(item + '_'))
  ) return 'var(--q-danger)'
  if (
    text.includes('PENDING') ||
    text.includes('WAITING') ||
    text.includes('DEGRADED') ||
    text.includes('PARTIAL') ||
    text.includes('NO_ELIGIBLE') ||
    text.includes('ENGINEERING_ONLY') ||
    text === 'INTRADAY_UNVERIFIED_DISABLED' ||
    text === 'TRIGGERS_DISABLED' ||
    text === 'NO_CANDIDATES' ||
    ['PAUSE', 'PAUSED'].includes(text)
  ) return 'var(--q-warning)'
  if (['DISABLED', 'FALSE', 'NO', 'RESEARCH_ONLY', 'AUDIT_ONLY', 'HISTORICAL', 'NOT_RUN', 'NOT_READY'].some(item => text === item || text.startsWith(item + '_'))) return 'var(--q-text-muted)'
  if (text.includes('NOT_TRIGGERED') || text.includes('UNDETERMINED') || text.includes('INVALIDATED')) return 'var(--q-text-muted)'
  return 'var(--q-text-muted)'
}

export function friendlyStatus(value: unknown): string {
  const text = String(value ?? '').toUpperCase()
  if (!text || text === '—') return '—'
  if (['PASS', 'READY', 'SUCCEEDED', 'REAL_READY', 'OK', 'TRUE', 'ACTIVE', 'RUNNING', 'VERIFIED', 'ACCEPTED', 'READY_DECISION_SUPPORT', 'READY_NO_SCAN'].includes(text)) return '可用'
  if (['DISABLED', 'FALSE', 'NO', 'TRIGGERS_DISABLED', 'INTRADAY_UNVERIFIED_DISABLED'].includes(text)) return '未启用'
  if (text.includes('LEGACY') || text.includes('HISTORICAL') || text === 'AUDIT_ONLY' || text === 'RESEARCH_ONLY') return '历史只读'
  if (text.includes('BLOCKED_DATA') || text === 'BLOCKED_MINUTE_DATA') return '等待数据'
  if (text === 'REJECT' || text.startsWith('REJECT_')) return '未通过'
  if (text.includes('BLOCK') || ['FAIL', 'REJECT', 'FAILED', 'ERROR', 'FALSE', 'NO', 'DENIED'].some(item => text === item || text.startsWith(item + '_'))) return '不可用'
  if (text.includes('OBSERVATION_ONLY')) return '仅供观察'
  if (text === 'NO_CANDIDATES') return '零候选'
  if (text === 'ENGINEERING_ONLY' || text === 'ENGINEERING_ONLY_UNVALIDATED') return '工程版本'
  if (text.includes('WAIT') || text.includes('PENDING') || text.includes('NO_ELIGIBLE') || text.includes('DEGRADED') || text === 'PAUSED' || text === 'PAUSE') return '等待数据'
  if (text.includes('PARTIAL')) return '部分可用'
  if (text === 'NOT_RUN') return '未运行'
  if (text === 'NOT_READY') return '未就绪'
  if (['CONSERVATIVE'].includes(text)) return '保守'
  if (['BALANCED'].includes(text)) return '均衡'
  if (['AGGRESSIVE'].includes(text)) return '积极'
  if (text.includes('SETTLED') || text.includes('TRIGGERED')) return '已结算'
  if (text.includes('NOT_TRIGGERED')) return '未触发'
  if (text.includes('UNDETERMINED')) return '无法判定'
  if (text.includes('INVALIDATED')) return '已失效'
  return String(value)
}

export function statusClass(value: unknown): string {
  const color = tone(value)
  if (color === 'var(--q-system-ok)') return 'status-ok'
  if (color === 'var(--q-danger)') return 'status-danger'
  if (color === 'var(--q-warning)') return 'status-warn'
  return 'status-neutral'
}

export function Badge({ value, label }: { value: string | number | boolean | null | undefined; label?: string }) {
  const text = value == null || value === '' ? '—' : String(value)
  return <span className={'status-chip ' + statusClass(text)} title={text}>{label || text}</span>
}

export function StatusChip({ value, label, raw = false }: { value: string | number | boolean | null | undefined; label?: string; raw?: boolean }) {
  const text = value == null || value === '' ? '—' : String(value)
  return <span className={'status-chip ' + statusClass(text)} title={text}>{label || (raw ? text : friendlyStatus(text))}</span>
}

export function Panel({ title, eyebrow, children, style, className = '' }: { title?: string; eyebrow?: string; children: ReactNode; style?: CSSProperties; className?: string }) {
  return (
    <section className={'panel ' + className} style={style}>
      {(title || eyebrow) && <div className="panel-header"><div>{eyebrow && <div className="eyebrow">{eyebrow}</div>}{title && <h3>{title}</h3>}</div></div>}
      {children}
    </section>
  )
}

export interface MetricItem {
  label: string
  value: ReactNode
  hint?: ReactNode
  tone?: 'up' | 'down' | 'ok' | 'warn' | 'danger' | 'neutral'
  mono?: boolean
}

export function MetricStrip({ items, className = '' }: { items: MetricItem[]; className?: string }) {
  return (
    <div className={'metric-strip ' + className}>
      {items.map((item, index) => (
        <div className="metric-item" key={item.label + '-' + index}>
          <div className="metric-label">{item.label}</div>
          <div className={'metric-value ' + (item.tone ? 'metric-' + item.tone : '') + (item.mono ? ' mono' : '')}>{item.value}</div>
          {item.hint != null && <div className="metric-hint">{item.hint}</div>}
        </div>
      ))}
    </div>
  )
}

export function PageHeader({ eyebrow, title, description, actions, meta }: { eyebrow?: string; title: string; description?: ReactNode; actions?: ReactNode; meta?: ReactNode }) {
  return (
    <header className="page-header">
      <div className="page-heading">
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {(actions || meta) && <div className="page-header-actions">{meta && <div className="page-meta">{meta}</div>}{actions && <div className="action-row">{actions}</div>}</div>}
    </header>
  )
}

export function SectionHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="section-header">
      <div>{eyebrow && <div className="eyebrow">{eyebrow}</div>}<h2>{title}</h2>{description && <p>{description}</p>}</div>
      {actions && <div className="action-row">{actions}</div>}
    </div>
  )
}

export function ChartFrame({ eyebrow, title, meta, children, className = '' }: { eyebrow?: string; title: string; meta?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={'chart-frame ' + className}>
      <div className="chart-frame-header">
        <div>{eyebrow && <div className="eyebrow">{eyebrow}</div>}<h2>{title}</h2></div>
        {meta && <div className="chart-meta">{meta}</div>}
      </div>
      <div className="chart-frame-body">{children}</div>
    </section>
  )
}

export function EmptyState({ title, detail, action, icon = 'chart' }: { title: string; detail?: ReactNode; action?: ReactNode; icon?: IconName }) {
  return <div className="empty-state"><Icon name={icon} size={24} /><div className="empty-title">{title}</div>{detail && <div className="empty-detail">{detail}</div>}{action && <div className="empty-action">{action}</div>}</div>
}

export function Notice({ tone: noticeTone = 'warn', title, children }: { tone?: 'ok' | 'warn' | 'danger' | 'info'; title?: string; children: ReactNode }) {
  return <div className={'notice notice-' + noticeTone}><div className="notice-mark" /><div>{title && <strong>{title}</strong>}<div>{children}</div></div></div>
}

export function TechnicalDetails({ title = 'Technical Details', children, open = false }: { title?: string; children: ReactNode; open?: boolean }) {
  return <details className="technical-details" open={open}><summary><Icon name="system" size={14} />{title}<span className="summary-caret">⌄</span></summary><div className="technical-body">{children}</div></details>
}

export function Tabs({ items, value, onChange, className = '' }: { items: { value: string; label: string; count?: ReactNode }[]; value: string; onChange: (value: string) => void; className?: string }) {
  return <div className={'tabs ' + className} role="tablist">{items.map(item => <button key={item.value} type="button" role="tab" aria-selected={value === item.value} className={value === item.value ? 'tab active' : 'tab'} onClick={() => onChange(item.value)}>{item.label}{item.count != null && <span className="tab-count">{item.count}</span>}</button>)}</div>
}

export function fmt(value: unknown, digits = 2): string {
  if (value == null || value === '') return '—'
  const scalar = metricScalar(value)
  const number = Number(scalar)
  if (!Number.isFinite(number)) return String(metricDisplay(value))
  return number.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

export function pct(value: unknown, digits = 2): string {
  if (value == null || value === '') return '—'
  const display = metricDisplay(value)
  if (typeof display === 'string' && /%$/.test(display.trim())) return display
  const number = Number(metricScalar(value))
  if (!Number.isFinite(number)) return String(display)
  return new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: digits }).format(number)
}

export function metricScalar(value: unknown): unknown {
  if (value && typeof value === 'object') {
    const metric = value as Record<string, unknown>
    if (metric.value != null) return metric.value
    if (metric.raw_value != null) return metric.raw_value
  }
  return value
}

export function metricDisplay(value: unknown): unknown {
  if (value && typeof value === 'object') {
    const metric = value as Record<string, unknown>
    if (metric.display_value != null) return metric.display_value
    if (metric.value != null) return metric.value
    if (metric.raw_value != null) return metric.raw_value
  }
  return value
}

export function newId(prefix: string): string {
  return prefix + '-' + Date.now() + '-' + Math.random().toString(16).slice(2)
}

export async function postJson(path: string, payload: Record<string, unknown>): Promise<any> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body?.detail?.message || body?.message || 'HTTP ' + response.status)
  return body
}

export function Table({ headers, children, minWidth = 760, className = '' }: { headers: string[]; children: ReactNode; minWidth?: number; className?: string }) {
  return <div className={'table-wrap ' + className}><table style={{ minWidth }}><thead><tr>{headers.map(header => <th key={header}>{header}</th>)}</tr></thead><tbody>{children}</tbody></table></div>
}

export function JsonBlock({ value }: { value: unknown }) {
  return <pre className="json-block">{JSON.stringify(value, null, 2)}</pre>
}

export type IconName = 'home' | 'market' | 'research' | 'backtest' | 'paper' | 'watch' | 'system' | 'database' | 'layers' | 'queue' | 'events' | 'search' | 'refresh' | 'chart' | 'activity' | 'shield' | 'chevron' | 'briefcase' | 'spark' | 'calendar' | 'filter' | 'external'

const ICON_PATHS: Record<IconName, string[]> = {
  home: ['M3 10.5 12 3l9 7.5', 'M5.5 9.5V21h13V9.5', 'M9 21v-6h6v6'],
  market: ['M4 19V9', 'M10 19V5', 'M16 19v-8', 'M22 19H2', 'm15 4 3-3 2 2'],
  research: ['M5 3h10l4 4v14H5z', 'M15 3v5h4', 'M8 12h8M8 16h6', 'm15.5 15.5 4 4'],
  backtest: ['M4 19V5', 'M4 19h17', 'm8 15 3-5 3 3 4-7'],
  paper: ['M6 3h12v18H6z', 'M9 7h6M9 11h6M9 15h4'],
  watch: ['M3 12s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6Z', 'M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z'],
  system: ['M12 3v3M12 18v3M3 12h3M18 12h3', 'm5.6 5.6 2.1 2.1m8.6 8.6 2.1 2.1m0-12.8-2.1 2.1m-8.6 8.6-2.1 2.1', 'M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z'],
  database: ['M4 5c0-2 16-2 16 0v14c0 2-16 2-16 0Z', 'M4 5c0 2 16 2 16 0M4 12c0 2 16 2 16 0'],
  layers: ['m12 3 9 4.5-9 4.5-9-4.5z', 'm3 12 9 4.5 9-4.5', 'm3 16.5 9 4.5 9-4.5'],
  queue: ['M5 4h14v16H5z', 'M8 8h8M8 12h8M8 16h5'],
  events: ['M4 5h16v12H8l-4 4z', 'M8 9h8M8 13h5'],
  search: ['m20 20-4.5-4.5', 'M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Z'],
  refresh: ['M20 11a8 8 0 0 0-14-4L4 9', 'M4 5v4h4', 'M4 13a8 8 0 0 0 14 4l2-2', 'M20 19v-4h-4'],
  chart: ['M4 19V5', 'M4 19h17', 'm7 15 3-4 3 2 5-7'],
  activity: ['M3 12h4l2-7 4 14 2-7h6'],
  shield: ['M12 3 20 6v5c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6z', 'm8.5 12 2.2 2.2 4.8-5'],
  chevron: ['m7 10 5 5 5-5'],
  briefcase: ['M4 7h16v13H4z', 'M9 7V4h6v3', 'M4 12h16'],
  spark: ['m12 3 1.7 5.3L19 10l-5.3 1.7L12 17l-1.7-5.3L5 10l5.3-1.7z', 'm19 16 .7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7z'],
  calendar: ['M5 4h14v16H5z', 'M8 2v4M16 2v4M5 9h14', 'M9 13h2M13 13h2M9 17h2'],
  filter: ['M4 5h16l-6 7v6l-4 2v-8z'],
  external: ['M14 4h6v6', 'M20 4 11 13', 'M18 13v6H4V5h6'],
}

export function Icon({ name, size = 18, strokeWidth = 1.8 }: { name: IconName; size?: number; strokeWidth?: number }) {
  return <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{ICON_PATHS[name].map((path, index) => <path key={index} d={path} />)}</svg>
}
