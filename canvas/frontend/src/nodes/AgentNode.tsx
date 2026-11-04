// canvas/frontend/src/nodes/AgentNode.tsx
import { useState, useEffect, useRef } from 'react'
import { Handle, Position } from '@xyflow/react'
import { useCanvas } from '../context/CanvasContext'
import type { AgentNodeData, AgentFloorState } from '../types'

// Inject keyframe animation once
const ANIM_ID = 'ofp-agent-pulse'
if (typeof document !== 'undefined' && !document.getElementById(ANIM_ID)) {
  const style = document.createElement('style')
  style.id = ANIM_ID
  style.textContent = `
    @keyframes ofp-pulse {
      0%, 100% { box-shadow: 0 0 10px var(--pulse-color, #38a16955); }
      50%       { box-shadow: 0 0 28px var(--pulse-color, #38a169bb); }
    }
    @keyframes ofp-pulse-amber {
      0%, 100% { box-shadow: 0 0 8px #d69e2e55; border-color: #d4a843; }
      50%       { box-shadow: 0 0 22px #d69e2ebb; border-color: #f6ad55; }
    }
    @keyframes ofp-flash-teal {
      from { box-shadow: 0 0 18px #00bcd4; border-color: #00bcd4; }
      to   { box-shadow: 0 0 0px transparent; border-color: #2a3a4c; }
    }
    @keyframes ofp-flash-purple {
      from { box-shadow: 0 0 18px #8b5cf6; border-color: #8b5cf6; }
      to   { box-shadow: 0 0 0px transparent; border-color: #2a3a4c; }
    }
    @keyframes ofp-dot {
      0%, 100% { opacity: 1; } 50% { opacity: 0.2; }
    }
  `
  document.head.appendChild(style)
}

const PROVIDER_COLOR: Record<string, string> = {
  anthropic: '#d4a843',
  openai: '#10a37f',
  google: '#4285f4',
  huggingface: '#ff7b00',
}

const STATE_COLOR: Record<AgentFloorState, string> = {
  speaking: '#38a169',
  waiting: '#4a5568',
  requesting: '#d69e2e',
  error: '#e53e3e',
  spawning: '#805ad5',
  yielding: '#00bcd4',
  manifest: '#8b5cf6',
}

const STATE_LABEL: Record<AgentFloorState, string> = {
  speaking: 'has floor',
  waiting: '⏳ waiting',
  requesting: '✋ requesting',
  error: '❌ error',
  spawning: '⚙ spawning',
  yielding: '↩ yielding',
  manifest: '📋 manifest',
}

const TYPE_COLOR: Record<string, string> = {
  showrunner: '#d4a843',
  orchestrator: '#d4a843',
  'code-generation': '#00bcd4',
  'text-to-image': '#8b5cf6',
  'text-to-music': '#f97316',
  'text-to-video': '#e53e3e',
}

