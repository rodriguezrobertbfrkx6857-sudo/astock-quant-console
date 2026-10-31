import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import Factors from './Factors'
import ResearchQueue from './ResearchQueue'
import Strategies from './Strategies'
import { EmptyState, MetricStrip, Notice, PageHeader, Panel, StatusChip, Table, Tabs, TechnicalDetails, fmt } from '../ui'

const TABS = [
  { value: 'strategies', label: 'Strategies' },
  { value: 'factors', label: 'Factors' },
  { value: 'runs', label: 'Runs' },
  { value: 'queue', label: 'Queue' },
]

function normalizeTab(value: string | null | undefined) {
  return TABS.some(item => item.value === value) ? value as string : 'strategies'
}

function RunView({ view }: { view: any }) {
  const runs = Array.isArray(view?.runs) ? view.runs : []
  const stage09 = Array.isArray(view?.stage09) ? view.stage09 : []
  const stage10 = Array.isArray(view?.stage10) ? view.stage10 : []
  const summary = view?.run_summary || {}
  const control = runs.find((run: any) => run.execution_scope === 'CONTROL')
  return (
    <div className="section-stack">
      <Panel eyebrow="RUN INVENTORY" title="研究运行">
        <div className="run-summary-grid">
          <div><span>独立 research runs</span><strong>{fmt(summary.research_runs ?? runs.length, 0)}</strong></div>
          <div><span>strategy runs</span><strong>{fmt(summary.strategy_runs, 0)}</strong></div>
          <div><span>control runs</span><strong>{fmt(summary.control_runs, 0)}</strong></div>
          <div><span>executor</span><StatusChip value={summary.executor_status || (view?.control_only ? 'CONTROL_ONLY' : 'READY')} raw /></div>
        </div>
        {view?.control_only && <Notice tone="warn" title="当前研究口径只有 CONTROL">CONTROL 结果不能升级为策略资格；策略、因子和 PAPER 资格仍以各自真实链路为准。</Notice>}
        {control && <TechnicalDetails title="CONTROL run context"><div className="detail-grid"><div>Research Run <strong>{control.research_run_id || '—'}</strong></div><div>Scope <strong>{control.execution_scope || '—'}</strong></div><div>OOS receipt <strong>{control.oos_receipt_id || '—'}</strong></div><div>Stage09 <strong>{control.stage09_status || 'CONTROL_NEVER_PROMOTES'}</strong></div></div></TechnicalDetails>}
      </Panel>

      <Panel eyebrow="RUNS" title={'全部运行 · ' + runs.length + ' 项'}>
        {runs.length ? <Table headers={['Run', 'Scope', 'Strategy', 'Business date', 'Status', 'Snapshot', 'Updated']} minWidth={980}>{runs.map((run: any, index: number) => <tr key={run.research_run_id || run.run_id || index}><td className="mono">{run.research_run_id || run.run_id || '—'}</td><td><StatusChip value={run.execution_scope || '—'} raw /></td><td>{run.strategy_id || run.strategy_name || '—'}</td><td>{run.business_date || run.trade_date || run.as_of || '—'}</td><td><StatusChip value={run.status || run.state || '—'} raw /></td><td className="mono">{String(run.snapshot || run.publication_id || '—').slice(0, 18)}</td><td>{run.updated_at || run.completed_at || '—'}</td></tr>)}</Table> : <EmptyState icon="research" title="暂无 research run" detail="当前没有可展示的运行记录；不要用 CONTROL-only 数量伪装成策略运行。" />}
      </Panel>

      <div className="two-column">
        <Panel eyebrow="STAGE 09" title="Qualification">
          {stage09.length ? stage09.slice(0, 8).map((result: any, index: number) => <div className="data-row" key={result.result_id || index}><div><div className="list-item-title">{result.result_id || 'Stage09 result'}</div><div className="list-item-meta">trades {fmt(result.effective_trades, 0)} · validation blocks {fmt(result.validation_blocks, 0)}</div></div><StatusChip value={result.rating || result.status || '—'} raw /></div>) : <EmptyState icon="shield" title="暂无 Stage09 结果" />}
        </Panel>
        <Panel eyebrow="STAGE 10" title="Formal vs Reference">
          {stage10.length ? stage10.slice(0, 8).map((result: any, index: number) => <div className="data-row" key={result.result_id || index}><div><div className="list-item-title">{result.research_run_id || result.result_id || 'Stage10 result'}</div><div className="list-item-meta">differences {fmt(result.differences, 0)} · exact match {result.exact_match ? 'TRUE' : 'FALSE'}</div></div><StatusChip value={result.status || '—'} raw /></div>) : <EmptyState icon="shield" title="暂无 Stage10 结果" />}
        </Panel>
      </div>

      <TechnicalDetails title="Research technical evidence">
        <div className="limitations">{(view?.limitations || []).map((item: string, index: number) => <div key={index}>• {item}</div>)}</div>
      </TechnicalDetails>
    </div>
  )
}

export default function ResearchLab({ forcedTab }: { forcedTab?: string }) {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [view, setView] = useState<any>(null)
  const [error, setError] = useState('')
  const tab = normalizeTab(forcedTab || params.get('tab'))
  const tabs = useMemo(() => TABS.map(item => ({ ...item, count: item.value === 'runs' ? view?.runs?.length || undefined : item.value === 'queue' ? view?.queue?.length || undefined : undefined })), [view])

  useEffect(() => {
    fetch('/api/v2/research/overview')
      .then(response => response.json())
      .then(setView)
      .catch((reason: Error) => setError(reason.message))
  }, [])

  const setTab = (value: string) => navigate('/research?tab=' + value)
  if (error) return <Notice tone="danger" title="Research 状态加载失败">{error}</Notice>
  if (!view) return <div className="page-loading">正在读取研究目录和运行状态…</div>

  return (
    <div>
      <PageHeader eyebrow="RESEARCH WORKSPACE" title="研究工作台" description="策略、因子、运行与队列共享同一入口；资格、PIT、OOS 和 Legacy 只读边界不因视觉重排而改变。" meta={<div>{view.as_of || view.timestamp || '—'}</div>} />
      <MetricStrip items={[
        { label: 'Strategy runs', value: fmt(view.run_summary?.strategy_runs, 0), hint: '独立策略运行' },
        { label: 'Research runs', value: fmt(view.run_summary?.research_runs ?? view.runs?.length, 0), hint: '包含 CONTROL/策略' },
        { label: 'Stage09', value: fmt(view.stage09?.length, 0), hint: '真实结果' },
        { label: 'Stage10', value: fmt(view.stage10?.length, 0), hint: '正式对照' },
      ]} />
      <div className="research-tabs"><Tabs items={tabs} value={tab} onChange={setTab} /></div>
      <div className="tab-content">
        {tab === 'strategies' && <Strategies embedded />}
        {tab === 'factors' && <Factors embedded />}
        {tab === 'runs' && <RunView view={view} />}
        {tab === 'queue' && <ResearchQueue embedded />}
      </div>
    </div>
  )
}
