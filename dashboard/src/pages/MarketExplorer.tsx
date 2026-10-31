import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ChartFrame, EmptyState, Icon, Notice, PageHeader, Panel, StatusChip, Tabs, Table, fmt, pct, secondaryButton } from '../ui'

const QUICK_CODES = [
  { code: '510300.SH', label: '沪深300 ETF' },
  { code: '510500.SH', label: '中证500 ETF' },
  { code: '159915.SZ', label: '创业板 ETF' },
  { code: '600519.SH', label: '贵州茅台' },
]

const SOURCE_LABEL: Record<string, string> = {
  REAL_READY_KNOWN_HISTORICAL: 'REAL Canonical ETF3 日线',
  etf_daily: 'ETF 日线',
  bars: '个股日线',
  bench_daily: '指数日线',
  LEGACY_EVIDENCE: 'Legacy 只读证据',
  LEGACY_READONLY_HISTORICAL: 'Legacy 历史日线',
  REAL_READY_PRICE_ONLY: '公开源未复权 PRICE_ONLY',
  REAL_PRICE_ONLY_WITH_LEGACY_HISTORY_FALLBACK: '公开源 PRICE_ONLY + Legacy 历史',
  MIXED: 'V2 PRICE_ONLY + Legacy 历史',
  REAL: 'V2 REAL Canonical',
}

