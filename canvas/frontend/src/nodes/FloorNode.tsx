// canvas/frontend/src/nodes/FloorNode.tsx
import { Handle, Position } from '@xyflow/react'
import { useCanvas } from '../context/CanvasContext'
import type { FloorNodeData, FloorPolicy } from '../types'

const POLICIES: FloorPolicy[] = [
  'SEQUENTIAL', 'ROUND_ROBIN', 'MODERATED', 'FREE_FOR_ALL', 'SHOWRUNNER_DRIVEN',
]

const SESSION_STATE_COLOR: Record<string, string> = {
  idle: '#4a5568',
  running: '#38a169',
  stopped: '#e53e3e',
}

export function FloorNode({ data }: { data: FloorNodeData }) {
  const { onRun, onStop, onFloorChange } = useCanvas()
  const stateColor = SESSION_STATE_COLOR[data.sessionState] ?? '#4a5568'
  const isRunning = data.sessionState === 'running'

  return (
    <div style={{
      background: 'linear-gradient(135deg, #1a2332 0%, #0d1520 100%)',
      border: `2px solid ${stateColor}`,
      borderRadius: 12,
      padding: '14px 16px',
      minWidth: 260,
      width: 300,
      boxSizing: 'border-box',
      color: '#e6edf5',
      fontFamily: 'ui-sans-serif, system-ui, sans-serif',
      fontSize: 13,
      boxShadow: `0 0 20px ${stateColor}44`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <div style={{
          width: 10, height: 10, borderRadius: '50%',
          background: stateColor, boxShadow: `0 0 6px ${stateColor}`,
        }} />
        <span style={{ fontWeight: 700, fontSize: 14 }}>Floor</span>
        <span style={{
          marginLeft: 'auto', fontSize: 11, background: `${stateColor}33`,
          color: stateColor, borderRadius: 4, padding: '1px 6px',
        }}>
          {data.sessionState}
        </span>
      </div>

      <label style={{ display: 'block', marginBottom: 8 }}>
        <div style={{ color: '#93a4b8', fontSize: 11, marginBottom: 3 }}>Policy</div>
        <select className="nodrag nopan"
          value={data.policy}
          onChange={(e) => onFloorChange({ policy: e.target.value as FloorPolicy })}
          disabled={isRunning}
          style={{
            width: '100%', background: '#0d1520', color: '#e6edf5',
            border: '1px solid #2a3a4c', borderRadius: 6, padding: '4px 8px', fontSize: 12,
          }}
        >
          {POLICIES.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
      </label>

      <label style={{ display: 'block', marginBottom: 8 }}>
        <div style={{ color: '#93a4b8', fontSize: 11, marginBottom: 3 }}>Topic</div>
        <input className="nodrag nopan"
          type="text"
          value={data.topic}
          onChange={(e) => onFloorChange({ topic: e.target.value })}
          disabled={isRunning}
          placeholder="Optional starting topic…"
          style={{
            width: '100%', background: '#0d1520', color: '#e6edf5',
            border: '1px solid #2a3a4c', borderRadius: 6, padding: '4px 8px',
            fontSize: 12, boxSizing: 'border-box',
          }}
        />
      </label>

      <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
        <label style={{ flex: 1, minWidth: 0 }}>
          <div style={{ color: '#93a4b8', fontSize: 11, marginBottom: 3 }}>Max turns</div>
          <input className="nodrag nopan"
            type="number"
            value={data.maxTurns ?? ''}
            min={1}
            onChange={(e) => onFloorChange({ maxTurns: e.target.value ? Number(e.target.value) : null })}
            disabled={isRunning}
            placeholder="∞"
            style={{
              width: '100%', background: '#0d1520', color: '#e6edf5',
              border: '1px solid #2a3a4c', borderRadius: 6, padding: '4px 8px',
              fontSize: 12, boxSizing: 'border-box',
            }}
          />
        </label>
        <label style={{ flex: 1, minWidth: 0 }}>
          <div style={{ color: '#93a4b8', fontSize: 11, marginBottom: 3 }}>MOT (ms)</div>
          <input className="nodrag nopan"
            type="number"
            value={data.mot || ''}
            min={0}
            step={100}
            onChange={(e) => onFloorChange({ mot: e.target.value ? Number(e.target.value) : 0 })}
            placeholder="0"
            style={{
              width: '100%', background: '#0d1520', color: '#e6edf5',
              border: '1px solid #2a3a4c', borderRadius: 6, padding: '4px 8px',
              fontSize: 12, boxSizing: 'border-box',
            }}
          />
        </label>
      </div>

      <div style={{ marginBottom: 10 }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
          <input className="nodrag nopan"
            type="checkbox"
            checked={data.noHuman}
            onChange={(e) => onFloorChange({ noHuman: e.target.checked })}
            disabled={isRunning}
          />
          <span style={{ color: '#93a4b8', fontSize: 11 }}>No human</span>
        </label>
      </div>

      {isRunning && (
        <div style={{ display: 'flex', gap: 10, marginBottom: 10, fontSize: 11, color: '#93a4b8' }}>
          <span>Turns: <b style={{ color: '#58a6ff' }}>{data.turnCount}</b></span>
          <span>Elapsed: <b style={{ color: '#58a6ff' }}>{data.elapsedSecs}s</b></span>
        </div>
      )}

      <button className="nodrag nopan"
        onClick={isRunning ? onStop : onRun}
        style={{
          width: '100%', padding: '7px 0',
          background: isRunning ? '#6b2121' : '#1a4731',
          color: isRunning ? '#fc8181' : '#68d391',
          border: `1px solid ${isRunning ? '#fc818166' : '#68d39166'}`,
          borderRadius: 7, fontWeight: 600, fontSize: 13, cursor: 'pointer',
        }}
      >
        {isRunning ? '■ Stop' : '▶ Run'}
      </button>

      <Handle type="source" position={Position.Right} style={{ background: stateColor }} />
    </div>
  )
}
