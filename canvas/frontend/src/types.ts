// canvas/frontend/src/types.ts

export type FloorPolicy =
  | 'SEQUENTIAL'
  | 'ROUND_ROBIN'
  | 'MODERATED'
  | 'FREE_FOR_ALL'
  | 'SHOWRUNNER_DRIVEN'

export type AgentProvider = 'anthropic' | 'openai' | 'google' | 'huggingface' | 'remote' | 'demo'

export type SessionState = 'idle' | 'running' | 'stopped'

export type AgentFloorState =
  | 'speaking' | 'waiting' | 'requesting' | 'error' | 'spawning'
  | 'yielding'   // transient ~1.5s: agent voluntarily yielded
  | 'manifest'   // transient ~1.5s: agent published capabilities

export type ArtifactKind = 'image' | 'code' | 'phase' | 'music'

export interface Message {
  id: string
  sender: string
  text: string
  ts: number
  media?: { type: string; url: string } | null
}

export interface FloorEventEntry {
  id: string
  type: 'floor_grant' | 'floor_revoke' | 'floor_yield' | 'floor_request'
       | 'utterance' | 'manifest_published'
  agent: string
  to?: string
  preview?: string
  ts: number
  raw?: Record<string, unknown>   // full WSEvent payload for inspector
  cursor?: number
  replay?: boolean
}

export interface EventInspectorNodeData {
  event: FloorEventEntry | null
}

export interface FloorNodeData {
  policy: FloorPolicy
  topic: string
  maxTurns: number | null
  mot: number             // minimum observation time between events in ms (0 = no delay)
  showFloorEvents: boolean
  noHuman: boolean
  humanName: string
  sessionState: SessionState
  sessionId: string | null
  turnCount: number
  elapsedSecs: number
}

export interface AgentNodeData {
  pinned?: boolean
  replaying?: boolean
  instanceId?: string
  speakerUri?: string
  asp?: { lifecycle: string; health: string; governance: string; stale: boolean; provenance: string }
  provider: AgentProvider
  name: string
  model: string
  systemPrompt: string
  slug: string
  agentType: string          // maps to TASK_SUBTYPES, '' = text-generation
  remoteTarget: string       // slug or URL for remote agents
  floorState: AgentFloorState
  messages: Message[]
  errorMessage?: string
}

export interface HumanNodeData {
  instanceId?: string
  speakerUri?: string
  asp?: AgentNodeData['asp']
  humanName: string
  messages: Message[]
}

export interface ConversationNodeData {
  selectedCursor?: number
  sessionId: string | null
  policy: string
  topic: string
  turnCount: number
  eventLog: FloorEventEntry[]
}

export interface ConversationChatNodeData {
  messages: Message[]
}

export interface ArtifactNodeData {
  kind: ArtifactKind
  agentName: string
  slug?: string
  label: string        // filename or slug
  preview: string      // first ~120 chars, or image URL
  url?: string         // direct URL for images / audio
}

export interface GlowEdgeData {
  active: boolean      // true while the target agent holds the floor
  color: string        // stroke color
  dashed: boolean      // true for artifact edges
  pulse?: 'amber' | 'teal' | 'purple' | null
  layer?: 'membership' | 'flow' | 'artifact'
  label?: string
  observedAt?: number
  failed?: boolean
}

export interface ArtifactCardData {
  id: string
  kind: 'markdown' | 'image' | 'memory'
  agent: string | null
  slug?: string
  preview: string
  url?: string
  category?: string
  content?: string
  x: number
  y: number
}

// ── WebSocket event union ─────────────────────────────────────────────────────

export type WSEvent = { cursor?: number; replay?: boolean; ts?: string | number;
  conversation_id?: string; scope_id?: string; scope_kind?: string; parent_conversation_id?: string;
  delivery_outcomes?: Record<string, string> } & (
  | { type: 'floor_snapshot'; policy: string; holder: string | null; holder_name?: string | null; turn_count?: number;
      queue: string[]; queue_names?: string[]; participants: { speaker_uri: string; name: string }[];
      restricted?: string[]; holders?: string[] }
  | { type: 'utterance'; sender: string; sender_uri: string; text: string; media: { type: string; url: string } | null; envelope_json?: Record<string, unknown> }
  | { type: 'floor_grant'; to: string; to_uri?: string; envelope_json?: Record<string, unknown> }
  | { type: 'floor_revoke'; from: string; from_uri?: string; envelope_json?: Record<string, unknown> }
  | { type: 'floor_request'; from: string; from_uri?: string; envelope_json?: Record<string, unknown> }
  | { type: 'floor_yield'; from: string; from_uri?: string; envelope_json?: Record<string, unknown> }
  | { type: 'manifest_published'; agent: string; agent_uri?: string; manifests?: Record<string, unknown>[]; envelope_json?: Record<string, unknown> }
  | { type: 'artifact_saved'; slug: string; kind: string; agent: string; preview: string }
  | { type: 'image_saved'; agent: string; url: string }
  | { type: 'memory_saved'; category: string; content: string; agent: string | null }
  | { type: 'agent_joined'; name: string; provider: string; instance_id?: string; speaker_uri?: string }
  | { type: 'agent_kicked'; name: string }
  | { type: 'agent_spawning'; name: string; provider: string }
  | { type: 'error'; agent: string; message: string; retryable: boolean }
  | { type: 'session_ended' }
  | { type: 'replay_gap'; oldest_cursor: number }
)

// ── Agent library entry ───────────────────────────────────────────────────────

export interface SlugEntry {
  slug: string
  display_name: string
  category: string
}