function asNumber(value: unknown): number | null {
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function computeSMA(closes: number[], period: number): (number | null)[] {
  return closes.map((_, i) => {
    if (i < period - 1) return null
    let sum = 0
    for (let j = i - period + 1; j <= i; j++) sum += closes[j]
    return sum / period
  })
}

function computeATR(rows: any[], period: number = 14): (number | null)[] {
  const trs: number[] = []
  for (let i = 0; i < rows.length; i++) {
    if (i === 0) {
      trs.push(Number(rows[i].high) - Number(rows[i].low))
    } else {
      const prevClose = Number(rows[i - 1].close)
      const h = Number(rows[i].high)
      const l = Number(rows[i].low)
      trs.push(Math.max(h - l, Math.abs(h - prevClose), Math.abs(l - prevClose)))
    }
  }
  return trs.map((_, i) => {
    if (i < period - 1) return null
    let sum = 0
    for (let j = i - period + 1; j <= i; j++) sum += trs[j]
    return sum / period
  })
}

function computeStrategySignals(rows: any[]) {
  if (rows.length < 61) return null
  const closes = rows.map((r: any) => Number(r.close))
  const opens = rows.map((r: any) => Number(r.open || r.close))
  const highs = rows.map((r: any) => Number(r.high))
  const lows = rows.map((r: any) => Number(r.low))
  const volumes = rows.map((r: any) => Number(r.vol || r.volume || r.amount || 0))
  const sma20 = computeSMA(closes, 20)
  const sma60 = computeSMA(closes, 60)
  const atr14 = computeATR(rows, 14)
  const last = rows.length - 1
  const c = closes[last]
  const o = opens[last]
  const prevClose = closes[last - 1]
  const s20 = sma20[last]
  const s60 = sma60[last]
  const a = Math.max(atr14[last] || 0.01, 0.01)
  const ret3 = c / closes[last - 3] - 1
  const ret5 = c / closes[last - 5] - 1
  const priorHigh20 = Math.max(...highs.slice(last - 20, last))
  const vol = volumes[last]
  const medianVol20 = [...volumes.slice(last - 19)].sort((x, y) => x - y)[10] || 1
  const signals: { family: string; label: string; version: string; triggered: boolean; score: number; zones: Record<string, number> }[] = []

  // ── V1 Strategies ─────────────────────────────────────────────
  // Trend pullback V1
  if (s20 != null && s60 != null && c > s20 && s20 > s60 && ret5 >= -0.03 && ret5 <= 0.03 && (c / s20 - 1) >= -0.01 && (c / s20 - 1) <= 0.02) {
    signals.push({
      family: 'SHORT_TREND_PULLBACK_V1', label: '趋势回踩', version: 'V1', triggered: true, score: c / s60 - 1,
      zones: { entry_low: Math.max(s20, c - 0.5 * a), entry_high: Math.min(c, s20 + 0.5 * a), target: c + 1.5 * a, stop: c - a, chase: Math.min(c, s20 + 0.5 * a) + 0.5 * a },
    })
  }
  // Breakout V1
  const breakout = priorHigh20 ? c / priorHigh20 - 1 : 99
  if (breakout > 0 && breakout <= 0.015 && vol >= 1.5 * medianVol20) {
    signals.push({
      family: 'SHORT_BREAKOUT_CONFIRM_V1', label: '突破确认', version: 'V1', triggered: true, score: breakout + vol / Math.max(medianVol20, 1),
      zones: { entry_low: priorHigh20, entry_high: priorHigh20 + 0.5 * a, target: c + 1.5 * a, stop: priorHigh20 - 0.75 * a, chase: priorHigh20 + a },
    })
  }
  // Reversal V1
  if (ret3 <= -0.05 && c >= 0.95 * (s60 || 0) && vol >= medianVol20) {
    signals.push({
      family: 'SHORT_REVERSAL_V1', label: '短期反转', version: 'V1', triggered: true, score: -ret3,
      zones: { entry_low: c - 0.5 * a, entry_high: c + 0.2 * a, target: c + a, stop: c - a, chase: c + 0.7 * a },
    })
  }

  // ── V2 Strategies ─────────────────────────────────────────────
  // Volume features for V2
  const avgVolRecent = volumes.slice(last - 4, last).reduce((s, v) => s + v, 0) / 4
  const pullbackVolRatio = avgVolRecent > 0 ? vol / avgVolRecent : 1
  const consolidationRange = Math.max(...highs.slice(last - 10)) - Math.min(...lows.slice(last - 10))
  const consolidationRatio = consolidationRange / (a * 10 || 1)

  // Trend pullback V2 (requires pullback volume decline)
  if (s20 != null && s60 != null && c > s20 && s20 > s60 && ret5 >= -0.03 && ret5 <= 0.03 && (c / s20 - 1) >= -0.01 && (c / s20 - 1) <= 0.02 && pullbackVolRatio < 1.0) {
    signals.push({
      family: 'SHORT_TREND_PULLBACK_V2', label: '趋势回踩 V2', version: 'V2', triggered: true, score: (c / s60 - 1) * 1.1,
      zones: { entry_low: Math.max(s20, c - 0.5 * a), entry_high: Math.min(c, s20 + 0.5 * a), target: c + 1.5 * a, stop: c - a, chase: Math.min(c, s20 + 0.5 * a) + 0.5 * a },
    })
  }
  // Breakout V2 (requires consolidation)
  if (breakout > 0 && breakout <= 0.015 && vol >= 1.5 * medianVol20 && consolidationRatio < 3.0) {
    signals.push({
      family: 'SHORT_BREAKOUT_CONFIRM_V2', label: '突破确认 V2', version: 'V2', triggered: true, score: (breakout + vol / Math.max(medianVol20, 1)) * 1.1,
      zones: { entry_low: priorHigh20, entry_high: priorHigh20 + 0.5 * a, target: c + 1.5 * a, stop: priorHigh20 - 0.75 * a, chase: priorHigh20 + a },
    })
  }
  // Reversal V2 (requires volume climax)
  const volRatio = vol / Math.max(medianVol20, 1)
  if (ret3 <= -0.05 && c >= 0.95 * (s60 || 0) && vol >= medianVol20 && volRatio >= 1.5) {
    signals.push({
      family: 'SHORT_REVERSAL_V2', label: '短期反转 V2', version: 'V2', triggered: true, score: -ret3 * 1.1,
      zones: { entry_low: c - 0.5 * a, entry_high: c + 0.2 * a, target: c + a, stop: c - a, chase: c + 0.7 * a },
    })
  }

  // ── New Strategies ────────────────────────────────────────────
  // Gap momentum
  const gapPct = prevClose > 0 ? o / prevClose - 1 : 0
  if (gapPct > 0.01 && volRatio >= 2.0 && c > o && c > (s20 || 0)) {
    signals.push({
      family: 'SHORT_GAP_MOMENTUM_V1', label: '跳空动量', version: 'NEW', triggered: true, score: gapPct * volRatio,
      zones: { entry_low: prevClose, entry_high: o, target: c + a, stop: prevClose - 0.5 * a, chase: c + 0.5 * a },
    })
  }
  // Relative strength - simplified without market data (assumes market weak if stock is strong)
  if (ret5 > 0.02 && c > (s20 || 0) && volRatio < 3.0) {
    signals.push({
      family: 'SHORT_RELATIVE_STRENGTH_V1', label: '相对强度', version: 'NEW', triggered: true, score: ret5,
      zones: { entry_low: c - 0.3 * a, entry_high: c + 0.2 * a, target: c + 1.2 * a, stop: c - a, chase: c + 0.8 * a },
    })
  }

  return { signals, sma20, sma60, atr14, ret3, ret5, priorHigh20, medianVol20, vol, close: c }
}

function MarketChart({ rows, showMA = false }: { rows: any[]; showMA?: boolean }) {
  const chartRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let chart: any
    let cancelled = false
    const validRows = rows.filter(row => row.trade_date && ['open', 'high', 'low', 'close'].every(key => asNumber(row[key]) != null))
    if (!chartRef.current || !validRows.length) return
    import('lightweight-charts').then(({ createChart }) => {
      if (cancelled || !chartRef.current) return
      chartRef.current.innerHTML = ''
      chart = createChart(chartRef.current, {
        width: chartRef.current.clientWidth,
        height: 430,
        layout: { background: { color: '#121821' }, textColor: '#d5dbe3' },
        grid: { vertLines: { color: '#27313d' }, horzLines: { color: '#27313d' } },
        timeScale: { borderColor: '#3a4654', timeVisible: false },
        rightPriceScale: { borderColor: '#3a4654' },
      })
      const candleSeries = chart.addCandlestickSeries({
        upColor: '#f05a67', downColor: '#35c98b',
        borderUpColor: '#f05a67', borderDownColor: '#35c98b',
        wickUpColor: '#f05a67', wickDownColor: '#35c98b',
      })
      const toTime = (d: string) => d.length === 8 ? d.slice(0, 4) + '-' + d.slice(4, 6) + '-' + d.slice(6, 8) : d
      const candleData = validRows.map(row => ({
        time: toTime(row.trade_date),
        open: Number(row.open), high: Number(row.high),
        low: Number(row.low), close: Number(row.close),
      }))
      candleSeries.setData(candleData)
      // Volume
      const volumeSeries = chart.addHistogramSeries({
        priceFormat: { type: 'volume' }, priceScaleId: '', color: '#4b5968',
      })
      volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } })
      volumeSeries.setData(validRows.map(row => ({
        time: toTime(row.trade_date),
        value: Number(row.vol || row.volume || 0),
        color: Number(row.close) >= Number(row.open) ? 'rgba(240,90,103,.48)' : 'rgba(53,201,139,.48)',
      })))
      // MA overlays
      if (showMA && validRows.length >= 20) {
        const closes = validRows.map(r => Number(r.close))
        const sma20 = computeSMA(closes, 20)
        const sma60 = computeSMA(closes, 60)
        const ma20Series = chart.addLineSeries({ color: '#f2c94c', lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
        ma20Series.setData(validRows.map((r, i) => sma20[i] != null ? ({ time: toTime(r.trade_date), value: sma20[i]! }) : null).filter(Boolean))
        if (validRows.length >= 60) {
          const ma60Series = chart.addLineSeries({ color: '#7ea9ff', lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
          ma60Series.setData(validRows.map((r, i) => sma60[i] != null ? ({ time: toTime(r.trade_date), value: sma60[i]! }) : null).filter(Boolean))
        }
      }
      chart.timeScale().fitContent()
      const onResize = () => { if (chartRef.current && chart) chart.applyOptions({ width: chartRef.current.clientWidth }) }
      window.addEventListener('resize', onResize)
      ;(chart as any).__cleanup = () => window.removeEventListener('resize', onResize)
    }).catch(() => {})
    return () => {
      cancelled = true
      if (chart?.__cleanup) chart.__cleanup()
      if (chart) chart.remove()
    }
  }, [rows, showMA])

  if (!rows.length) return <EmptyState icon="chart" title="暂无可绘制的日线" detail="当前 API 没有返回完整 OHLC 行，不使用占位线或其他日期数据替代。" />
  return <div ref={chartRef} className="market-chart" aria-label="日线 K 线图" />
}

