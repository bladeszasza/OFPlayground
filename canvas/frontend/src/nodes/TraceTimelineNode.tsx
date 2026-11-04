// canvas/frontend/src/nodes/TraceTimelineNode.tsx
import { useState, useMemo } from 'react'
import { Handle, Position } from '@xyflow/react'
import type { ConversationNodeData, FloorEventEntry } from '../types'
import { useCanvas } from '../context/CanvasContext'

// ── Layout ────────────────────────────────────────────────────────────────────
const LANE_W = 160
const ML     = 72   // left margin — timestamps
const MT     = 46   // top margin — lane labels
const MR     = 20   // right margin
const ROW_H  = 36
const MIN_W  = 380
const FLOOR_LANE = 'Floor'

// ── Styling ───────────────────────────────────────────────────────────────────
const COLORS: Record<FloorEventEntry['type'], string> = {
  floor_grant:        '#38a169',
  floor_revoke:       '#e53e3e',
  floor_yield:        '#00bcd4',
  floor_request:      '#d4a843',
  utterance:          '#58a6ff',
  manifest_published: '#8b5cf6',
}

const LABELS: Record<FloorEventEntry['type'], string> = {
  floor_grant:        'floor_grant',
  floor_revoke:       'floor_revoke',
  floor_yield:        'floor_yield',
  floor_request:      'floor_request',
  utterance:          'utterance',
  manifest_published: 'manifest',
}

const FLOOR_EVENT_TYPES: Set<FloorEventEntry['type']> = new Set([
  'floor_grant', 'floor_revoke', 'floor_yield', 'floor_request',
])