export function AgentNode({ data }: { data: AgentNodeData }) {
  const { onKick, sessionRunning } = useCanvas()
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState('')
  const [hovered, setHovered] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const prevFloorState = useRef<AgentFloorState>(data.floorState)
  const [lastMsgPreview, setLastMsgPreview] = useState<string | null>(null)

  // Keep last message preview updated
  useEffect(() => {
    const last = data.messages[data.messages.length - 1]
    if (last?.text) {
      setLastMsgPreview(last.text.slice(0, 90) + (last.text.length > 90 ? '…' : ''))
    }
  }, [data.messages])

  // Reset preview on new speaking turn
  useEffect(() => {
    if (data.floorState === 'speaking' && prevFloorState.current !== 'speaking') {
      setLastMsgPreview(null)
    }
    prevFloorState.current = data.floorState
  }, [data.floorState])
  const providerColor = PROVIDER_COLOR[data.provider] ?? '#58a6ff'
  const stateColor = STATE_COLOR[data.floorState] ?? '#4a5568'
  const isSpeaking = data.floorState === 'speaking'
  const isSpawning = data.floorState === 'spawning'
  const isRequesting = data.floorState === 'requesting'
  const isYielding = data.floorState === 'yielding'
  const isManifest = data.floorState === 'manifest'
  const typeColor = data.agentType ? (TYPE_COLOR[data.agentType] ?? '#58a6ff') : null

  const stateAnimStyle: React.CSSProperties = {}

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        background: 'linear-gradient(135deg, #161f2e 0%, #0d1520 100%)',
        border: `2px solid ${isSpeaking ? stateColor : '#2a3a4c'}`,
        borderRadius: 10, padding: '12px 14px',
        minWidth: 220, maxWidth: 300,
        color: '#e6edf5', fontFamily: 'ui-sans-serif, system-ui, sans-serif', fontSize: 12,
        position: 'relative', transition: 'border-color 0.2s',
        opacity: isSpawning ? 0.6 : 1,
        ...stateAnimStyle,
      }}
    >
      <Handle type="target" position={Position.Left} style={{ background: providerColor }} />

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <span style={{
          background: `${providerColor}22`, color: providerColor,
          borderRadius: 4, padding: '1px 6px', fontSize: 10, fontWeight: 700,
          textTransform: 'uppercase',
        }}>
          {data.provider}
        </span>
        {typeColor && data.agentType && (
          <span style={{
            background: `${typeColor}22`, color: typeColor,
            borderRadius: 4, padding: '1px 6px', fontSize: 9, fontWeight: 700,
            textTransform: 'uppercase',
          }}>
            {data.agentType.replace('-', ' ')}
          </span>
        )}
        <span style={{ fontWeight: 700, fontSize: 13, flex: 1 }}>{data.name}</span>
        <span style={{
          fontSize: 10, background: `${stateColor}33`, color: stateColor,
          borderRadius: 4, padding: '2px 7px', fontWeight: 600,
          border: `1px solid ${stateColor}55`,
          display: 'flex', alignItems: 'center', gap: 3,
        }}>
          {isSpeaking && (
            <span style={{ display: 'inline-flex', gap: 2 }}>
              {[0, 0.3, 0.6].map((delay) => (
                <span key={delay} style={{
                  width: 4, height: 4, borderRadius: '50%', background: stateColor,
                  display: 'inline-block',
                  opacity: 1,
                }} />
              ))}
            </span>
          )}
          {STATE_LABEL[data.floorState]}
        </span>
      </div>

      {data.model && (
        <div style={{ color: '#93a4b8', fontSize: 10, marginBottom: 6 }}>{data.model}</div>
      )}
      <div style={{ fontSize: 11, color: '#bed1e8', margin: '8px 0', borderTop: '1px solid #303d50', paddingTop: 6 }}>
        {data.asp ? <>
          <div>Lifecycle: {data.asp.lifecycle} · Health: {data.asp.health}</div>
          <div>Governance: {data.asp.governance}{data.asp.stale ? ' · STALE' : ''}</div>
          <small>Health source: {data.asp.provenance}</small>
        </> : 'ASP: no trusted status observation yet'}
      </div>

      {(data.pinned || data.replaying) && <div style={{ color: '#a9cce8', fontSize: 10, marginBottom: 6 }}>{data.pinned ? 'Pinned position' : ''}{data.pinned && data.replaying ? ' · ' : ''}{data.replaying ? 'Communication replay · ASP above is live' : ''}</div>}

      {/* Last message preview — always visible, cleared at start of new turn */}
      {lastMsgPreview && (
        <div style={{
          background: '#0d1520', border: '1px solid #2a3a4c',
          borderRadius: 5, padding: '5px 8px', fontSize: 11,
          color: '#93a4b8', lineHeight: 1.4, marginBottom: 6,
          wordBreak: 'break-word',
        }}>
          {lastMsgPreview}
        </div>
      )}

      {data.floorState === 'error' && data.errorMessage && (
        <div style={{
          background: '#2d1515', border: '1px solid #e53e3e44',
          borderRadius: 5, padding: '4px 7px', fontSize: 11,
          color: '#fc8181', marginBottom: 6,
        }}>
          {data.errorMessage}
        </div>
      )}

      {data.messages.length > 0 && (
        <div style={{ marginTop: 4 }}>
          <div
            onClick={() => setExpanded(!expanded)}
            style={{ cursor: 'pointer', fontSize: 10, color: '#93a4b8', marginBottom: 4, userSelect: 'none' }}
          >
            {expanded ? '▾' : '▸'} {data.messages.length} message{data.messages.length !== 1 ? 's' : ''}
          </div>
          {expanded && (
            <div style={{ maxHeight: 180, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 4 }}>
              {data.messages.slice(-8).map((msg) => (
                <div key={msg.id} style={{
                  background: '#0d1520', borderRadius: 6, padding: '5px 8px',
                  fontSize: 11, lineHeight: 1.4, color: '#c9d1d9',
                  wordBreak: 'break-word', border: '1px solid #2a3a4c',
                }}>
                  {msg.media && (
                    <div style={{ marginBottom: 4 }}>
                      {msg.media.type === 'image' ? (
                        <img src={msg.media.url} alt="output" style={{ maxWidth: '100%', borderRadius: 4 }} />
                      ) : (
                        <a href={msg.media.url} target="_blank" rel="noreferrer" style={{ color: '#58a6ff' }}>
                          [{msg.media.type}]
                        </a>
                      )}
                    </div>
                  )}
                  {msg.text}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {error && <div role="alert" style={{ color: '#fc8181' }}>{error}</div>}
      <div style={{ opacity: hovered ? 1 : 0.85 }}>
        <button className="nodrag nopan"
          disabled={removing}
          aria-label={`Remove ${data.name}${sessionRunning ? ' from session' : ' from configuration'}`}
          onClick={async () => {
            setRemoving(true)
            setError('')
            try { await onKick(data.name) }
            catch (err) { setError(String(err)) }
            finally { setRemoving(false) }
          }}
          style={{
            position: 'absolute', top: 8, right: 8,
            background: '#6b2121', color: '#fc8181',
            border: '1px solid #fc818166',
            borderRadius: 4, fontSize: 10, padding: '2px 6px', cursor: 'pointer',
          }}
        >
          {removing ? 'Removing…' : 'Remove'}
        </button>
      </div>
    </div>
  )
}