function StrategyAnalysisPanel({ data, code }: { data: any[]; code: string }) {
  const analysis = useMemo(() => computeStrategySignals(data), [data])
  if (!analysis) return <EmptyState icon="research" title="数据不足" detail="需要至少 61 个交易日数据才能计算策略信号。" />
  const { signals, sma20, sma60, atr14, ret3, ret5, priorHigh20, close } = analysis
  const last = data.length - 1
  const atr = atr14[last]
  const s20 = sma20[last]
  const s60 = sma60[last]

  return <div>
    <div className="panel-lede">
      以下信号基于日线数据计算，使用冻结策略配置。不产生交易信号，不连接 PAPER 或券商。排名不是上涨概率。
    </div>

    <div className="detail-grid" style={{ marginBottom: '16px' }}>
      <div>当前价<strong>¥{close?.toFixed(2) || '—'}</strong></div>
      <div>SMA20<strong>{s20?.toFixed(2) || '—'}</strong></div>
      <div>SMA60<strong>{s60?.toFixed(2) || '—'}</strong></div>
      <div>ATR14<strong>{atr?.toFixed(2) || '—'}</strong></div>
      <div>3日收益<strong style={{ color: ret3 != null && ret3 >= 0 ? 'var(--q-market-up)' : 'var(--q-market-down)' }}>{ret3 != null ? pct(ret3) : '—'}</strong></div>
      <div>5日收益<strong style={{ color: ret5 != null && ret5 >= 0 ? 'var(--q-market-up)' : 'var(--q-market-down)' }}>{ret5 != null ? pct(ret5) : '—'}</strong></div>
      <div>前20日最高<strong>{priorHigh20?.toFixed(2) || '—'}</strong></div>
      <div>均线位置<strong>{s20 != null && s60 != null ? (close > s20 && s20 > s60 ? '多头排列' : close < s20 && s20 < s60 ? '空头排列' : '交叉整理') : '—'}</strong></div>
    </div>

    <div className="subsection-title">策略信号</div>
    {signals.length === 0 ? (
      <div style={{ color: 'var(--q-text-muted)', fontSize: '12px', padding: '12px 0' }}>
        当前不满足任何策略族的入选条件。这不一定意味着看空，只是没有符合冻结规则的观察信号。
      </div>
    ) : signals.map(signal => (
      <div key={signal.family} className="strategy-signal-card">
        <div className="strategy-signal-head">
          <div>
            <div className="list-item-title">{signal.label}</div>
            <div className="list-item-meta">
              {signal.family} · <span style={{ color: signal.version === 'V2' ? 'var(--q-info)' : signal.version === 'NEW' ? 'var(--q-gold)' : 'var(--q-text-muted)' }}>{signal.version}</span>
            </div>
          </div>
          <StatusChip value="TRIGGERED" label="满足条件" />
        </div>
        <div className="strategy-signal-zones">
          <div className="signal-zone">
            <span className="signal-zone-label">观察买入区间</span>
            <span className="signal-zone-value">¥{signal.zones.entry_low?.toFixed(2)} – ¥{signal.zones.entry_high?.toFixed(2)}</span>
          </div>
          <div className="signal-zone">
            <span className="signal-zone-label">目标/减仓</span>
            <span className="signal-zone-value" style={{ color: 'var(--q-market-up)' }}>¥{signal.zones.target?.toFixed(2)}</span>
          </div>
          <div className="signal-zone">
            <span className="signal-zone-label">风险线</span>
            <span className="signal-zone-value" style={{ color: 'var(--q-danger)' }}>¥{signal.zones.stop?.toFixed(2)}</span>
          </div>
          <div className="signal-zone">
            <span className="signal-zone-label">追高放弃</span>
            <span className="signal-zone-value">¥{signal.zones.chase?.toFixed(2)}</span>
          </div>
        </div>
        <div style={{ color: 'var(--q-text-faint)', fontSize: '10px', marginTop: '6px' }}>
          区间来自日线波动指标，是观察计划，不是保证成交或预知高低点。
        </div>
      </div>
    ))}

    <div style={{ marginTop: '16px', padding: '10px 12px', background: 'rgba(228,185,79,0.04)', border: '1px solid rgba(228,185,79,0.2)', borderRadius: '6px', color: 'var(--q-text-muted)', fontSize: '11px' }}>
      以上信号仅供研究参考，不构成买入建议。实际交易须考虑停牌、涨跌停、流动性、T+1 等约束。
    </div>
  </div>
}

