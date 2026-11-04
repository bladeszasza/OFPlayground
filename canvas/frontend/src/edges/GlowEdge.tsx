import { EdgeLabelRenderer, getBezierPath } from '@xyflow/react'
import type { EdgeProps } from '@xyflow/react'
import type { GlowEdgeData } from '../types'

export function GlowEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data }: EdgeProps) {
  const edge = data as unknown as GlowEdgeData
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition })
  const flow = edge?.layer === 'flow'
  const color = !flow && edge?.active ? '#72d19a' : edge?.color ?? '#34475b'
  const marker = `route-${String(id).replace(/[^a-zA-Z0-9_-]/g, '_')}`
  return <>
    <defs>{flow && <marker id={marker} viewBox="0 -4 9 8" refX="8" refY="0" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M0,-4L9,0L0,4" fill={color} />
    </marker>}</defs>
    <path id={id} className="react-flow__edge-path" d={path} markerEnd={flow ? `url(#${marker})` : undefined}
      style={{ stroke: color, strokeWidth: flow ? 2 : edge?.active ? 2.5 : 1, fill: 'none',
        strokeDasharray: edge?.dashed ? '5 4' : undefined, opacity: flow ? .85 : .55 }} />
    {flow && edge.active && <path className="ofp-observed-route-flash" d={path}
      style={{ stroke: color, strokeWidth: 3, strokeDasharray: '9 14', fill: 'none', pointerEvents: 'none' }} />}
    {flow && edge.label && <EdgeLabelRenderer><div className="nodrag nopan" style={{
      position: 'absolute', transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
      background: '#0f1a29', color, border: `1px solid ${color}77`, borderRadius: 4,
      padding: '2px 5px', fontSize: 9, maxWidth: 200, pointerEvents: 'none',
    }}>{edge.label}</div></EdgeLabelRenderer>}
  </>
}
