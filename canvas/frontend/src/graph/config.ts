import type { Node, Edge } from '@xyflow/react'

export interface CanvasConfiguration { version: 1; nodes: Node[]; edges: Array<{source:string;target:string}> }
const FIELDS: Record<string, string[]> = {
  FloorNode: ['policy','topic','maxTurns','mot','showFloorEvents','noHuman','humanName','pinned'],
  AgentNode: ['provider','name','model','systemPrompt','slug','agentType','remoteTarget','pinned'],
  HumanNode: ['humanName','pinned'],
}

export function preferredConfiguration<T>(initial: T | null, saved: T | null, running: boolean, hasSession: boolean): T | null {
  // A live runner owns its configuration. Once stopped, preserve newer local
  // edits/imports; a fresh CLI prepopulation still takes precedence on launch.
  return running || initial && !hasSession ? initial ?? saved : saved ?? initial
}

export function serializeConfiguration(nodes: Node[], edges: Edge[]): CanvasConfiguration {
  const clean = nodes.filter(node => node.type && FIELDS[node.type]).map(node => ({
    id: node.id, type: node.type, position: {...node.position},
    data: Object.fromEntries(FIELDS[node.type!].filter(key => key in node.data).map(key => [key,node.data[key]])),
  }))
  const ids = new Set(clean.map(node => node.id))
  return {version:1,nodes:clean,edges:edges.filter(edge => ids.has(edge.source) && ids.has(edge.target)).map(({source,target}) => ({source,target}))}
}

export function parseConfiguration(text: string): CanvasConfiguration {
  if (new TextEncoder().encode(text).length > 1024*1024) throw new Error('Configuration exceeds 1 MiB')
  const data = JSON.parse(text)
  if (!data || !Array.isArray(data.nodes) || !Array.isArray(data.edges) || data.nodes.length > 2000) throw new Error('Expected a Canvas JSON configuration with nodes and edges')
  if (data.nodes.filter((node: Node) => node.type === 'FloorNode').length !== 1) throw new Error('Configuration needs exactly one floor')
  const ids = new Set<string>()
  const names = new Set<string>()
  for (const node of data.nodes) {
    if (!node || typeof node.id !== 'string' || ids.has(node.id) || !node.data || typeof node.data !== 'object') throw new Error('Nodes need unique IDs and configuration data')
    ids.add(node.id)
    if (node.type === 'AgentNode') {
      if (typeof node.data.name !== 'string' || !node.data.name.trim() || typeof node.data.provider !== 'string') throw new Error('Agents need a name and provider')
      const name = node.data.name.trim().toLowerCase()
      if (names.has(name)) throw new Error('Agent names must be unique')
      names.add(name)
    }
    if (node.position && ![node.position.x,node.position.y].every(Number.isFinite)) throw new Error('Node positions must be finite')
  }
  for (const edge of data.edges) if (!edge || !ids.has(edge.source) || !ids.has(edge.target)) throw new Error('Edges must refer to configured nodes')
  return data
}

export function restoreConfigurationLayout(nodes: Node[], config: CanvasConfiguration): Node[] {
  return nodes.map(node => {
    const saved = config.nodes.find(item => item.id === node.id || item.type === node.type &&
      (node.type === 'FloorNode' || node.type === 'AgentNode' && item.data.name === node.data.name ||
       node.type === 'HumanNode' && item.data.humanName === node.data.humanName))
    return saved?.position ? {...node,position:{...saved.position},data:{...node.data,pinned:!!saved.data.pinned}} : node
  })
}