function IntradayPanel({ code }: { code: string }) {
  const [tickData, setTickData] = useState<any>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    fetch('/api/v2/watch')
      .then(r => r.json())
      .then(data => setTickData(data))
      .catch(() => setTickData({ state: 'ERROR' }))
      .finally(() => setLoading(false))
  }, [code])

  const watchStatus = tickData?.status || tickData?.state || 'UNKNOWN'

  return <div>
    <div className="panel-lede">
      盘中分时数据需要实时行情供应商通过验收。当前日线数据不会冒充 tick 或分钟 bar。
    </div>
    <div className="detail-grid">
      <div>实时行情<strong><StatusChip value={watchStatus} raw /></strong></div>
      <div>分钟数据<strong>{watchStatus === 'WATCH_FULLY_VERIFIED' ? '可用' : '未验收'}</strong></div>
      <div>触发状态<strong>{watchStatus === 'WATCH_FULLY_VERIFIED' ? '已启用' : '已关闭'}</strong></div>
      <div>验证样本<strong>{String(tickData?.validated_sample_count || 0)}</strong></div>
    </div>
    {watchStatus !== 'WATCH_FULLY_VERIFIED' && (
      <Notice tone="warn" title="盘中数据暂不可用">
        实时行情验收未通过。日线数据不会冒充 tick。请前往「盘中观察」页面查看验收进度和本地验收步骤。
        <Link className="inline-link" to="/intraday" style={{ marginLeft: '4px' }}>打开盘中观察 →</Link>
      </Notice>
    )}
  </div>
}

