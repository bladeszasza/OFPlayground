// canvas/frontend/src/nodes/HumanNode.tsx
import { useState } from 'react'
import { Handle, Position } from '@xyflow/react'
import { useCanvas } from '../context/CanvasContext'
import type { HumanNodeData } from '../types'

export function HumanNode({ data }: { data: HumanNodeData }) {
  const { onSend, sessionRunning } = useCanvas()
  const [input, setInput] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')

  const submit = async () => {
    if (!input.trim() || !sessionRunning || pending) return
    setPending(true)
    setError('')
    try {
      await onSend(input.trim())
      setInput('')
    } catch (err) {
      setError(String(err))
    } finally {
      setPending(false)
    }
  }

  return (
    <div style={{
      background: 'linear-gradient(135deg, #1a2a1a 0%, #0d1520 100%)',
      border: '2px solid #38a169',
      borderRadius: 10, padding: '12px 14px',
      minWidth: 220, maxWidth: 300,
      color: '#e6edf5', fontFamily: 'ui-sans-serif, system-ui, sans-serif', fontSize: 12,
    }}>
      <Handle type="target" position={Position.Left} style={{ background: '#38a169' }} />

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <span style={{
          background: '#38a16922', color: '#68d391',
          borderRadius: 4, padding: '1px 6px', fontSize: 10, fontWeight: 700,
        }}>HUMAN</span>
        <span style={{ fontWeight: 700, fontSize: 13 }}>{data.humanName}</span>
      </div>

      {data.asp && <p style={{fontSize:10,color:'#b7c5d8'}}>ASP: {data.asp.lifecycle} · {data.asp.health} · {data.asp.governance}{data.asp.stale ? ' · STALE' : ''}</p>}

      {data.messages.length > 0 && (
        <div style={{
          maxHeight: 120, overflowY: 'auto',
          display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 8,
        }}>
          {data.messages.slice(-4).map((msg) => (
            <div key={msg.id} style={{
              background: '#0d1520', borderRadius: 6, padding: '4px 8px',
              fontSize: 11, color: '#c9d1d9', wordBreak: 'break-word',
              border: '1px solid #2a3a4c',
            }}>
              {msg.text}
            </div>
          ))}
        </div>
      )}

      {sessionRunning && (
        <div style={{ display: 'flex', gap: 6 }}>
          <input className="nodrag nopan" aria-label="Human message"
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
            placeholder="Type a message…"
            style={{
              flex: 1, background: '#0d1520', color: '#e6edf5',
              border: '1px solid #2a3a4c', borderRadius: 6, padding: '4px 8px',
              fontSize: 11, boxSizing: 'border-box',
            }}
          />
          <button className="nodrag nopan" onClick={submit} disabled={pending} style={{
            background: '#1a4731', color: '#68d391',
            border: '1px solid #68d39166',
            borderRadius: 6, padding: '4px 10px', cursor: 'pointer', fontSize: 11,
          }}>{pending ? 'Sending…' : 'Send'}</button>
        </div>
      )}
      {error && <div role="alert" style={{ color: '#fc8181', marginTop: 6 }}>{error}</div>}
    </div>
  )
}
