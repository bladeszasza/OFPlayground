import { apiFetch } from './api'
import { version as appVersion } from '../package.json'
// canvas/frontend/src/App.tsx
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type NodeTypes,
  type ReactFlowInstance,
  type EdgeTypes,
  BackgroundVariant,
} from '@xyflow/react'
import { applyDagreLayout } from './graph/layout'
import { parseConfiguration, serializeConfiguration, restoreConfigurationLayout, preferredConfiguration } from './graph/config'

import { CanvasContext } from './context/CanvasContext'
import type { CanvasCallbacks } from './context/CanvasContext'
import { FloorNode } from './nodes/FloorNode'
import { AgentNode } from './nodes/AgentNode'
import { HumanNode } from './nodes/HumanNode'
import { ConversationNode } from './nodes/ConversationNode'
import { TraceTimelineNode } from './nodes/TraceTimelineNode'
import { ConversationChatNode } from './nodes/ConversationChatNode'
import { EventInspectorNode } from './nodes/EventInspectorNode'
import { ImageArtifactNode } from './nodes/ImageArtifactNode'
import { CodeArtifactNode } from './nodes/CodeArtifactNode'
import { PhaseArtifactNode } from './nodes/PhaseArtifactNode'
import { MusicArtifactNode } from './nodes/MusicArtifactNode'
import { GlowEdge } from './edges/GlowEdge'
import { SlugCombobox } from './components/SlugCombobox'
import { useSlugs } from './hooks/useSlugs'
import { useModels, useProviderConfiguration } from './hooks/useModels'
import { usePresets } from './hooks/usePresets'
import { useRemoteAgents } from './hooks/useRemoteAgents'
import { useSession } from './hooks/useSession'
import { useAgentStatus } from './hooks/useAgentStatus'
import { AgentStatusDock } from './components/AgentStatusDock'
import { GraphActivityPanel } from './components/GraphActivityPanel'
import { useGraphActivity } from './hooks/useGraphActivity'
import type { ActivityRecord } from './graph/activity'

import type {
  FloorNodeData, AgentNodeData, HumanNodeData, ConversationNodeData,
  ConversationChatNodeData, EventInspectorNodeData,
  ArtifactNodeData, GlowEdgeData, WSEvent, FloorPolicy, AgentFloorState, Message,
  FloorEventEntry,
} from './types'

// ── Stable module-level constants — never recreated on render ─────────────────

const NODE_TYPES: NodeTypes = {
  FloorNode,
  AgentNode,
  HumanNode,
  ConversationNode,
  TraceTimelineNode,
  ConversationChatNode,
  EventInspectorNode,
  ImageArtifactNode,
  CodeArtifactNode,
  PhaseArtifactNode,
  MusicArtifactNode,
}

const EDGE_TYPES = { glow: GlowEdge } as unknown as EdgeTypes