function fmtTs(ts: number) {
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`
}

function bezier(x1: number, y1: number, x2: number, y2: number) {
  const mx = (x1 + x2) / 2
  const curve = Math.min(Math.abs(x2 - x1) * 0.4, 18)
  return `M${x1},${y1} C${mx},${y1 - curve} ${mx},${y2 + curve} ${x2},${y2}`
}

// ── Component ─────────────────────────────────────────────────────────────────
export function TraceTimelineNode({ data }: { data: ConversationNodeData }) {
  const { onSelectEvent } = useCanvas()
  const [senderFilter, setSenderFilter] = useState('__all__')
  const [activeTypes, setActiveTypes] = useState<Set<FloorEventEntry['type']>>(
    new Set<FloorEventEntry['type']>([
      'floor_grant','floor_revoke','floor_yield','floor_request','utterance','manifest_published',
    ])
  )

  const liveUrl = data.sessionId ? `/trace/live?session=${data.sessionId}` : null

  // Build lanes: Floor virtual lane first (if floor events exist), then agents by first-seen order
  const lanes = useMemo(() => {
    const seen = new Map<string, number>()
    let hasFloorEvents = false
    for (const e of data.eventLog) {
      if (FLOOR_EVENT_TYPES.has(e.type)) hasFloorEvents = true
      if (e.agent && !seen.has(e.agent)) seen.set(e.agent, e.ts)
      if (e.to && e.to !== e.agent && !seen.has(e.to)) seen.set(e.to, e.ts)
    }
    const agentLanes = [...seen.entries()].sort((a, b) => a[1] - b[1]).map(([n]) => n)
    return hasFloorEvents ? [FLOOR_LANE, ...agentLanes] : agentLanes
  }, [data.eventLog])

  // Event types that actually appear
  const presentTypes = useMemo(() => {
    const t = new Set<FloorEventEntry['type']>()
    for (const e of data.eventLog) t.add(e.type)
    return ([...t] as FloorEventEntry['type'][]).sort()
  }, [data.eventLog])

  const toggleType = (t: FloorEventEntry['type']) =>
    setActiveTypes(prev => {
      const next = new Set(prev)
      next.has(t) ? next.delete(t) : next.add(t)
      return next
    })

  // Sender filter applies to agent names; "Floor" lane is shown for floor events regardless
  const senderLanes = lanes.filter(l => l !== FLOOR_LANE)

  const filtered = useMemo(() =>
    data.eventLog.filter(e =>
      (senderFilter === '__all__' || e.agent === senderFilter) &&
      activeTypes.has(e.type)
    ),
    [data.eventLog, senderFilter, activeTypes]
  )

  const nodeW = Math.max(MIN_W, ML + lanes.length * LANE_W + MR)
  const svgW  = Math.max(nodeW - 28, ML + lanes.length * LANE_W + MR)
  const svgH  = MT + Math.max(1, filtered.length) * ROW_H + 16

  const laneX = (name: string) => {
    const idx = lanes.indexOf(name)
    return idx < 0 ? NaN : ML + idx * LANE_W + LANE_W / 2
  }

  // Resolve logical source/target for each event
  function eventRoute(evt: FloorEventEntry): { sx: number; tx: number } {
    const col = laneX(evt.agent)
    const floor = laneX(FLOOR_LANE)
    switch (evt.type) {
      case 'floor_grant':   return { sx: floor, tx: col }       // Floor → agent
      case 'floor_revoke':  return { sx: col,   tx: floor }     // agent → Floor
      case 'floor_request': return { sx: col,   tx: floor }     // agent → Floor
      case 'floor_yield':   return { sx: col,   tx: floor }     // agent → Floor
      default:              return { sx: col,   tx: NaN }       // dot only
    }
  }

  return (
    <div style={{
      background: 'linear-gradient(135deg, #1a1f2e 0%, #0d1117 100%)',
      border: '2px solid #30363d',
      borderRadius: 10,
      padding: '12px 14px',
      width: nodeW,
      color: '#e6edf5',
      fontFamily: 'ui-sans-serif, system-ui, sans-serif',
      fontSize: 12,
      boxSizing: 'border-box',
    }}>
      <Handle type="target" position={Position.Top} />

      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
        <span style={{ fontSize: 15 }}>⏱</span>
        <span style={{ fontWeight: 700, fontSize: 13 }}>Timeline</span>
        <span style={{ color: '#4a5568', fontSize: 11 }}>
          {filtered.length} / {data.eventLog.length} events · {lanes.length - (lanes.includes(FLOOR_LANE) ? 1 : 0)} agents
        </span>
        {liveUrl && (
          <button
            className="nodrag"
            onClick={() => window.open(liveUrl, '_blank')}
            title="Open full trace in browser"
            style={{
              marginLeft: 'auto', background: '#1a2a3a', color: '#58a6ff',
              border: '1px solid #58a6ff44', borderRadius: 4,
              padding: '2px 8px', fontSize: 10, cursor: 'pointer',
            }}
          >
            ⤢ Full
          </button>
        )}
      </div>

      {/* ── Static filter bar ─────────────────────────────────────────────── */}
      <div
        className="nodrag nowheel nopan"
        style={{
          background: '#0d1117',
          border: '1px solid #21262d',
          borderRadius: 7,
          padding: '8px 10px',
          marginBottom: 10,
          display: 'flex',
          flexDirection: 'column',
          gap: 8,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ color: '#93a4b8', fontSize: 11, flexShrink: 0 }}>Sender:</span>
          <select
            value={senderFilter}
            onChange={e => setSenderFilter(e.target.value)}
            style={{
              background: '#0d1520', color: '#dce8f5', border: '1px solid #2a3f53',
              borderRadius: 5, padding: '3px 6px', fontSize: 11,
              fontFamily: 'ui-sans-serif, system-ui, sans-serif',
            }}
          >
            <option value="__all__">All senders</option>
            {senderLanes.map(name => <option key={name} value={name}>{name}</option>)}
          </select>
        </div>

        {presentTypes.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px 14px' }}>
            {presentTypes.map(t => (
              <label
                key={t}
                style={{ display: 'inline-flex', alignItems: 'center', gap: 5, cursor: 'pointer', fontSize: 11 }}
              >
                <input
                  type="checkbox"
                  checked={activeTypes.has(t)}
                  onChange={() => toggleType(t)}
                  style={{ accentColor: COLORS[t], cursor: 'pointer', margin: 0 }}
                />
                <span style={{
                  width: 10, height: 10, borderRadius: 2,
                  background: COLORS[t], display: 'inline-block', flexShrink: 0,
                  border: '1px solid rgba(255,255,255,0.15)',
                }} />
                <span style={{ color: '#c9d1d9' }}>{LABELS[t]}</span>
              </label>
            ))}
          </div>
        )}
      </div>

      {/* ── SVG Timeline ──────────────────────────────────────────────────── */}
      <div
        className="nodrag nowheel nopan"
        onWheel={e => e.stopPropagation()}
        style={{
          background: '#0b111a',
          border: '1px solid #21262d',
          borderRadius: 7,
          overflowY: 'auto',
          overflowX: 'auto',
          maxHeight: 360,
        }}
      >
        {filtered.length === 0 ? (
          <div style={{
            height: 120, display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: '#4a5568', fontSize: 11,
          }}>
            {data.eventLog.length === 0 ? 'Run a session to see the timeline' : 'No events match the current filters'}
          </div>
        ) : (
          <svg width={svgW} height={svgH} style={{ display: 'block' }}>
            <defs>
              <marker id="tl-arr" viewBox="0 -4 8 8" refX="7" refY="0" markerWidth="5" markerHeight="5" orient="auto">
                <path d="M0,-4L8,0L0,4" fill="#7a9ab5" />
              </marker>
              {/* Colored arrow markers per event type */}
              {(Object.entries(COLORS) as [FloorEventEntry['type'], string][]).map(([type, col]) => (
                <marker key={type} id={`arr-${type}`} viewBox="0 -4 8 8" refX="7" refY="0" markerWidth="5" markerHeight="5" orient="auto">
                  <path d="M0,-4L8,0L0,4" fill={col} />
                </marker>
              ))}
            </defs>

            {/* Alternating row bands */}
            {filtered.map((_, i) =>
              i % 2 === 0 ? null : (
                <rect key={`band-${i}`}
                  x={0} y={MT + i * ROW_H - ROW_H / 2}
                  width={9999} height={ROW_H}
                  fill="rgba(255,255,255,0.022)"
                />
              )
            )}

            {/* Vertical lane lines */}
            {lanes.map(name => {
              const x = laneX(name)
              const isFloor = name === FLOOR_LANE
              return (
                <g key={`lane-${name}`}>
                  <line
                    x1={x} y1={MT - 18}
                    x2={x} y2={svgH - 6}
                    stroke={isFloor ? '#6ea8d9' : '#2a3e52'}
                    strokeWidth={isFloor ? 2 : 1}
                    opacity={isFloor ? 0.6 : 1}
                  />
                  <rect
                    x={x - 54} y={4}
                    width={108} height={22}
                    rx={5}
                    fill={isFloor ? '#0d2030' : '#111c28'}
                    stroke={isFloor ? '#4a7299' : '#2a3e52'}
                    strokeWidth={1}
                  />
                  <text
                    x={x} y={19}
                    textAnchor="middle"
                    fill={isFloor ? '#6ea8d9' : '#b8ccdf'}
                    fontSize={11}
                    fontWeight={600}
                    fontFamily="ui-sans-serif, system-ui, sans-serif"
                  >
                    {name.length > 13 ? `${name.slice(0, 12)}…` : name}
                  </text>
                </g>
              )
            })}

            {/* Events */}
            {filtered.map((evt, i) => {
              const y   = MT + i * ROW_H
              const col = COLORS[evt.type] ?? '#8a99ac'
              const { sx, tx } = eventRoute(evt)
              const hasSrc = !Number.isNaN(sx)
              const hasDst = !Number.isNaN(tx) && tx !== sx
              const dotX  = hasSrc ? sx : NaN
              const r     = evt.type === 'utterance' ? 6 : 5
              const selected = evt.cursor !== undefined && evt.cursor === data.selectedCursor

              return (
                <g key={evt.id} role="button" tabIndex={0} className="nodrag nopan"
                  aria-label={`${fmtTs(evt.ts)} ${evt.type} ${evt.agent}`} aria-pressed={selected}
                  style={{cursor:'pointer'}} onClick={() => onSelectEvent(evt)}
                  onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); onSelectEvent(evt) } }}>
                  {selected && <rect x={0} y={y-ROW_H/2} width={svgW} height={ROW_H} fill="#8bcaff22" stroke="#8bcaff" strokeWidth={1} />}
                  {/* Timestamp */}
                  <text x={6} y={y + 4} fill="#4a6278" fontSize={10}
                    fontFamily="ui-monospace, 'Courier New', monospace">
                    {fmtTs(evt.ts)}
                  </text>

                  {/* Route arrow */}
                  {hasSrc && hasDst && (
                    <path
                      d={bezier(sx, y, tx, y)}
                      fill="none"
                      stroke={col}
                      strokeWidth={1.8}
                      opacity={0.8}
                      markerEnd={`url(#arr-${evt.type})`}
                    />
                  )}

                  {/* Dot on source lane */}
                  {!Number.isNaN(dotX) && (
                    <circle cx={dotX} cy={y} r={r}
                      fill={col} stroke="#0b111a" strokeWidth={1.5}
                      style={{ cursor: 'pointer' }}>
                      <title>{`${evt.type} — ${evt.agent}${evt.preview ? `: ${evt.preview}` : ''}`}</title>
                    </circle>
                  )}

                  {/* Dot at arrow destination (target) */}
                  {hasDst && (
                    <circle cx={tx} cy={y} r={3}
                      fill={col} stroke="#0b111a" strokeWidth={1} opacity={0.5} />
                  )}

                  {/* Utterance preview text */}
                  {evt.type === 'utterance' && evt.preview && !Number.isNaN(dotX) && (
                    <text x={dotX + r + 8} y={y + 4}
                      fill="#527085" fontSize={10} fontStyle="italic"
                      fontFamily="ui-sans-serif, system-ui, sans-serif">
                      {evt.preview.length > 30 ? `${evt.preview.slice(0, 29)}…` : evt.preview}
                    </text>
                  )}
                </g>
              )
            })}
          </svg>
        )}
      </div>

      <Handle type="source" position={Position.Bottom} />
    </div>
  )
}
