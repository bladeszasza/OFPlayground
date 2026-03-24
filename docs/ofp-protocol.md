# Open Floor Protocol (OFP) Reference

OFP Playground implements the [Open Floor Protocol](https://github.com/open-voice-interoperability/openfloor-python), a standard for multi-party AI conversations. This document covers how the playground maps OFP concepts to its internal architecture.

## OFP Envelope Structure

Every message in the system is wrapped in an OFP `Envelope`:

```python
Envelope(
    sender=Sender(
        speakerUri="tag:ofp-playground.local,2025:llm-alice",
        serviceUrl="local://llm-alice",
    ),
    conversation=Conversation(id="conv:a1b2c3d4-..."),
    events=[UtteranceEvent(dialogEvent=DialogEvent(...))],
)
```

### Envelope Fields

| Field | Description |
|-------|-------------|
| `sender.speakerUri` | URI identifying the sending agent |
| `sender.serviceUrl` | Service endpoint (local or HTTP) |
| `conversation.id` | Session-scoped conversation identifier |
| `events` | List of OFP events (utterances, floor control, etc.) |

## OFP Event Types

### UtteranceEvent

An agent speaks. Contains a `DialogEvent` with typed features.

```python
UtteranceEvent(
    dialogEvent=DialogEvent(
        speakerUri="tag:...:llm-alice",
        id="uuid",
        features={
            "text": TextFeature(tokens=[Token(value="Hello world")]),
            "image": ImageFeature(tokens=[Token(value="/path/to/image.png")]),
        },
    ),
    to=BotAgent(speakerUri="tag:...:llm-bob"),  # optional: private message
)
```

**Features** map to the `ArtifactFeature` model:

| Feature Key | Description | Example |
|------------|-------------|---------|
| `text` | Mandatory text content (verbalizable fallback) | Dialogue, instructions |
| `image` | Image file path or URL | Generated storyboard |
| `video` | Video file path | Generated clip |
| `audio` | Audio file path | Music cue |
| `3d` | 3D model reference | (reserved) |

### RequestFloorEvent

Agent requests permission to speak.

```python
RequestFloorEvent(
    dialogEvent=DialogEvent(
        speakerUri="tag:...:llm-alice",
        id="uuid",
        features={"text": TextFeature(tokens=[Token(value="responding to discussion")])},
    )
)
```

### GrantFloorEvent

FloorManager grants an agent the floor.

```python
GrantFloorEvent(
    dialogEvent=DialogEvent(
        speakerUri=FLOOR_MANAGER_URI,
        id="uuid",
        features={"text": TextFeature(tokens=[Token(value="You have the floor")])},
    ),
    to=BotAgent(speakerUri="tag:...:llm-alice"),
)
```

### RevokeFloorEvent

FloorManager revokes the floor from an agent.

```python
RevokeFloorEvent(
    dialogEvent=DialogEvent(
        speakerUri=FLOOR_MANAGER_URI,
        id="uuid",
        features={"text": TextFeature(tokens=[Token(value="Floor revoked")])},
    ),
    to=BotAgent(speakerUri="tag:...:llm-alice"),
)
```

### YieldFloorEvent

Agent voluntarily gives up the floor.

```python
YieldFloorEvent(
    dialogEvent=DialogEvent(
        speakerUri="tag:...:llm-alice",
        id="uuid",
        features={"text": TextFeature(tokens=[Token(value="")])},
    )
)
```

### PublishManifestsEvent

Agent advertises its capabilities (OFP manifest).

```python
PublishManifestsEvent(
    dialogEvent=DialogEvent(
        speakerUri="tag:...:llm-alice",
        id="uuid",
        features={"text": TextFeature(tokens=[Token(value="")])},
    ),
    manifests=[manifest],
)
```

### InviteEvent

FloorManager invites an agent to join the conversation.

```python
InviteEvent(
    dialogEvent=DialogEvent(
        speakerUri=FLOOR_MANAGER_URI,
        id="uuid",
        features={"text": TextFeature(tokens=[Token(value="")])},
    ),
    to=BotAgent(speakerUri="tag:...:llm-alice", serviceUrl="local://..."),
)
```

### UninviteEvent

FloorManager removes an agent from the conversation (used by `[KICK]`).

### DeclineEvent

Agent declines to participate (logged, no action taken).

## OFP Manifest

Every agent publishes a manifest describing its capabilities:

```python
Manifest(
    identification=Identification(
        speakerUri="tag:...:llm-alice",
        serviceUrl="local://llm-alice",
        conversationalName="Alice",
        role="Research scientist",
        synopsis="Explores topics with curiosity and rigor",
    ),
    capabilities=[
        Capability(
            keyphrases=["text-generation", "analysis"],
            descriptions=["General-purpose text generation"],
            supportedLayers=SupportedLayers(
                input=["text"],
                output=["text"],
            ),
        )
    ],
)
```

### Manifest Fields

| Field | Description |
|-------|-------------|
| `identification.speakerUri` | Agent's unique URI |
| `identification.conversationalName` | Display name |
| `identification.role` | Agent's role description |
| `identification.synopsis` | System prompt / personality |
| `capabilities[].keyphrases` | Task types the agent handles |
| `capabilities[].supportedLayers.input` | Input modalities (text, image, etc.) |
| `capabilities[].supportedLayers.output` | Output modalities |

### Capability Keyphrases by Agent Type

| Agent Type | Keyphrases |
|-----------|------------|
| Text agents (Anthropic/OpenAI/Google/HF) | `["text-generation"]` |
| Image agents (HF FLUX) | `["text-to-image", "image-generation"]` |
| Image agents (OpenAI) | `["text-to-image", "openai-image-generation"]` |
| Image agents (Gemini) | `["text-to-image", "gemini-image-generation"]` |
| Video agents | `["text-to-video", "video-generation"]` |
| Music agents | `["text-to-music", "music-generation", "lyria"]` |
| Vision agents | `["image-to-text", "vision"]` |
| WebPageAgent | `["web-page-generation"]` |
| Orchestrator agents | `["orchestration", "task-management", ...]` |

## How the Playground Uses OFP

### Envelope Routing

The `MessageBus` routes envelopes based on the `to` field:

- **Private messages**: `event.to.speakerUri` is set → deliver only to that agent + floor manager
- **Broadcasts**: No `to` field → deliver to all registered agents except sender
- **Floor manager**: Always receives a copy (except when it's the sender)

### Floor Protocol Implementation

The playground's floor control follows OFP conventions:

1. Agent calls `request_floor()` → sends `RequestFloorEvent`
2. FloorController evaluates policy → approves or queues
3. If approved: FloorManager sends `GrantFloorEvent` to agent
4. Agent generates response → calls `yield_floor()` → sends `YieldFloorEvent`
5. FloorController advances to next holder (per policy)

### Manifest Exchange

On agent startup:

1. FloorManager sends `InviteEvent` to agent
2. Agent receives invite → builds and publishes manifest via `PublishManifestsEvent`
3. FloorManager stores manifest in `_manifests` registry
4. Orchestrators use manifests for capability-aware task assignment

### Remote Agent OFP Communication

`RemoteOFPAgent` bridges HTTP OFP endpoints:

1. Send `InviteEvent` → remote responds with manifest
2. Relay manifest to local bus
3. On utterances: POST JSON to remote endpoint, capture response
4. Forward response as local `UtteranceEvent`

Remote agents follow cascade prevention rules:
- Remote-to-remote responses are blocked (prevents exponential loops)
- Floor manager messages are filtered to only `[DIRECTIVE for <name>]` patterns

## Artifact Model

`Utterance` and `ArtifactFeature` (in `models/artifact.py`) provide the typed internal representation of OFP dialog event features:

```python
@dataclass
class ArtifactFeature:
    feature_key: str    # 'text', 'image', 'video', '3d', 'audio'
    mime_type: str
    value: str | None         # inline text
    value_url: str | None     # file path or URL

@dataclass
class Utterance:
    speaker_uri: str
    speaker_name: str
    features: dict[str, ArtifactFeature]
    timestamp: float
```

Every `Utterance` has a mandatory `text` feature plus optional extended keys (`image`, `video`, `audio`, `3d`).

Factory methods:
- `Utterance.from_text(uri, name, text)` — plain text
- `Utterance.from_image(uri, name, text_desc, image_path)` — image + text fallback
- `Utterance.from_video(uri, name, text_desc, video_path)` — video + text fallback