const ARTIFACT_COLOR: Record<string, string> = {
  image: '#8b5cf6', code: '#10a37f', file: '#10a37f',
  phase: '#58a6ff', markdown: '#58a6ff', music: '#f97316',
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function artifactNodeType(kind: string): string {
  if (kind === 'image') return 'ImageArtifactNode'
  if (kind === 'code' || kind === 'file') return 'CodeArtifactNode'
  if (kind === 'music') return 'MusicArtifactNode'
  return 'PhaseArtifactNode'
}

function appendArtifactNode(nodes: Node[], artifact: Node): Node[] {
  const previous = nodes.filter(node => node.id !== artifact.id)
  const artifacts = previous.filter(node => node.type?.includes('ArtifactNode'))
  const expired = new Set(artifacts.slice(0, Math.max(0, artifacts.length - 99)).map(node => node.id))
  return [...previous.filter(node => !expired.has(node.id)), artifact]
}

function nextArtifactPosition(
  currentNodes: Node[],
  agentNodeId: string,
): { x: number; y: number } {
  const agentNode = currentNodes.find(n => n.id === agentNodeId)
  const agentName = (agentNode?.data as AgentNodeData | undefined)?.name ?? ''
  const baseX = (agentNode?.position.x ?? 400) + 260
  const baseY = agentNode?.position.y ?? 100
  const agentArtifactCount = currentNodes.filter(n =>
    (n.type?.includes('ArtifactNode') ?? false) &&
    asArtifact(n.data)?.agentName === agentName
  ).length
  return { x: baseX, y: baseY + agentArtifactCount * 140 }
}

function glowEdge(
  id: string, source: string, target: string,
  opts: Partial<GlowEdgeData> = {},
): Edge {
  return {
    id,
    source,
    target,
    type: 'glow',
    deletable: false,
    data: { active: false, color: '#2a3a4c', dashed: false, layer: opts.dashed ? 'artifact' : 'membership', ...opts } satisfies GlowEdgeData,
  }
}

function toFloorEntry(event: WSEvent): FloorEventEntry | null {
  const id = event.cursor === undefined ? `${Date.now()}-${Math.random()}` : `cursor-${event.cursor}`
  const ts = event.ts ? new Date(event.ts).getTime() : Date.now()
  // Prefer the full OFP envelope JSON when available, fall back to the WS event itself
  const raw = (event as unknown as Record<string, unknown>).envelope_json as Record<string, unknown> | undefined
    ?? (event as unknown as Record<string, unknown>)
  switch (event.type) {
    case 'floor_grant':
      return { id, ts, cursor: event.cursor, replay: event.replay, type: 'floor_grant', agent: event.to, to: event.to, raw }
    case 'floor_revoke':
      return { id, ts, cursor: event.cursor, replay: event.replay, type: 'floor_revoke', agent: event.from, raw }
    case 'floor_yield':
      return { id, ts, cursor: event.cursor, replay: event.replay, type: 'floor_yield', agent: event.from, raw }
    case 'floor_request':
      return { id, ts, cursor: event.cursor, replay: event.replay, type: 'floor_request', agent: event.from, raw }
    case 'utterance':
      return { id, ts, cursor: event.cursor, replay: event.replay, type: 'utterance', agent: event.sender, preview: event.text.slice(0, 70), raw }
    case 'manifest_published':
      return { id, ts, cursor: event.cursor, replay: event.replay, type: 'manifest_published', agent: event.agent, raw }
    default:
      return null
  }
}

// ── Constants ─────────────────────────────────────────────────────────────────

const FLOOR_ID     = 'floor-main'
const CONV_ID      = 'conv-main'
const TIMELINE_ID  = 'timeline-main'
const CHAT_ID      = 'chat-main'
const INSPECTOR_ID = 'inspector-main'
const LAYOUT_KEY   = 'ofp-canvas-layout-v6'  // bumped: inspector + wider chat
const PIN_KEY = 'ofp-canvas-pins-v1'
const VIEWPORT_KEY = 'ofp-canvas-viewport-v1'
const CONFIG_KEY = 'ofp-canvas-configuration-v1'
const API = ''

// ── Typed data accessors ──────────────────────────────────────────────────────

function asFloor(data: unknown): FloorNodeData { return data as unknown as FloorNodeData }
function asAgent(data: unknown): AgentNodeData { return data as unknown as AgentNodeData }
function asHuman(data: unknown): HumanNodeData { return data as unknown as HumanNodeData }
function asConv(data: unknown): ConversationNodeData { return data as unknown as ConversationNodeData }
function asChat(data: unknown): ConversationChatNodeData { return data as unknown as ConversationChatNodeData }
function asArtifact(data: unknown): ArtifactNodeData { return data as unknown as ArtifactNodeData }
function asGlowEdge(data: unknown): GlowEdgeData { return data as unknown as GlowEdgeData }
function asInspector(data: unknown): EventInspectorNodeData { return data as unknown as EventInspectorNodeData }

function restorePositions(nodes: Node[]): Node[] {
  try {
    const positions: Record<string, { x: number; y: number }> = JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{}')
    const pins: string[] = JSON.parse(localStorage.getItem(PIN_KEY) || '[]')
    return nodes.map(node => ({ ...node,
      position: positions[node.id] && Number.isFinite(positions[node.id].x) && Number.isFinite(positions[node.id].y) ? positions[node.id] : node.position,
      data: { ...node.data, pinned: pins.includes(node.id) } }))
  } catch { return nodes }
}

// ── Initial nodes ─────────────────────────────────────────────────────────────

function makeInitialNodes(): Node[] {
  return applyDagreLayout(
    [
      {
        id: FLOOR_ID, type: 'FloorNode', position: { x: 0, y: 0 },
        data: {
          policy: 'SEQUENTIAL', topic: '', maxTurns: null, mot: 0,
          showFloorEvents: false, noHuman: true, humanName: 'User',
          sessionState: 'idle', sessionId: null, turnCount: 0, elapsedSecs: 0,
        } satisfies FloorNodeData,
      },
      {
        id: CONV_ID, type: 'ConversationNode', position: { x: 0, y: 0 },
        data: {
          sessionId: null, policy: '', topic: '', turnCount: 0, eventLog: [],
        } satisfies ConversationNodeData,
      },
      {
        id: TIMELINE_ID, type: 'TraceTimelineNode', position: { x: 0, y: 0 },
        data: {
          sessionId: null, policy: '', topic: '', turnCount: 0, eventLog: [],
        } satisfies ConversationNodeData,
      },
      {
        id: CHAT_ID, type: 'ConversationChatNode', position: { x: 0, y: 0 },
        data: { messages: [] } satisfies ConversationChatNodeData,
      },
      {
        id: INSPECTOR_ID, type: 'EventInspectorNode', position: { x: 0, y: 0 },
        data: { event: null } satisfies EventInspectorNodeData,
      },
    ],
    [
      glowEdge(`e-${FLOOR_ID}-${CONV_ID}`, FLOOR_ID, CONV_ID),
      glowEdge(`e-${CONV_ID}-${TIMELINE_ID}`, CONV_ID, TIMELINE_ID),
      glowEdge(`e-${FLOOR_ID}-${CHAT_ID}`, FLOOR_ID, CHAT_ID),
      glowEdge(`e-${TIMELINE_ID}-${INSPECTOR_ID}`, TIMELINE_ID, INSPECTOR_ID),
    ]
  )
}

function hydrateFromConfig(config: { nodes: any[]; edges: any[] }): {
  nodes: Node[]; edges: Edge[]
} {
  const rfNodes: Node[] = []
  const rfEdges: Edge[] = []

  for (const n of config.nodes ?? []) {
    if (n.type === 'FloorNode') {
      rfNodes.push({
        id: FLOOR_ID, type: 'FloorNode', position: { x: 0, y: 0 },
        data: {
          policy: n.data?.policy ?? 'SEQUENTIAL',
          topic: n.data?.topic ?? '',
          maxTurns: n.data?.maxTurns ?? null,
          mot: n.data?.mot ?? 0,
          noHuman: n.data?.noHuman ?? true,
          humanName: n.data?.humanName ?? 'User',
          showFloorEvents: false,
          sessionState: 'idle' as const,
          sessionId: null,
          turnCount: 0,
          elapsedSecs: 0,
        } satisfies FloorNodeData,
      })
    } else if (n.type === 'AgentNode') {
      const agentId = `agent-${n.data?.name}`
      rfNodes.push({
        id: agentId, type: 'AgentNode', position: { x: 0, y: 0 },
        data: {
          provider: n.data?.provider ?? 'anthropic',
          name: n.data?.name ?? '',
          model: n.data?.model ?? '',
          systemPrompt: n.data?.systemPrompt ?? '',
          slug: n.data?.slug ?? '',
          agentType: n.data?.agentType ?? '',
          remoteTarget: n.data?.remoteTarget ?? '',
          floorState: 'waiting' as const,
          messages: [],
        } satisfies AgentNodeData,
      })
      rfEdges.push(glowEdge(`e-${FLOOR_ID}-${agentId}`, FLOOR_ID, agentId))
    } else if (n.type === 'HumanNode') {
      rfNodes.push({
        id: 'human-main', type: 'HumanNode', position: { x: 0, y: 0 },
        data: {
          humanName: n.data?.humanName ?? 'User',
          messages: [],
        } satisfies HumanNodeData,
      })
      rfEdges.push(glowEdge('e-floor-human-main', FLOOR_ID, 'human-main'))
    }
  }

  if (!rfNodes.find(n => n.id === CONV_ID)) {
    rfNodes.push({
      id: CONV_ID, type: 'ConversationNode', position: { x: 0, y: 0 },
      data: {
        sessionId: null, policy: '', topic: '', turnCount: 0, eventLog: [],
      } satisfies ConversationNodeData,
    })
    rfEdges.push(glowEdge(`e-${FLOOR_ID}-${CONV_ID}`, FLOOR_ID, CONV_ID))
  }

  if (!rfNodes.find(n => n.id === TIMELINE_ID)) {
    rfNodes.push({
      id: TIMELINE_ID, type: 'TraceTimelineNode', position: { x: 0, y: 0 },
      data: {
        sessionId: null, policy: '', topic: '', turnCount: 0, eventLog: [],
      } satisfies ConversationNodeData,
    })
    rfEdges.push(glowEdge(`e-${CONV_ID}-${TIMELINE_ID}`, CONV_ID, TIMELINE_ID))
  }

  if (!rfNodes.find(n => n.id === CHAT_ID)) {
    rfNodes.push({
      id: CHAT_ID, type: 'ConversationChatNode', position: { x: 0, y: 0 },
      data: { messages: [] } satisfies ConversationChatNodeData,
    })
    rfEdges.push(glowEdge(`e-${FLOOR_ID}-${CHAT_ID}`, FLOOR_ID, CHAT_ID))
  }

  if (!rfNodes.find(n => n.id === INSPECTOR_ID)) {
    rfNodes.push({
      id: INSPECTOR_ID, type: 'EventInspectorNode', position: { x: 0, y: 0 },
      data: { event: null } satisfies EventInspectorNodeData,
    })
    rfEdges.push(glowEdge(`e-${TIMELINE_ID}-${INSPECTOR_ID}`, TIMELINE_ID, INSPECTOR_ID))
  }

  return {
    nodes: applyDagreLayout(rfNodes, rfEdges),
    edges: rfEdges,
  }
}

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  const [nodes, setNodes, onNodesChange] = useNodesState(makeInitialNodes())
  const [edges, setEdges, onEdgesChange] = useEdgesState([
    glowEdge(`e-${FLOOR_ID}-${CONV_ID}`, FLOOR_ID, CONV_ID),
    glowEdge(`e-${CONV_ID}-${TIMELINE_ID}`, CONV_ID, TIMELINE_ID),
    glowEdge(`e-${FLOOR_ID}-${CHAT_ID}`, FLOOR_ID, CHAT_ID),
    glowEdge(`e-${TIMELINE_ID}-${INSPECTOR_ID}`, TIMELINE_ID, INSPECTOR_ID),
  ])
  const [sessionId, setSessionId] = useState<string | null>(null)
  const activity = useGraphActivity(sessionId)
  const [showMembership, setShowMembership] = useState(true)
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth >= 900)
  const [configurationReady, setConfigurationReady] = useState(false)
  const [configurationNotice, setConfigurationNotice] = useState('')
  const [showFlow, setShowFlow] = useState(true)
  const [showDetails, setShowDetails] = useState(false)
  const [systemReducedMotion, setSystemReducedMotion] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches)
  const [manualReducedMotion, setManualReducedMotion] = useState(false)
  const reduceMotion = systemReducedMotion || manualReducedMotion
  useEffect(() => {
    const media = matchMedia('(prefers-reduced-motion: reduce)')
    const changed = () => setSystemReducedMotion(media.matches)
    media.addEventListener('change', changed)
    return () => media.removeEventListener('change', changed)
  }, [])
  const [hasViewport] = useState(() => { try { return !!localStorage.getItem(VIEWPORT_KEY) } catch { return false } })
  const { snapshot: agentStatus, streaming: statusStreaming } = useAgentStatus(sessionId)
  const flowRef = useRef<ReactFlowInstance | null>(null)
  const manualConfiguration = useRef(false)
  const [connected, setConnectedState] = useState(false)
  const [errorBanner, setErrorBanner] = useState<string | null>(null)
  const [showAddAgent, setShowAddAgent] = useState(false)
  const [addingAgent, setAddingAgent] = useState(false)
  const startedAt = useRef<number>(0)
  const elapsedTimer = useRef<ReturnType<typeof setInterval> | null>(null)
  const motRef = useRef<number>(0)
  const eventBufferRef = useRef<WSEvent[]>([])
  const drainingRef = useRef(false)

  const slugs = useSlugs()
  const models = useModels()
  const providerConfiguration = useProviderConfiguration()
  const presets = usePresets()
  const remoteAgents = useRemoteAgents()
  const [selectedPreset, setSelectedPreset] = useState('')

  useEffect(() => {
    Promise.all([
      apiFetch('/session/initial').then(r => r.ok ? r.json() : null),
      apiFetch('/session/status').then(r => { if (!r.ok) throw new Error('Session observation unavailable'); return r.json() }),
    ]).then(([config, status]) => {
        if (manualConfiguration.current) return
        let saved: string | null = null
        try { saved = localStorage.getItem(CONFIG_KEY) } catch { /* optional browser storage */ }
        const resolved = preferredConfiguration(config, saved ? parseConfiguration(saved) : null, status.running, !!status.session_id)
        if (!resolved) {
          setConfigurationReady(true)
          setTimeout(() => void flowRef.current?.fitView({padding:.15,duration:0}), 100)
          return
        }
        const hydrated = hydrateFromConfig(resolved)
        setNodes(restorePositions(hydrated.nodes))
        setEdges(hydrated.edges)
        setConfigurationReady(true)
          if (!status.running || !status.session_id) return
          setSessionId(status.session_id)
          startedAt.current = (status.started_at ?? Date.now()/1000) * 1000
          setNodes(previous => previous.map(node => node.type === 'FloorNode' ? { ...node,
            data: { ...node.data, sessionId: status.session_id, sessionState: 'running' } } : node))
          if (elapsedTimer.current) clearInterval(elapsedTimer.current)
          elapsedTimer.current = setInterval(() => setNodes(previous => previous.map(node => node.type === 'FloorNode' ? {...node,
            data:{...node.data,elapsedSecs:Math.floor((Date.now()-startedAt.current)/1000)}} : node)),1000)
      })
      .catch(() => setErrorBanner('Configuration could not be restored. Your stored configuration has been retained.'))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Restore layout ──────────────────────────────────────────────────────────
  useEffect(() => {
    try {
      setNodes(restorePositions)
    } catch { /* ignore */ }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const positionState = JSON.stringify(Object.fromEntries(nodes.map(node => [node.id, node.position])))
  const pinState = JSON.stringify(nodes.filter(node => node.data.pinned).map(node => node.id))
  const configurationState = JSON.stringify(serializeConfiguration(nodes, edges))
  useEffect(() => {
    if (!configurationReady) return
    const timer = setTimeout(() => {
      try { localStorage.setItem(CONFIG_KEY, configurationState) } catch { /* storage may be disabled */ }
    }, 350)
    return () => clearTimeout(timer)
  }, [configurationState, configurationReady])
  useEffect(() => {
    const timer = setTimeout(() => {
      try { localStorage.setItem(LAYOUT_KEY, positionState); localStorage.setItem(PIN_KEY, pinState) }
      catch { /* browser storage is optional */ }
    }, 350)
    return () => clearTimeout(timer)
  }, [positionState, pinState])

  // ── Helpers ─────────────────────────────────────────────────────────────────
  const patchNode = useCallback(<T extends object>(id: string, patch: Partial<T>) => {
    setNodes((nds) => nds.map((n) => n.id === id ? { ...n, data: { ...n.data, ...patch } } : n))
  }, [setNodes])

  const isRunning = asFloor(nodes.find((n) => n.id === FLOOR_ID)?.data)?.sessionState === 'running'
  const nodeIdState = nodes.map(node => node.id).sort().join('\n')
  useEffect(() => {
    const ids = new Set(nodeIdState.split('\n'))
    setEdges(previous => previous.filter(edge => ids.has(edge.source) && ids.has(edge.target)))
  }, [nodeIdState, setEdges])

  // ── WS event handler ────────────────────────────────────────────────────────
  const handleEvent = useCallback((event: WSEvent) => {
    if (event.type === 'replay_gap') {
      setErrorBanner('Some earlier events are no longer available. Showing retained history.')
      return
    }
    // Append to conversation event log for all renderable event types
    const entry = toFloorEntry(event)
    if (entry) {
      setNodes(nds => nds.map(n => {
        if (n.id !== CONV_ID && n.id !== TIMELINE_ID) return n
        const cd = asConv(n.data)
        return {
          ...n,
          data: {
            ...cd,
            eventLog: [...cd.eventLog.slice(-199), entry],
            turnCount: event.type === 'utterance' ? cd.turnCount + 1 : cd.turnCount,
          },
        }
      }))
    }
    switch (event.type) {
      case 'utterance': {
        const msg: Message = {
          id: `${Date.now()}-${Math.random()}`,
          sender: event.sender, text: event.text, ts: Date.now(), media: event.media,
        }
        setNodes((nds) => nds.map((n) => {
          if (n.type === 'FloorNode') return { ...n, data: { ...n.data, turnCount: asFloor(n.data).turnCount + 1 } }
          if (n.type === 'AgentNode' && asAgent(n.data).name === event.sender) {
            const nd = asAgent(n.data)
            return { ...n, data: { ...nd, messages: [...nd.messages.slice(-99), msg] } }
          }
          if (n.type === 'HumanNode' && asHuman(n.data).humanName === event.sender) {
            const nd = asHuman(n.data)
            return { ...n, data: { ...nd, messages: [...nd.messages.slice(-99), msg] } }
          }
          if (n.id === CHAT_ID) {
            const nd = asChat(n.data)
            return { ...n, data: { ...nd, messages: [...nd.messages.slice(-299), msg] } }
          }
          return n
        }))
        // Image utterance → create ImageArtifactNode
        if (event.media?.type === 'image') {
          const agentNodeId = `agent-${event.sender}`
          const artifactId = `artifact-img-${Date.now()}`
          setNodes((nds) => {
            const artNode: Node = {
              id: artifactId, type: 'ImageArtifactNode',
              position: nextArtifactPosition(nds, agentNodeId),
              data: {
                kind: 'image', agentName: event.sender,
                label: 'image', preview: event.media!.url, url: event.media!.url,
              } satisfies ArtifactNodeData,
            }
            return appendArtifactNode(nds, artNode)
          })
          setEdges((eds) => [...eds, glowEdge(
            `e-${agentNodeId}-${artifactId}`,
            agentNodeId, artifactId,
            { color: '#8b5cf6', dashed: true },
          )])
        }
        break
      }

      case 'floor_snapshot': {
        if (event.turn_count !== undefined) patchNode<FloorNodeData>(FLOOR_ID,{turnCount:event.turn_count})
        break
      }

      case 'floor_grant': {
        setNodes((nds) => nds.map((n) => {
          if (n.type !== 'AgentNode') return n
          const nd = asAgent(n.data)
          const state: AgentFloorState = nd.name === event.to ? 'speaking' : 'waiting'
          return { ...n, data: { ...nd, floorState: state } }
        }))
        const targetAgentId = `agent-${event.to}`
        setEdges((eds) => eds.map((e) => ({
          ...e,
          data: {
            ...asGlowEdge(e.data),
            active: e.source === FLOOR_ID && e.target === targetAgentId,
            pulse: null,   // clear any pending request pulse
          },
        })))
        break
      }

      case 'floor_revoke': {
        setNodes((nds) => nds.map((n) => {
          if (n.type !== 'AgentNode') return n
          const nd = asAgent(n.data)
          if (nd.name === event.from) return { ...n, data: { ...nd, floorState: 'waiting' as AgentFloorState } }
          return n
        }))
        setEdges((eds) => eds.map((e) => ({ ...e, data: { ...asGlowEdge(e.data), active: false } })))
        break
      }

      case 'floor_request': {
        const targetAgentId = `agent-${event.from}`
        setNodes((nds) => nds.map((n) => {
          if (n.type !== 'AgentNode') return n
          const nd = asAgent(n.data)
          if (nd.name !== event.from) return n
          return { ...n, data: { ...nd, floorState: 'requesting' as AgentFloorState } }
        }))
        // pulse the edge amber while requesting
        setEdges(eds => eds.map(e =>
          e.source === FLOOR_ID && e.target === targetAgentId
            ? { ...e, data: { ...asGlowEdge(e.data), pulse: 'amber' as const } }
            : e
        ))
        break
      }

      case 'floor_yield': {
        const targetAgentId = `agent-${event.from}`
        setNodes(nds => nds.map(n => {
          if (n.type !== 'AgentNode') return n
          const nd = asAgent(n.data)
          if (nd.name !== event.from) return n
          return { ...n, data: { ...nd, floorState: 'yielding' as AgentFloorState } }
        }))
        setEdges(eds => eds.map(e =>
          e.source === FLOOR_ID && e.target === targetAgentId
            ? { ...e, data: { ...asGlowEdge(e.data), active: false, pulse: 'teal' as const } }
            : e
        ))
        break
      }

      case 'manifest_published': {
        // Capability publication is not a floor or work transition.
        break
      }

      case 'agent_spawning': {
        setNodes(nds => nds.map(n => n.type === 'AgentNode' && asAgent(n.data).name === event.name
          ? { ...n, data: { ...n.data, floorState: 'spawning' as AgentFloorState } } : n))
        break
      }

      case 'agent_joined': {
        setNodes((nds) => {
          if (event.provider === 'human') {
            return nds.map(n => n.type === 'HumanNode' ? { ...n, data: { ...n.data,
              humanName: event.name, instanceId: event.instance_id, speakerUri: event.speaker_uri } } : n)
          }
          const exists = nds.find((n) => n.type === 'AgentNode' && asAgent(n.data).name === event.name)
          if (exists) {
            return nds.map((n) =>
              n.id === exists.id ? { ...n, data: { ...n.data, floorState: 'waiting' as AgentFloorState,
                instanceId: event.instance_id, speakerUri: event.speaker_uri } } : n
            )
          }
          const newId = `agent-${event.name}`
          const newNode: Node = {
            id: newId, type: 'AgentNode',
            position: { x: 400, y: 100 + nds.filter((n) => n.type === 'AgentNode').length * 220 },
            data: {
              provider: event.provider as AgentNodeData['provider'],
              name: event.name, model: '', systemPrompt: '', slug: '',
              instanceId: event.instance_id, speakerUri: event.speaker_uri,
              agentType: '', remoteTarget: '', floorState: 'waiting', messages: [],
            } satisfies AgentNodeData,
          }
          setEdges((eds) => [...eds, glowEdge(`e-${FLOOR_ID}-${newId}`, FLOOR_ID, newId)])
          return [...nds, newNode]
        })
        break
      }

      case 'agent_kicked': {
        setNodes((nds) => {
          const ids = new Set(nds.filter(n => n.type === 'AgentNode' && asAgent(n.data).name === event.name || n.type === 'HumanNode' && asHuman(n.data).humanName === event.name).map(n => n.id))
          if (nds.some(n => ids.has(n.id) && n.selected)) setTimeout(() => void flowRef.current?.fitView({padding:.15,duration:0}), 100)
          setEdges(eds => eds.filter(e => !ids.has(e.source) && !ids.has(e.target)))
          return nds.filter(n => !ids.has(n.id))
        })
        break
      }

      case 'artifact_saved': {
        const agentNodeId = `agent-${event.agent}`
        const artifactId = `artifact-${event.slug ?? Date.now()}`
        const color = ARTIFACT_COLOR[event.kind] ?? '#58a6ff'
        const nodeType = artifactNodeType(event.kind)
        setNodes((nds) => {
          const artNode: Node = {
            id: artifactId, type: nodeType,
            position: nextArtifactPosition(nds, agentNodeId),
            data: {
              kind: event.kind as ArtifactNodeData['kind'],
              agentName: event.agent,
              slug: event.slug,
              label: event.slug ?? 'artifact',
              preview: event.preview,
            } satisfies ArtifactNodeData,
          }
          return appendArtifactNode(nds, artNode)
        })
        setEdges((eds) => [...eds, glowEdge(
          `e-${agentNodeId}-${artifactId}`,
          agentNodeId, artifactId,
          { color, dashed: true },
        )])
        break
      }

      case 'image_saved': {
        const agentNodeId = `agent-${event.agent}`
        const artifactId = `artifact-img-saved-${Date.now()}`
        setNodes((nds) => {
          const artNode: Node = {
            id: artifactId, type: 'ImageArtifactNode',
            position: nextArtifactPosition(nds, agentNodeId),
            data: {
              kind: 'image', agentName: event.agent,
              label: event.url.split('/').pop() ?? 'image',
              preview: event.url, url: event.url,
            } satisfies ArtifactNodeData,
          }
          return appendArtifactNode(nds, artNode)
        })
        setEdges((eds) => [...eds, glowEdge(
          `e-${agentNodeId}-${artifactId}`,
          agentNodeId, artifactId,
          { color: '#8b5cf6', dashed: true },
        )])
        break
      }

      case 'memory_saved': {
        const agentNodeId = `agent-${event.agent ?? 'unknown'}`
        const artifactId = `artifact-mem-${Date.now()}`
        setNodes((nds) => {
          const artNode: Node = {
            id: artifactId, type: 'PhaseArtifactNode',
            position: nextArtifactPosition(nds, agentNodeId),
            data: {
              kind: 'phase', agentName: event.agent ?? 'unknown',
              label: event.category, preview: event.content.slice(0, 120),
            } satisfies ArtifactNodeData,
          }
          return appendArtifactNode(nds, artNode)
        })
        setEdges((eds) => [...eds, glowEdge(
          `e-${agentNodeId}-${artifactId}`,
          agentNodeId, artifactId,
          { color: '#58a6ff', dashed: true },
        )])
        break
      }

      case 'error': {
        setNodes((nds) => nds.map((n) => {
          if (n.type === 'AgentNode' && asAgent(n.data).name === event.agent)
            return { ...n, data: { ...n.data, floorState: 'error' as AgentFloorState, errorMessage: event.message } }
          return n
        }))
        break
      }

      case 'session_ended': {
        patchNode<FloorNodeData>(FLOOR_ID, { sessionState: 'stopped' })
        patchNode<ConversationNodeData>(CONV_ID, { sessionId: null })
        patchNode<ConversationNodeData>(TIMELINE_ID, { sessionId: null })
        // Keep chat messages after session ends so user can read them
        setEdges((eds) => eds.map((e) => ({ ...e, data: { ...asGlowEdge(e.data), active: false } })))
        if (elapsedTimer.current) clearInterval(elapsedTimer.current)
        break
      }
    }
  }, [patchNode, setEdges, setNodes])

  // ── Session control ──────────────────────────────────────────────────────────
  const handleRun = useCallback(async () => {
    manualConfiguration.current = true
    setErrorBanner(null)
    const floorNode = nodes.find((n) => n.id === FLOOR_ID)
    if (!floorNode) return
    const fd = asFloor(floorNode.data)
    const graphNodes = nodes.map((n) => ({ id: n.id, type: n.type, data: n.data }))
    const graphEdges = edges.map((e) => ({ source: e.source, target: e.target }))
    try {
      const res = await apiFetch(`${API}/session/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nodes: graphNodes, edges: graphEdges }),
      })
      if (!res.ok) {
        const detail = await res.json().then((d: { detail?: string }) => d.detail).catch(() => res.statusText)
        setErrorBanner(`Start failed: ${detail}`)
        return
      }
      const { session_id } = await res.json() as { session_id: string }
      eventBufferRef.current = []
      setNodes(previous => previous.filter(node => !node.type?.includes('ArtifactNode')).map(node =>
        node.type === 'AgentNode' || node.type === 'HumanNode' ? { ...node, data: { ...node.data, messages: [], floorState: 'waiting',
          asp: undefined, instanceId: undefined, speakerUri: undefined, errorMessage: undefined } } : node))
      setSessionId(session_id)
      startedAt.current = Date.now()
      if (elapsedTimer.current) clearInterval(elapsedTimer.current)
      patchNode<FloorNodeData>(FLOOR_ID, { sessionState: 'running', sessionId: session_id, turnCount: 0, elapsedSecs: 0 })
      const convPatch = { sessionId: session_id, policy: fd.policy, topic: fd.topic, turnCount: 0, eventLog: [] }
      patchNode<ConversationNodeData>(CONV_ID, convPatch)
      patchNode<ConversationNodeData>(TIMELINE_ID, convPatch)
      patchNode<ConversationChatNodeData>(CHAT_ID, { messages: [] })
      elapsedTimer.current = setInterval(() => {
        patchNode<FloorNodeData>(FLOOR_ID, { elapsedSecs: Math.floor((Date.now() - startedAt.current) / 1000) })
      }, 1000)
    } catch (e) {
      setErrorBanner(`Network error: ${e}`)
    }
  }, [nodes, edges, patchNode])

  const handleStop = useCallback(async () => {
    try {
      const response = await apiFetch(`${API}/session/stop`, { method: 'DELETE' })
      if (!response.ok) throw new Error((await response.json()).detail ?? 'Stop rejected')
      patchNode<FloorNodeData>(FLOOR_ID, { sessionState: 'stopped' })
      setEdges((eds) => eds.map((e) => ({ ...e, data: { ...asGlowEdge(e.data), active: false } })))
      if (elapsedTimer.current) clearInterval(elapsedTimer.current)
    } catch (error) { setErrorBanner(`Stop failed: ${error}`) }
  }, [patchNode, setEdges])

  // ── MOT event buffering ───────────────────────────────────────────────────────
  // Keep motRef current so the async drain loop always reads the latest value
  useEffect(() => {
    const floorNode = nodes.find(n => n.id === FLOOR_ID)
    motRef.current = asFloor(floorNode?.data)?.mot ?? 0
  }, [nodes])

  const drainBuffer = useCallback(async () => {
    if (drainingRef.current) return
    drainingRef.current = true
    while (eventBufferRef.current.length > 0) {
      const evt = eventBufferRef.current.shift()!
      handleEvent(evt)
      const delay = motRef.current
      if (delay > 0) {
        await new Promise<void>(r => setTimeout(r, delay))
      }
    }
    drainingRef.current = false
  }, [handleEvent])

  const onEvent = useCallback((event: WSEvent) => {
    activity.observe(event)
    if (eventBufferRef.current.length >= 1000) {
      eventBufferRef.current.splice(0, eventBufferRef.current.length - 999)
      setErrorBanner('Presentation buffer exceeded 1,000 events. Some visual history was skipped; ASP remains live.')
    }
    eventBufferRef.current.push(event)
    drainBuffer()
  }, [drainBuffer, activity.observe])

  // ── WS ───────────────────────────────────────────────────────────────────────
  const { connected: wsConnected, sendMessage, kickAgent } = useSession({ sessionId, onEvent })
  useEffect(() => setConnectedState(wsConnected), [wsConnected])

  // ── Add agent ─────────────────────────────────────────────────────────────
  const [newAgent, setNewAgent] = useState({
    provider: 'anthropic', name: '', model: '', systemPrompt: '', slug: '', agentType: '', remoteTarget: '',
  })
  const providerModels = models[newAgent.provider] ?? []
  const providerInfo = providerConfiguration.providers[newAgent.provider]
  const selectedTask = newAgent.agentType || 'text-generation'
  const configuredModel = providerInfo?.defaults[selectedTask]
  const [personaPreview, setPersonaPreview] = useState('')
  const [personaProblem, setPersonaProblem] = useState('')
  const [previewPending, setPreviewPending] = useState(false)
  const previewRequest = useRef(0)
  useEffect(() => {
    previewRequest.current++; setPersonaPreview(''); setPersonaProblem(''); setPreviewPending(false)
  }, [newAgent.slug])
  async function previewPersona() {
    const request = ++previewRequest.current
    setPreviewPending(true); setPersonaProblem('')
    try {
      const response = await apiFetch('/agents/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reference:newAgent.slug})})
      const body = await response.json()
      if (!response.ok) throw new Error(body.detail ?? 'Persona could not be resolved')
      if (request === previewRequest.current) setPersonaPreview(body.prompt)
    } catch (error) { if (request === previewRequest.current) setPersonaProblem(error instanceof Error ? error.message : 'Preview unavailable') }
    finally { if (request === previewRequest.current) setPreviewPending(false) }
  }

  const handleAddAgent = useCallback(async () => {
    if (newAgent.provider === 'remote' ? !newAgent.remoteTarget.trim() : !newAgent.name.trim()) return
    if (addingAgent) return
    if (nodes.some(n => n.type === 'AgentNode' && asAgent(n.data).name.toLowerCase() === newAgent.name.trim().toLowerCase())) {
      setErrorBanner('An agent with that name already exists.')
      return
    }
    if (isRunning) {
      setAddingAgent(true)
      try {
        const response = await apiFetch(`${API}/session/agent/add`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(newAgent),
        })
        if (!response.ok) throw new Error((await response.json()).detail ?? 'Agent rejected')
      } catch (err) {
        setErrorBanner(String(err))
        return
      } finally {
        setAddingAgent(false)
      }
    } else {
      const newId = `agent-${newAgent.name}`
      const newNode: Node = {
        id: newId, type: 'AgentNode',
        position: { x: 400, y: 100 + nodes.filter((n) => n.type === 'AgentNode').length * 220 },
        data: {
          provider: newAgent.provider as AgentNodeData['provider'],
          name: newAgent.name, model: newAgent.model,
          systemPrompt: newAgent.systemPrompt, slug: newAgent.slug,
          agentType: newAgent.agentType, remoteTarget: newAgent.remoteTarget,
          floorState: 'waiting', messages: [],
        } satisfies AgentNodeData,
      }
      setNodes((nds) => [...nds, newNode])
      setEdges((eds) => [...eds, glowEdge(`e-${FLOOR_ID}-${newId}`, FLOOR_ID, newId)])
    }
    setNewAgent({ provider: 'anthropic', name: '', model: '', systemPrompt: '', slug: '', agentType: '', remoteTarget: '' })
    setShowAddAgent(false)
  }, [newAgent, addingAgent, isRunning, nodes, setNodes, setEdges])

  const loadPreset = useCallback(async (name: string) => {
    if (!name) return
    manualConfiguration.current = true
    try {
      const config = await apiFetch(`/presets/${name}`).then(r => r.json())
      const hydrated = hydrateFromConfig(config)
      setNodes(hydrated.nodes)
      setEdges(hydrated.edges)
      setSelectedPreset('')
      setSessionId(null)
      setConfigurationReady(true)
      setTimeout(() => void flowRef.current?.fitView({ padding: .15, duration: 0 }), 100)
    } catch { setErrorBanner('Preset could not be loaded. Try again.') }
  }, [setNodes, setEdges])

  async function importConfiguration(file?: File) {
    if (!file || isRunning) return
    manualConfiguration.current = true
    try {
      if (file.size > 1024 * 1024) throw new Error('Configuration exceeds 1 MiB')
      const config = parseConfiguration(await file.text())
      const hydrated = hydrateFromConfig(config)
      setNodes(restoreConfigurationLayout(hydrated.nodes, config))
      setEdges(hydrated.edges); setSessionId(null); setConfigurationReady(true)
      setErrorBanner(null); setConfigurationNotice('Configuration loaded. Review providers and prompts before Run.')
      setTimeout(() => void flowRef.current?.fitView({padding:.15,duration:0}), 100)
    } catch (error) { setErrorBanner(`Import failed: ${error instanceof Error ? error.message : 'Invalid JSON'}`) }
  }

  function exportConfiguration() {
    const url = URL.createObjectURL(new Blob([configurationState], {type:'application/json'}))
    const link = document.createElement('a'); link.href = url; link.download = 'ofp-canvas.json'; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    setConfigurationNotice('Saved locally. Authored prompts and remote URLs are included; runtime output, status and credential fields are excluded. Review before sharing.')
  }

  const handleSelectEvent = useCallback((event: FloorEventEntry | null) => {
    patchNode<EventInspectorNodeData>(INSPECTOR_ID, { event })
    if (event?.cursor !== undefined) {
      const record = activity.records.find(row => row.event.cursor === event.cursor)
      if (record) activity.setCursor(record.id)
    }
    if (event?.agent) activity.setFocus(event.agent)
    if (event) setNodes(previous => previous.map(node => ({...node, selected:
      (node.type === 'AgentNode' && asAgent(node.data).name === event.agent) ||
      (node.type === 'HumanNode' && asHuman(node.data).humanName === event.agent) ||
      (node.id === FLOOR_ID && event.type.startsWith('floor_') && !event.raw?.parent_conversation_id),
    })))
  }, [patchNode, activity.records, activity.setCursor, activity.setFocus, setNodes])

  const handleKick = useCallback(async (name: string) => {
    if (isRunning) {
      await kickAgent(name)
    } else {
      const ids = new Set(nodes.filter(n => n.type === 'AgentNode' && asAgent(n.data).name === name).map(n => n.id))
      setNodes(nds => nds.filter(n => !ids.has(n.id)))
      setEdges(eds => eds.filter(e => !ids.has(e.source) && !ids.has(e.target)))
    }
  }, [isRunning, kickAgent, nodes, setNodes, setEdges])

  // ── Context value ─────────────────────────────────────────────────────────
  useEffect(() => {
    setNodes(nds => nds.map(node => {
      if (node.type !== 'AgentNode' && node.type !== 'HumanNode') return node
      const data = asAgent(node.data)
      const name = node.type === 'HumanNode' ? asHuman(node.data).humanName : data.name
      const status = agentStatus.agents.find(agent => agent.agentId === data.instanceId ||
        agent.ext['local.ofp-playground']?.speakerUri === data.speakerUri || agent.name === name)
      if (!status) return node
      return { ...node, data: { ...data, instanceId: status.agentId,
        asp: { lifecycle: status.lifecycle.value, health: status.health.value, governance: status.governance.value,
          stale: !agentStatus.available || status.lifecycle.value === 'unreachable', provenance: status.health.source } } }
    }))
  }, [agentStatus, setNodes])

  const focusStatusAgent = useCallback((agentId: string) => {
    const name = agentStatus.participants.find(p => p.agentId === agentId)?.name
    const target = nodes.find(node => (node.type === 'AgentNode' || node.type === 'HumanNode') && (asAgent(node.data).instanceId === agentId || asAgent(node.data).name === name || asHuman(node.data).humanName === name))
    if (!target) return
    setNodes(nds => nds.map(node => ({ ...node, selected: node.id === target.id })))
    void flowRef.current?.fitView({ nodes: [{ id: target.id }], padding: 0.7, duration: reduceMotion ? 0 : 200 })
  }, [agentStatus.participants, nodes, setNodes, reduceMotion])

  const callbacks = useMemo<CanvasCallbacks>(() => ({
    onRun: handleRun,
    onStop: handleStop,
    onFloorChange: (patch) => patchNode<FloorNodeData>(FLOOR_ID, patch),
    onKick: handleKick,
    onSend: sendMessage,
    onSelectEvent: handleSelectEvent,
    sessionRunning: isRunning,
  }), [handleRun, handleStop, patchNode, handleKick, sendMessage, handleSelectEvent, isRunning])

  const inspectRecord = useCallback((record: ActivityRecord) => {
    const entry = toFloorEntry(record.event)
    if (entry) patchNode<EventInspectorNodeData>(INSPECTOR_ID, { event: { ...entry, cursor: record.event.cursor, replay: true } })
    const name = record.event.type === 'floor_snapshot' ? record.event.holder_name : entry?.agent
    const targets = nodes.filter(node =>
      (node.type === 'AgentNode' && asAgent(node.data).name === name) ||
      (node.type === 'HumanNode' && asHuman(node.data).humanName === name) ||
      (node.id === FLOOR_ID && record.event.type.startsWith('floor_') && !record.event.parent_conversation_id))
    if (targets.length) {
      const selected = new Set(targets.map(node => node.id))
      setNodes(previous => previous.map(node => ({ ...node, selected: selected.has(node.id) })))
      void flowRef.current?.fitView({ nodes: targets.map(({id}) => ({id})), padding: .8,
        duration: reduceMotion ? 0 : 180 })
    }
  }, [nodes, patchNode, setNodes, reduceMotion])

  const presentationNodes = useMemo<Node[]>(() => {
    const historicalMessages: Message[] = activity.cursor === null ? [] : activity.visible.flatMap(({ id, observedAt, event }) =>
      event.type === 'utterance' ? [{ id: `replay-${id}`, sender: event.sender, text: event.text,
        ts: observedAt, media: event.media }] : [])
    const floor = activity.floor
    return nodes.map(node => {
      const detail = ['ConversationNode', 'TraceTimelineNode', 'EventInspectorNode', 'ConversationChatNode'].includes(node.type ?? '')
      let data = node.data
      if (node.type === 'AgentNode') {
        const agent = asAgent(data)
        const uri = agent.speakerUri || floor?.participants.find(p => p.name === agent.name)?.speaker_uri
        const floorState: AgentFloorState = floor && uri ? floor.holders.includes(uri) ? 'speaking' : floor.queue.includes(uri) ? 'requesting' : 'waiting' : agent.floorState
        data = { ...agent, floorState, replaying: activity.cursor !== null,
          ...(activity.cursor !== null ? { messages: historicalMessages.filter(message => message.sender === agent.name).slice(-100) } : {}) }
      }
      if (node.type === 'ConversationChatNode' && activity.cursor !== null) data = { ...data, messages: historicalMessages.slice(-300) }
      if (node.type === 'HumanNode' && activity.cursor !== null) data = { ...data,
        messages: historicalMessages.filter(message => message.sender === asHuman(data).humanName).slice(-100) }
      if ((node.id === CONV_ID || node.id === TIMELINE_ID) && activity.cursor !== null) data = { ...data,
        eventLog: activity.visible.flatMap(row => { const entry = toFloorEntry(row.event); return entry ? [{ ...entry, cursor: row.event.cursor, replay: true }] : [] }).slice(-200) }
      if (node.id === TIMELINE_ID) data = {...data,selectedCursor:activity.cursor === null ? undefined : activity.visible[activity.visible.length-1]?.event.cursor}
      return { ...node, data, hidden: detail && !showDetails,
        style: { ...node.style, ...(activity.focus && node.type === 'AgentNode' && asAgent(data).name !== activity.focus ? { opacity: .42 } : { opacity: 1 }) } }
    })
  }, [nodes, activity.visible, activity.cursor, activity.floor, activity.focus, showDetails])

  const presentationEdges = useMemo(() => {
    const lookup = new Map<string, string>()
    nodes.forEach(node => {
      if (node.type === 'AgentNode' && asAgent(node.data).speakerUri) lookup.set(asAgent(node.data).speakerUri!, node.id)
    })
    activity.floor?.participants.forEach(participant => {
      const node = nodes.find(node => node.type === 'AgentNode' && asAgent(node.data).name === participant.name ||
        node.type === 'HumanNode' && asHuman(node.data).humanName === participant.name)
      if (node) lookup.set(participant.speaker_uri, node.id)
    })
    const visibleIds = new Set(presentationNodes.filter(node => !node.hidden).map(node => node.id))
    const membership = showMembership ? edges.filter(edge => visibleIds.has(edge.source) && visibleIds.has(edge.target)).map(edge => {
      const targetUri = [...lookup].find(([, id]) => id === edge.target)?.[0]
      return { ...edge, data: { ...asGlowEdge(edge.data), pulse: null,
        active: edge.source === FLOOR_ID && !!targetUri && !!activity.floor?.holders.includes(targetUri) } }
    }) : []
    const flows: Edge[] = showFlow ? activity.routes.flatMap((route, index) => {
      const source = lookup.get(route.source), target = lookup.get(route.target)
      if (!source || !target || source === target || !visibleIds.has(source) || !visibleIds.has(target)) return []
      const failed = route.outcome !== 'queued'
      return [glowEdge(`observed-${index}-${source}-${target}`, source, target, {
        layer: 'flow', label: `${route.type} · ${route.outcome}${route.count > 1 ? ` ×${route.count}` : ''}`,
        color: failed ? '#fc8d89' : '#69baff', dashed: failed, failed,
        active: activity.cursor === null && !route.replay && activity.now - route.observedAt < 2100,
        observedAt: route.observedAt,
      })]
    }) : []
    return [...membership, ...flows]
  }, [nodes, edges, presentationNodes, activity.floor, activity.routes, activity.cursor, activity.now, showMembership, showFlow])

  const layoutUnpinned = useCallback(() => {
    setNodes(previous => applyDagreLayout(previous, edges.filter(edge => !asGlowEdge(edge.data)?.layer || asGlowEdge(edge.data).layer !== 'flow')))
  }, [edges, setNodes])
  const togglePins = useCallback(() => {
    setNodes(previous => previous.map(node => node.selected ? { ...node, data: { ...node.data, pinned: !node.data.pinned } } : node))
  }, [setNodes])


  return (
    <div className={reduceMotion ? 'ofp-reduced-motion' : undefined} style={{ width: '100vw', height: '100vh', display: 'flex', background: '#0d1117' }}>
      {/* ── Sidebar ─────────────────────────────────────────────────────────── */}
      <div style={{
        width: 220, background: '#161b22', borderRight: '1px solid #21262d',
        display: sidebarOpen ? 'flex' : 'none', flexDirection: 'column', padding: '14px 10px',
        gap: 8, flexShrink: 0, overflowY: 'auto',
      }}>
        <div style={{ color: '#58a6ff', fontWeight: 700, fontSize: 14, marginBottom: 6 }}>OFP Canvas <small style={{fontSize:10}}>v{appVersion}</small></div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: '#93a4b8' }}>
          <div style={{ width: 7, height: 7, borderRadius: '50%', background: connected ? '#38a169' : '#e53e3e' }} />
          {connected ? 'Connected' : 'Disconnected'}
        </div>

        <button onClick={() => void (isRunning ? handleStop() : handleRun())}
          style={{ background: isRunning ? '#632c30' : '#1c4d3e', color: '#f1f7ff', border: '1px solid #536a80', padding: 9, borderRadius: 6, cursor: 'pointer' }}>
          {isRunning ? 'Stop session' : 'Run session'}
        </button>
        <button onClick={exportConfiguration} style={inputStyle}>Save configuration</button>
        <label style={{fontSize:11,color:'#b7c5d8'}}>Load configuration JSON
          <input aria-label="Load configuration JSON" type="file" accept=".json,application/json" disabled={isRunning}
            style={{display:'block',fontSize:10,maxWidth:200,marginTop:5}}
            onChange={event => { void importConfiguration(event.target.files?.[0]); event.target.value='' }} />
        </label>
        {configurationNotice && <p role="status" style={{fontSize:10,color:'#b7c5d8'}}>{configurationNotice}</p>}

        {presets.length > 0 && (
          <>
            <div style={{ color: '#93a4b8', fontSize: 10, fontWeight: 600, textTransform: 'uppercase', letterSpacing: 1 }}>
              Load Preset
            </div>
            <select
              aria-label="Load preset"
              value={selectedPreset}
              onChange={e => { setSelectedPreset(e.target.value); loadPreset(e.target.value) }}
              disabled={isRunning}
              style={{
                width: '100%', background: '#0d1520', color: '#e6edf5',
                border: '1px solid #2a3a4c', borderRadius: 5,
                padding: '4px 7px', fontSize: 11, fontFamily: 'inherit',
              }}
            >
              <option value="">Select a preset…</option>
              {presets.map(p => (
                <option key={p.name} value={p.name} title={p.description}>
                  {p.title}
                </option>
              ))}
            </select>
            <hr style={{ border: 'none', borderTop: '1px solid #21262d' }} />
          </>
        )}

        <hr style={{ border: 'none', borderTop: '1px solid #21262d' }} />

        <div style={{ color: '#93a4b8', fontSize: 10, fontWeight: 600, textTransform: 'uppercase', letterSpacing: 1 }}>
          Add Agent
        </div>

        <button
          onClick={() => setShowAddAgent(!showAddAgent)}
          style={{
            background: '#1a2a3a', color: '#58a6ff', border: '1px solid #58a6ff44',
            borderRadius: 6, padding: '7px 0', fontSize: 12, cursor: 'pointer', fontWeight: 600,
          }}
        >
          + Agent
        </button>

        {showAddAgent && (
          <div style={{
            background: '#0d1520', border: '1px solid #2a3a4c', borderRadius: 7, padding: 10,
            display: 'flex', flexDirection: 'column', gap: 6, fontSize: 11,
          }}>
            {/* Provider */}
            <select
              aria-label="Provider"
              value={newAgent.provider}
              onChange={(e) => setNewAgent((a) => ({ ...a, provider: e.target.value, remoteTarget: '', name: '', agentType: e.target.value === 'demo' ? 'normal' : '' }))}
              style={inputStyle}
            >
              <option value="anthropic">Anthropic</option>
              <option value="openai">OpenAI</option>
              <option value="google">Google</option>
              <option value="huggingface">HuggingFace</option>
              <option value="remote">Remote OFP</option>
              <option value="demo">Synthetic demo · no provider key</option>
            </select>

            {newAgent.provider === 'remote' ? (
              <>
                {/* Remote target: slug or URL with known-slug suggestions */}
                <div style={{ position: 'relative' }}>
                  <input
                    aria-label="Remote agent slug or URL" list="remote-agent-options"
                    type="text"
                    placeholder="Slug (e.g. polly) or URL"
                    value={newAgent.remoteTarget}
                    onChange={(e) => {
                      const v = e.target.value
                      const match = remoteAgents.find(r => r.slug === v)
                      setNewAgent((a) => ({
                        ...a,
                        remoteTarget: v,
                        name: match ? match.name : a.name,
                      }))
                    }}
                    style={inputStyle}
                  />
                  <datalist id="remote-agent-options">{remoteAgents.map(agent => <option key={agent.slug} value={agent.slug}>{agent.name}</option>)}</datalist>
                </div>
                {/* Display name (auto-filled from slug) */}
                <input
                  type="text"
                  aria-label="Remote display name"
                  placeholder="Display name (auto-filled)"
                  value={newAgent.name}
                  onChange={(e) => setNewAgent((a) => ({ ...a, name: e.target.value }))}
                  style={inputStyle}
                />
              </>
            ) : (
              <>
                {/* Type */}
                <select
                  aria-label="Task or synthetic scenario"
                  value={newAgent.agentType}
                  onChange={(e) => setNewAgent((a) => ({ ...a, agentType: e.target.value }))}
                  style={inputStyle}
                >
                  {(newAgent.provider === 'demo' ? (providerInfo?.tasks ?? ['normal']) : ['', ...(providerInfo?.tasks ?? [])]).map((t) => (
                    <option key={t} value={t}>{t === '' ? '(default)' : t}</option>
                  ))}
                </select>

                {/* Name */}
                <input
                  type="text"
                  aria-label="Agent name"
                  placeholder="Name (required)"
                  value={newAgent.name}
                  onChange={(e) => setNewAgent((a) => ({ ...a, name: e.target.value }))}
                  style={inputStyle}
                />

                {newAgent.provider === 'demo' && <p style={{ color: '#a9cce8', margin: 0 }}>Scripted local runtime. Status comes from actual ASP instrumentation; no model call is made.</p>}
                {newAgent.provider !== 'demo' && <p role="status" style={{color:'#a9cce8',margin:0}}>
                  {providerInfo ? (providerInfo.credentialConfigured ? 'Provider key configured. Account and model access have not been checked.' : 'Provider key missing. Configure it in TOML or the server environment before Run or a live add.') : (providerConfiguration.problem || 'Loading provider settings…')}
                  {' '}{configuredModel ? `Default: ${configuredModel}.` : 'Uses the task adapter default unless you enter a model.'}
                  {' '}{isRunning ? 'Adding starts a participant immediately and may incur provider charges.' : 'Adding configures a participant. Run starts provider requests and may incur charges.'}
                </p>}
                {/* Model with suggestions */}
                <div style={{ position: 'relative' }}>
                  <input
                    type="text"
                    aria-label="Model" list="provider-model-options"
                    placeholder="Model (optional)"
                    value={newAgent.model}
                    onChange={(e) => setNewAgent((a) => ({ ...a, model: e.target.value }))}
                    style={inputStyle}
                  />
                  <datalist id="provider-model-options">{providerModels.map(model => <option key={model} value={model} />)}</datalist>
                </div>

                {/* Slug/system prompt with autocomplete */}
                <SlugCombobox
                  value={newAgent.systemPrompt || newAgent.slug}
                  onChange={(v) => setNewAgent((a) => ({
                    ...a,
                    systemPrompt: v.startsWith('@') ? '' : v,
                    slug: v.startsWith('@') ? v : '',
                  }))}
                  slugs={slugs}
                  style={inputStyle}
                />
                <button disabled={!newAgent.slug || previewPending} onClick={() => void previewPersona()} style={inputStyle}>{previewPending ? 'Resolving persona…' : 'Preview resolved persona'}</button>
                {personaProblem && <p role="alert">{personaProblem}</p>}
                {personaPreview && <textarea aria-label="Resolved persona instructions" readOnly value={personaPreview} rows={9} style={{...inputStyle,resize:'vertical'}} />}
              </>
            )}

            <button
              disabled={addingAgent || !(newAgent.provider === 'remote' ? newAgent.remoteTarget.trim() : newAgent.name.trim())}
              onClick={handleAddAgent}
              style={{
                background: '#1a4731', color: '#68d391', border: '1px solid #68d39166',
                borderRadius: 5, padding: '5px 0', cursor: 'pointer', fontSize: 11,
              }}
            >
              {addingAgent ? 'Adding participant…' : 'Add'}
            </button>
          </div>
        )}

        <hr style={{ border: 'none', borderTop: '1px solid #21262d' }} />

        {errorBanner && (
          <div role="alert" style={{
            background: '#2d1515', border: '1px solid #e53e3e44',
            borderRadius: 5, padding: '6px 8px', color: '#fc8181', fontSize: 11,
          }}>
            {errorBanner}
            <button
              aria-label="Dismiss error"
              onClick={() => setErrorBanner(null)}
              style={{ float: 'right', background: 'none', border: 'none', color: '#fc8181', cursor: 'pointer' }}
            >×</button>
          </div>
        )}
      </div>

      {/* ── Canvas ──────────────────────────────────────────────────────────── */}
      <CanvasContext.Provider value={callbacks}>
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
        <div style={{ flex: 1, minHeight: 0, position: 'relative' }}>
          <ReactFlow<Node, Edge>
            onInit={instance => {
              flowRef.current = instance
              try { const saved = JSON.parse(localStorage.getItem(VIEWPORT_KEY) || 'null'); if (saved && [saved.x, saved.y, saved.zoom].every(Number.isFinite)) void instance.setViewport(saved) } catch { /* optional */ }
            }}
            onMoveEnd={(_, viewport) => { try { localStorage.setItem(VIEWPORT_KEY, JSON.stringify(viewport)) } catch { /* optional */ } }}
            onNodeDragStop={(_, moved) => setNodes(previous => previous.map(node => node.id === moved.id ? { ...node, data: { ...node.data, pinned: true } } : node))}
            nodes={presentationNodes}
            edges={presentationEdges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            nodesConnectable={false}
            edgesReconnectable={false}
            deleteKeyCode={null}
            nodeTypes={NODE_TYPES}
            edgeTypes={EDGE_TYPES}
            fitView={!hasViewport}
            onlyRenderVisibleElements
            minZoom={0.05}
            maxZoom={2}
            proOptions={{ hideAttribution: true }}
            style={{ background: '#0d1117' }}
          >
            <Background color="#1a2332" variant={BackgroundVariant.Dots} gap={20} size={1} />
            <Controls style={{ background: '#161b22', border: '1px solid #21262d' }} />
            <MiniMap
              style={{ background: '#161b22', border: '1px solid #21262d' }}
              nodeColor="#2a3a4c"
            />
          </ReactFlow>
          <div className="graph-layer-controls" aria-label="Graph layers and layout">
            <button aria-expanded={sidebarOpen} onClick={() => setSidebarOpen(!sidebarOpen)}>Configuration</button>
            <label><input type="checkbox" checked={showMembership} onChange={e => setShowMembership(e.target.checked)} />Membership</label>
            <label><input type="checkbox" checked={showFlow} onChange={e => setShowFlow(e.target.checked)} />Observed routes</label>
            <label><input type="checkbox" checked={showDetails} onChange={e => setShowDetails(e.target.checked)} />Trace & chat</label>
            <label title={systemReducedMotion ? 'Your browser requests reduced motion' : 'Disable route animation and animated focus'}><input type="checkbox" checked={reduceMotion} disabled={systemReducedMotion} onChange={e => setManualReducedMotion(e.target.checked)} />Reduce motion</label>
            <button onClick={layoutUnpinned}>Layout unpinned</button>
            <button onClick={togglePins} disabled={!nodes.some(node => node.selected)}>Pin / unpin selected</button>
            <button onClick={() => { const selected = nodes.filter(node => node.selected && !node.hidden); void flowRef.current?.fitView({ ...(selected.length ? {nodes:selected} : {}), padding: .2, duration: 0 }) }}>Fit selection / all</button>
            <span>Drag to pin · links show membership or observed transport</span>
          </div>
        </div>
        <GraphActivityPanel activity={activity} connected={connected} onInspect={inspectRecord} />
        </div>
      </CanvasContext.Provider>
      <AgentStatusDock snapshot={agentStatus} streaming={statusStreaming} onFocus={focusStatusAgent} />
    </div>
  )
}

const inputStyle: React.CSSProperties = {
  width: '100%', background: '#0d1520', color: '#e6edf5',
  border: '1px solid #2a3a4c', borderRadius: 5, padding: '4px 7px',
  fontSize: 11, boxSizing: 'border-box', fontFamily: 'inherit',
}
