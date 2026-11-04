import dagre from 'dagre'
import type { Node, Edge } from '@xyflow/react'

export function applyDagreLayout(nodes: Node[], edges: Edge[]): Node[] {
  const floor = nodes.find(node => node.type === 'FloorNode')
  const participants = nodes.filter(node => node.type === 'AgentNode' || node.type === 'HumanNode')
  if (floor && participants.length) {
    // A membership star is a fleet, not a deep pipeline. Tile its participants
    // so adding voices does not create a single unreadably tall column.
    const columns = Math.ceil(Math.sqrt(participants.length))
    const width = Math.max(330, ...participants.map(node => (node.measured?.width ?? 280) + 40))
    const height = Math.max(240, ...participants.map(node => (node.measured?.height ?? 190) + 40))
    const positions = new Map(participants.map((node, index) => [node.id, {
      x: 350 + (index % columns) * width, y: Math.floor(index / columns) * height,
    }]))
    positions.set(floor.id, {x:0,y:Math.max(0,(Math.ceil(participants.length/columns)*height-330)/2)})
    nodes.filter(node => !positions.has(node.id)).forEach((node,index) => positions.set(node.id, {
      x:350+(index%columns)*width, y:(Math.ceil(participants.length/columns)+Math.floor(index/columns))*height+100,
    }))
    return nodes.map(node => node.data.pinned || !positions.has(node.id) ? node : {...node,position:positions.get(node.id)!})
  }
  const graph = new dagre.graphlib.Graph()
  graph.setGraph({ rankdir: 'LR', ranksep: 100, nodesep: 60 })
  graph.setDefaultEdgeLabel(() => ({}))
  nodes.forEach(node => graph.setNode(node.id, { width: node.measured?.width || 280, height: node.measured?.height || 210 }))
  const ids = new Set(nodes.map(node => node.id))
  edges.filter(edge => ids.has(edge.source) && ids.has(edge.target)).forEach(edge => graph.setEdge(edge.source, edge.target))
  dagre.layout(graph)
  return nodes.map(node => {
    if (node.data.pinned) return node
    const position = graph.node(node.id)
    return { ...node, position: { x: position.x - position.width / 2, y: position.y - position.height / 2 } }
  })
}