export default function MarketExplorer() {
  const [searchParams, setSearchParams] = useSearchParams()
  const initialCode = searchParams.get('code') || '510300.SH'
  const [input, setInput] = useState(initialCode)
  const [code, setCode] = useState(initialCode)
  const [codeName, setCodeName] = useState('')
  const [data, setData] = useState<any[]>([])
  const [source, setSource] = useState('')
  const [sourceState, setSourceState] = useState('')
  const [sourceMessage, setSourceMessage] = useState('')
  const [availableDates, setAvailableDates] = useState<string[]>([])
  const [manifestHash, setManifestHash] = useState('')
  const [suggests, setSuggests] = useState<{ ts_code: string; name: string }[]>([])
  const [loading, setLoading] = useState(false)
  const [prediction, setPrediction] = useState<any>(null)
  const [activeTab, setActiveTab] = useState('basic')
  const [showMA, setShowMA] = useState(true)

  // Sync URL params
  useEffect(() => {
    const urlCode = searchParams.get('code')
    if (urlCode && urlCode !== code) {
      setCode(urlCode)
      setInput(urlCode)
    }
  }, [searchParams])

  const loadDaily = (selectedCode: string) => {
    setLoading(true)
    setPrediction(null)
    fetch('/api/v2/market/daily?ts_code=' + encodeURIComponent(selectedCode) + '&start_date=20230101')
      .then(response => response.json())
      .then(result => {
        const rows = result.rows || result.data || []
        setData(rows)
        setSource(result.provenance || result.source_kind || result.state || result.source_table || '')
        setSourceState(result.state || '')
        setSourceMessage(result.message || '')
        setAvailableDates(result.available_dates || rows.map((row: any) => row.trade_date).filter(Boolean))
        setManifestHash(result.manifest_hash || '')
      })
      .catch(reason => {
        setData([])
        setSource('')
        setSourceState('ERROR')
        setSourceMessage(reason.message || '行情读取失败')
        setAvailableDates([])
        setManifestHash('')
      })
      .finally(() => setLoading(false))
  }

  const loadSecurity = (selectedCode: string) => {
    fetch('/api/v2/market/security?ts_code=' + encodeURIComponent(selectedCode))
      .then(r => r.json())
      .then(result => {
        const sec = result.security || {}
        setCodeName(sec.name || sec.symbol || '')
      })
      .catch(() => {})
  }

  const query = () => {
    const queryText = input.trim()
    if (!queryText) return
    if (queryText.includes('.')) {
      setCode(queryText)
      setCodeName('')
      setSearchParams({ code: queryText })
      loadSecurity(queryText)
      return
    }
    setLoading(true)
    fetch('/api/v2/market/securities?q=' + encodeURIComponent(queryText) + '&limit=10')
      .then(response => response.json())
      .then(result => {
        const first = (result.securities || result.data || [])[0]
        if (first) {
          setCode(first.ts_code)
          setCodeName(first.name || '')
          setInput(first.ts_code)
          setSearchParams({ code: first.ts_code })
        } else {
          setData([])
          setSource('')
          setCodeName('')
          setSourceMessage('没有找到匹配证券')
        }
      })
      .catch(reason => setSourceMessage(reason.message || '证券搜索失败'))
      .finally(() => setLoading(false))
  }

  useEffect(() => { loadDaily(code); loadSecurity(code) }, [code])

  useEffect(() => {
    const queryText = input.trim()
    if (!queryText || queryText.includes('.') || queryText.length < 2) {
      setSuggests([])
      return
    }
    const timer = window.setTimeout(() => {
      fetch('/api/v2/market/securities?q=' + encodeURIComponent(queryText) + '&limit=8')
        .then(response => response.json())
        .then(result => setSuggests((result.securities || result.data || []).map((item: any) => ({ ts_code: item.ts_code, name: item.name }))))
        .catch(() => {})
    }, 250)
    return () => window.clearTimeout(timer)
  }, [input])

  const latest = data[data.length - 1]
  const previous = data[data.length - 2]
  const latestClose = asNumber(latest?.close)
  const previousClose = asNumber(previous?.close)
  const change = latestClose != null && previousClose != null ? latestClose - previousClose : null
  const changePct = change != null && previousClose ? change / previousClose : null
  const directionClass = change == null ? '' : change >= 0 ? 'market-up' : 'market-down'

  const strategyAnalysis = useMemo(() => computeStrategySignals(data), [data])
  const signalCount = strategyAnalysis?.signals?.length || 0

  return (
    <div>
      <PageHeader
        eyebrow="行情分析"
        title={codeName ? `${codeName} · ${code}` : code}
        description="搜索任意证券，查看历史日线、技术指标和策略分析。图表支持均线叠加。策略分析使用冻结规则计算观察区间，不产生交易信号。"
        meta={<><div>{SOURCE_LABEL[source] || source || '未找到数据'}</div><div className="page-meta-inline">{availableDates.length ? availableDates[availableDates.length - 1] : '—'}</div></>}
        actions={<button type="button" style={secondaryButton} onClick={() => loadDaily(code)} disabled={loading}><Icon name="refresh" size={14} /> {loading ? '读取中…' : '刷新行情'}</button>}
      />

      <Panel className="market-command-panel">
        <div className="search-bar">
          <div className="search-control">
            <Icon name="search" size={15} />
            <input className="search-input" value={input} onChange={event => setInput(event.target.value)} onKeyDown={event => event.key === 'Enter' && query()} placeholder="输入代码或名称，例如 600519 / 茅台 / 000001.SZ" aria-label="搜索证券" />
            {suggests.length > 0 && input !== code && <div className="suggestion-list">{suggests.map(item => <button type="button" className="suggestion" key={item.ts_code} onClick={() => { setCode(item.ts_code); setInput(item.ts_code); setCodeName(item.name); setSuggests([]); setSearchParams({ code: item.ts_code }) }}><span className="suggestion-code">{item.ts_code}</span><span className="suggestion-name">{item.name}</span></button>)}</div>}
          </div>
          <button type="button" style={secondaryButton} onClick={query} disabled={loading}>{loading ? '查询中…' : '查询'}</button>
        </div>
        <div className="quick-symbols">{QUICK_CODES.map(item => <button type="button" key={item.code} className={'quick-symbol' + (code === item.code ? ' active' : '')} onClick={() => { setCode(item.code); setInput(item.code); setCodeName(item.label); setSearchParams({ code: item.code }) }}>{item.label} <span>{item.code}</span></button>)}</div>
      </Panel>

      <div className="quote-hero">
        <div className="quote-main"><div className="quote-code">{code}</div><div className="quote-name">{codeName || '证券行情'}</div><div className="quote-price">{latestClose == null ? '—' : fmt(latestClose, 3)}</div></div>
        <div className="quote-cell"><div className="quote-label">涨跌</div><div className={'quote-value ' + directionClass}>{change == null ? '—' : (change >= 0 ? '+' : '') + fmt(change, 3)}</div></div>
        <div className="quote-cell"><div className="quote-label">涨跌幅</div><div className={'quote-value ' + directionClass}>{changePct == null ? '—' : pct(changePct)}</div></div>
        <div className="quote-cell"><div className="quote-label">数据截止</div><div className="quote-value">{latest?.trade_date || '—'}</div><div className="metric-hint">{sourceState ? <StatusChip value={sourceState} raw /> : '—'}</div></div>
      </div>

      {sourceMessage && <Notice tone="warn" title="数据说明">{sourceMessage}</Notice>}

      {/* 策略信号快速提示 */}
      {signalCount > 0 && (
        <Notice tone="info" title={`${signalCount} 个策略条件满足`}>
          {strategyAnalysis?.signals.map(s => s.label).join('、')} — 查看下方「策略分析」了解观察区间和失效条件。
        </Notice>
      )}

      <ChartFrame eyebrow="K线走势" title="日K线与成交量" meta={<>
        <label style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '11px', color: 'var(--q-text-muted)', cursor: 'pointer' }}>
          <input type="checkbox" checked={showMA} onChange={e => setShowMA(e.target.checked)} />
          均线 (MA20/MA60)
        </label>
        <span className="chart-meta-spacer"><StatusChip value={source || '—'} raw /> {data.length} bars</span>
      </>}>
        <MarketChart rows={data} showMA={showMA} />
      </ChartFrame>
      {showMA && <div style={{ display: 'flex', gap: '16px', marginTop: '4px', fontSize: '11px', color: 'var(--q-text-muted)' }}>
        <span><span style={{ display: 'inline-block', width: '12px', height: '2px', background: '#f2c94c', marginRight: '4px', verticalAlign: 'middle' }} />MA20</span>
        <span><span style={{ display: 'inline-block', width: '12px', height: '2px', background: '#7ea9ff', marginRight: '4px', verticalAlign: 'middle' }} />MA60</span>
      </div>}

      <div className="market-detail-panel">
        <Tabs items={[
          { value: 'basic', label: '日线数据' },
          { value: 'strategy', label: '策略分析', count: signalCount || undefined },
          { value: 'intraday', label: '盘中数据' },
          { value: 'diagnostic', label: '研究诊断' },
        ]} value={activeTab} onChange={setActiveTab} />
        <div className="tab-panel">
          {activeTab === 'basic' && (
            <Table headers={['日期', '开盘', '最高', '最低', '收盘', '成交量']} minWidth={620}>
              {data.slice(-30).reverse().map((row, index) => <tr key={row.trade_date + '-' + index}>
                <td>{row.trade_date || '—'}</td>
                <td>{fmt(row.open, 3)}</td>
                <td>{fmt(row.high, 3)}</td>
                <td>{fmt(row.low, 3)}</td>
                <td className={asNumber(row.close) != null && asNumber(row.open) != null && Number(row.close) >= Number(row.open) ? 'market-up' : 'market-down'}>{fmt(row.close, 3)}</td>
                <td>{fmt(row.vol ?? row.volume, 0)}</td>
              </tr>)}
            </Table>
          )}
          {activeTab === 'strategy' && <StrategyAnalysisPanel data={data} code={code} />}
          {activeTab === 'intraday' && <IntradayPanel code={code} />}
          {activeTab === 'diagnostic' && (
            <div>
              <div className="diagnostic-row">
                <div>
                  <div className="list-item-title">基于历史日线的可解释性诊断</div>
                  <div className="list-item-meta">不产生交易信号，不连接 PAPER、券商或账户。</div>
                </div>
                <button type="button" style={secondaryButton} onClick={() => fetch('/api/v2/market/predict?ts_code=' + encodeURIComponent(code)).then(r => r.json()).then(setPrediction).catch(reason => setPrediction({ state: 'ERROR', message: reason.message }))}>运行诊断</button>
              </div>
              {prediction && <div className="diagnostic-result"><StatusChip value={prediction.state || '—'} raw /><span>{prediction.message || prediction.detail || '研究诊断已返回'}</span>{prediction.latest && <span className="metric-hint">latest {prediction.latest.trade_date || prediction.latest.ts_code}</span>}</div>}
              <div className="provenance-line">来源：{SOURCE_LABEL[source] || source || '—'} · 日期：{availableDates[0] || '—'} → {availableDates[availableDates.length - 1] || '—'}</div>
            </div>
          )}
        </div>
      </div>

      {/* 快捷操作 */}
      <Panel eyebrow="快捷操作" title="对这只股票还可以做什么">
        <div className="next-steps-grid">
          <Link className="next-step" to={`/holdings`}>
            <Icon name="briefcase" size={20} />
            <div><div className="next-step-title">添加到持仓</div><div className="next-step-detail">手工录入 {code} 的持仓信息</div></div>
          </Link>
          <Link className="next-step" to={`/opportunities`}>
            <Icon name="activity" size={20} />
            <div><div className="next-step-title">查看扫描候选</div><div className="next-step-detail">看这只股票是否出现在候选名单</div></div>
          </Link>
          <Link className="next-step" to={`/intraday`}>
            <Icon name="watch" size={20} />
            <div><div className="next-step-title">加入盘中观察</div><div className="next-step-detail">监控条件和触发</div></div>
          </Link>
        </div>
      </Panel>
    </div>
  )
}
