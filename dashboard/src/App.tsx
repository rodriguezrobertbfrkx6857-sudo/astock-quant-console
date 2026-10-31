import { useState } from 'react'
import { Link, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import Overview from './pages/Overview'
import Review from './pages/Review'
import ResearchLab from './pages/ResearchLab'
import BacktestCenter from './pages/BacktestCenter'
import MarketExplorer from './pages/MarketExplorer'
import PaperPortfolio from './pages/PaperPortfolio'
import EventIntelligence from './pages/EventIntelligence'
import DataHealth from './pages/DataHealth'
import DataExplorer from './pages/DataExplorer'
import Watch from './pages/Watch'
import SystemStatus from './pages/SystemStatus'
import ShortTermWorkbench from './pages/ShortTermWorkbench'
import Holdings from './pages/Holdings'
import Opportunities from './pages/Opportunities'
import IntradayWatch from './pages/IntradayWatch'
import AIAssistant from './components/AIAssistant'
import { Icon, type IconName } from './ui'

interface NavItem {
  path: string
  label: string
  eyebrow: string
  icon: IconName
  primary?: boolean
}

const PRIMARY_NAV_ITEMS: NavItem[] = [
  { path: '/', label: '今日工作台', eyebrow: '每日总览', icon: 'home', primary: true },
  { path: '/holdings', label: '我的持仓', eyebrow: '持仓与计划', icon: 'briefcase', primary: true },
  { path: '/opportunities', label: '明日机会', eyebrow: '候选与观察', icon: 'activity', primary: true },
  { path: '/intraday', label: '盘中观察', eyebrow: '条件监控', icon: 'watch', primary: true },
  { path: '/review', label: '复盘记录', eyebrow: '计划与实际', icon: 'calendar', primary: true },
]

const SECONDARY_NAV_ITEMS: NavItem[] = [
  { path: '/market', label: '行情', eyebrow: 'Quotes & Charts', icon: 'market' },
  { path: '/research', label: '策略研究', eyebrow: 'Strategies & Factors', icon: 'research' },
  { path: '/backtests', label: '回测', eyebrow: 'Historical Replay', icon: 'backtest' },
  { path: '/paper', label: 'PAPER', eyebrow: 'Simulated Portfolio', icon: 'paper' },
  { path: '/watch', label: 'Watch & AI', eyebrow: 'Signals & Alerts', icon: 'watch' },
  { path: '/system', label: '系统', eyebrow: 'Runtime & Evidence', icon: 'system' },
]

function routeIsActive(pathname: string, path: string): boolean {
  if (path === '/') return pathname === '/'
  if (path === '/research') return pathname === '/research' || ['/strategies', '/factors', '/research-queue'].includes(pathname)
  return pathname === path || pathname.startsWith(path + '/')
}

function NavLink({ item, collapsed }: { item: NavItem; collapsed: boolean }) {
  const location = useLocation()
  const active = routeIsActive(location.pathname, item.path)
  return (
    <Link className={'nav-item' + (active ? ' active' : '')} to={item.path} title={collapsed ? item.label : undefined} aria-current={active ? 'page' : undefined}>
      <span className="nav-icon"><Icon name={item.icon} size={19} /></span>
      <span className="nav-copy"><span className="nav-main">{item.label}</span><span className="nav-eyebrow">{item.eyebrow}</span></span>
    </Link>
  )
}

function TerminalShell() {
  const [collapsed, setCollapsed] = useState(() => typeof window !== 'undefined' && window.innerWidth <= 1060)
  const [aiOpen, setAiOpen] = useState(false)
  const location = useLocation()
  const allItems = [...PRIMARY_NAV_ITEMS, ...SECONDARY_NAV_ITEMS]
  const current = allItems.find(item => routeIsActive(location.pathname, item.path)) || PRIMARY_NAV_ITEMS[0]

  return (
    <div className="app-shell">
      <aside className={'sidebar' + (collapsed ? ' collapsed' : '')}>
        <div className="brand">
          <div className="brand-mark"><Icon name="spark" size={21} /></div>
          <div className="brand-copy"><div className="brand-title">Quant Lab</div><div className="brand-subtitle">A股量化研究终端</div></div>
          <button className="collapse-button" type="button" onClick={() => setCollapsed(value => !value)} aria-label={collapsed ? '展开侧栏' : '折叠侧栏'} title={collapsed ? '展开侧栏' : '折叠侧栏'}>
            <span className={'collapse-icon' + (collapsed ? ' is-collapsed' : '')}><Icon name="chevron" size={16} /></span>
          </button>
        </div>

        <div className="nav-section">
          <div className="nav-section-label">日常操作</div>
          {PRIMARY_NAV_ITEMS.map(item => <NavLink item={item} collapsed={collapsed} key={item.path} />)}
        </div>

        <div className="nav-section nav-section-system">
          <div className="nav-section-label">高级功能</div>
          {SECONDARY_NAV_ITEMS.map(item => <NavLink item={item} collapsed={collapsed} key={item.path} />)}
        </div>

        <div className="sidebar-footer">
          <div className="safety-lock"><Icon name="shield" size={15} /><span>本地运行 · 不连接券商</span></div>
        </div>
      </aside>

      <div className="main-shell">
        <header className="topbar">
          <div className="topbar-title">{current.label}</div>
          <div className="topbar-context">{current.eyebrow}</div>
          <div className="topbar-status">
            <span className="status-chip status-ok">本地运行</span>
            <span className="status-chip status-neutral">实盘关闭</span>
          </div>
        </header>
        <main className="content-shell">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/holdings" element={<Holdings />} />
            <Route path="/opportunities" element={<Opportunities />} />
            <Route path="/intraday" element={<IntradayWatch />} />
            <Route path="/review" element={<Review />} />
            <Route path="/short-term" element={<ShortTermWorkbench />} />
            <Route path="/research" element={<ResearchLab />} />
            <Route path="/strategies" element={<ResearchLab forcedTab="strategies" />} />
            <Route path="/factors" element={<ResearchLab forcedTab="factors" />} />
            <Route path="/research-queue" element={<ResearchLab forcedTab="queue" />} />
            <Route path="/backtests" element={<BacktestCenter />} />
            <Route path="/market" element={<MarketExplorer />} />
            <Route path="/paper" element={<PaperPortfolio />} />
            <Route path="/events" element={<EventIntelligence />} />
            <Route path="/data-health" element={<DataHealth />} />
            <Route path="/data" element={<DataExplorer />} />
            <Route path="/watch" element={<Watch />} />
            <Route path="/system" element={<SystemStatus />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>

      {/* AI Assistant Floating Button */}
      <button
        className="ai-assistant-fab"
        onClick={() => setAiOpen(true)}
        aria-label="打开 AI 助手"
        title="AI 量化助手"
      >
        <Icon name="spark" size={24} />
      </button>

      {/* AI Assistant Panel */}
      <AIAssistant isOpen={aiOpen} onClose={() => setAiOpen(false)} />
    </div>
  )
}

export default function App() {
  return <TerminalShell />
}
