import { useState, useRef, useEffect } from 'react'
import { Icon } from '../ui'

interface Message {
  role: 'user' | 'assistant'
  content: string
  timestamp: string
  privacyNotice?: string
}

interface AIAssistantProps {
  isOpen: boolean
  onClose: () => void
  initialTsCode?: string
}

export default function AIAssistant({ isOpen, onClose, initialTsCode }: AIAssistantProps) {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: 'assistant',
      content: '你好！我是量化分析助手。我可以帮你了解市场行情、扫描候选、策略信号等信息。请问有什么可以帮你的？',
      timestamp: new Date().toISOString(),
    },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [aiConfigured, setAiConfigured] = useState(true)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    if (isOpen && inputRef.current) {
      inputRef.current.focus()
    }
  }, [isOpen])

  const suggestedQuestions = [
    '当前数据状态如何？',
    '有哪些候选股票？',
    '系统有哪些功能限制？',
    initialTsCode ? `分析一下 ${initialTsCode}` : '',
  ].filter(Boolean)

  const sendMessage = async (text?: string) => {
    const messageText = text || input.trim()
    if (!messageText || loading) return

    const userMessage: Message = {
      role: 'user',
      content: messageText,
      timestamp: new Date().toISOString(),
    }
    setMessages(prev => [...prev, userMessage])
    setInput('')
    setLoading(true)
    setError('')

    try {
      const response = await fetch('/api/v2/ai/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: messageText,
          ts_code: initialTsCode,
        }),
      })

      const data = await response.json()

      if (data.status === 'NOT_CONFIGURED') {
        setAiConfigured(false)
        setError(data.message)
      } else if (data.status === 'OK') {
        const assistantMessage: Message = {
          role: 'assistant',
          content: data.response,
          timestamp: new Date().toISOString(),
          privacyNotice: data.privacy_notice,
        }
        setMessages(prev => [...prev, assistantMessage])
      } else {
        setError(data.message || 'AI 调用失败')
      }
    } catch (err) {
      setError('网络错误，请检查服务是否启动')
    } finally {
      setLoading(false)
    }
  }

  if (!isOpen) return null

  return (
    <div className="ai-assistant-overlay" onClick={onClose}>
      <div className="ai-assistant-panel" onClick={e => e.stopPropagation()}>
        <div className="ai-assistant-header">
          <div className="ai-assistant-title">
            <Icon name="spark" size={20} />
            <span>AI 量化助手</span>
            <span className="ai-privacy-badge" title="仅发送公开数据，不含私人信息"><Icon name="shield" size={12} /></span>
          </div>
          <button className="ai-assistant-close" onClick={onClose} aria-label="关闭">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="ai-assistant-messages">
          {messages.map((msg, idx) => (
            <div key={idx} className={`ai-message ai-message-${msg.role}`}>
              <div className="ai-message-avatar">
                {msg.role === 'assistant' ? <Icon name="spark" size={14} /> : <Icon name="briefcase" size={14} />}
              </div>
              <div className="ai-message-content">
                <div className="ai-message-text">{msg.content}</div>
                {msg.role === 'assistant' && idx === messages.length - 1 && (
                  <div className="ai-message-footer">
                    {msg.privacyNotice && <span className="ai-message-privacy"><Icon name="shield" size={10} /> {msg.privacyNotice}</span>}
                    <span className="ai-message-disclaimer">仅供参考，不构成投资建议</span>
                  </div>
                )}
              </div>
            </div>
          ))}
          {loading && (
            <div className="ai-message ai-message-assistant">
              <div className="ai-message-avatar">
                <Icon name="spark" size={14} />
              </div>
              <div className="ai-message-content">
                <div className="ai-message-typing">
                  <span></span><span></span><span></span>
                </div>
              </div>
            </div>
          )}
          {error && (
            <div className="ai-message-error">
              <Icon name="shield" size={14} />
              <span>{error}</span>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {messages.length <= 1 && (
          <div className="ai-assistant-suggestions">
            {suggestedQuestions.map((q, idx) => (
              <button
                key={idx}
                className="ai-suggestion-chip"
                onClick={() => sendMessage(q)}
                disabled={loading}
              >
                {q}
              </button>
            ))}
          </div>
        )}

        <div className="ai-assistant-input-area">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && !e.shiftKey && sendMessage()}
            placeholder={aiConfigured ? '输入你的问题...' : 'AI 未配置'}
            disabled={loading || !aiConfigured}
            className="ai-assistant-input"
          />
          <button
            className="ai-assistant-send"
            onClick={() => sendMessage()}
            disabled={loading || !input.trim() || !aiConfigured}
          >
            {loading ? (
              <Icon name="refresh" size={16} />
            ) : (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" />
              </svg>
            )}
          </button>
        </div>
      </div>
    </div>
  )
}
