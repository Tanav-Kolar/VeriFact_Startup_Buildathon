import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Graph from 'graphology'
import Sigma from 'sigma'
import type { FrameData, GraphEdgeData, GraphNodeData, StorylineRow } from '../types/api'
import { formatHourOffset, formatPlatformLabel, formatT0ShortIst, formatUtcTimestamp } from '../utils/format'
import {
  RESPONSE_GOLD_COLOUR,
  getStorylineColour,
  hashString,
  hexToRgba
} from '../utils/palette'

export interface GraphViewSettings {
  nodeSizeScale: number
  showLinks: boolean
  linkOpacity: number
  showLabels: boolean
  enabledPlatforms: string[]
}

export interface NebulaGraphProps {
  t0Utc: string | null
  frames: FrameData[]
  storylines: StorylineRow[]
  enabledSources: string[]
  currentHour: number
  focusedStorylineId: string | null
  selectedNodeId: string | null
  onSelectNode: (node: GraphNodeData | null) => void
  onFocusStoryline?: (storylineId: string) => void
  onJumpToHour?: (hour: number) => void
}

interface PositionedHub {
  id: string
  storylineId: string
  name: string
  count: number
  hx: number
  hy: number
  size: number
  colour: string
  dimmed: boolean
}

interface PositionedNode {
  data: GraphNodeData
  storylineId: string
  x: number
  y: number
  size: number
  baseColour: string
  dimmed: boolean
  isRecent: boolean
  isNewArrival: boolean
}

interface SpokeEdgeMeta {
  key: string
  hubId: string
  nodeId: string
  storylineId: string
  colour: string
  pull: number
}

interface SimEdgeMeta {
  key: string
  source: string
  target: string
  storylineId: string
  colour: string
  dimmed: boolean
}

interface OverlayHub {
  id: string
  storylineId: string
  name: string
  count: number
  vx: number
  vy: number
  hy: number
  size: number
  colour: string
  dimmed: boolean
  isFocused: boolean
  isHovered: boolean
}

interface OverlayPoint {
  id: string
  vx: number
  vy: number
  size: number
  isOfficial: boolean
  isResponse: boolean
  hasConflict: boolean
  isSelected: boolean
  isHovered: boolean
  isRecent: boolean
  isNewArrival: boolean
  dimmed: boolean
}

interface OverlayEdge {
  key: string
  x1: number
  y1: number
  x2: number
  y2: number
  kind: 'links_to' | 'contradicts'
  dimmed: boolean
  highlighted: boolean
}

const HUB_PREFIX = '__hub__:'
const CLUSTER_MIN_ITEMS = 3
const CONSTELLATION_RADIUS = 18
const UNASSIGNED_HUB_POS = { hx: 0, hy: -3.5 }
const SUB_NODE_MIN_SIZE = 3.0
const SUB_NODE_MAX_SIZE = 6.5
const HUB_MAX_SIZE = 24
const SUB_NODE_MIN_DIST = 2.6
const SUB_NODE_MAX_DIST = 6.2
const MAX_RECENT_GLOW_OVERLAY = 240
const TRANSITION_DURATION_MS = 360
const GRAPH_BBOX = { x: [-26, 26] as [number, number], y: [-26, 26] as [number, number] }

function computeHubPosition(
  storylineId: string | null,
  storylineOrder: Map<string, number>
): { hx: number; hy: number } {
  if (!storylineId || storylineId === 'unassigned') {
    return UNASSIGNED_HUB_POS
  }
  const total = Math.max(1, storylineOrder.size)
  const index = storylineOrder.get(storylineId) ?? hashString(storylineId) % total
  const phaseOffset = total % 4 === 0 ? Math.PI / total : 0
  const angle = (index / total) * Math.PI * 2 + phaseOffset
  return {
    hx: Math.cos(angle) * CONSTELLATION_RADIUS,
    hy: Math.sin(angle) * CONSTELLATION_RADIUS
  }
}

function computeScatterCoords(node: GraphNodeData): { x: number; y: number } {
  const h1 = hashString(`${node.id}:sx`)
  const h2 = hashString(`${node.id}:sy`)
  const angle = ((h1 % 10000) / 10000) * Math.PI * 2
  const radius = 3 + Math.sqrt((h2 % 10000) / 10000) * 18
  return {
    x: Math.cos(angle) * radius,
    y: Math.sin(angle) * radius
  }
}

function computeRootSubNodeCoords(
  node: GraphNodeData,
  hx: number,
  hy: number
): { x: number; y: number } {
  const h1 = hashString(node.id)
  const h2 = hashString(`${node.id}:r`)
  const platformBias = ((hashString(node.platform) % 360) * Math.PI) / 180
  const localAngle = platformBias + (((h1 % 1000) / 1000) - 0.5) * Math.PI * 1.6
  const dist =
    SUB_NODE_MIN_DIST + ((h2 % 1000) / 1000) * (SUB_NODE_MAX_DIST - SUB_NODE_MIN_DIST)
  return {
    x: hx + Math.cos(localAngle) * dist,
    y: hy + Math.sin(localAngle) * dist
  }
}

function computeReplySubNodeCoords(
  node: GraphNodeData,
  parentPos: { x: number; y: number },
  hx: number,
  hy: number
): { x: number; y: number } {
  const dx = parentPos.x - hx
  const dy = parentPos.y - hy
  const parentAngle = Math.atan2(dy, dx)
  const parentDist = Math.hypot(dx, dy)
  const h1 = hashString(node.id)
  const h2 = hashString(`${node.id}:r`)
  const angleJitter = (((h1 % 1000) / 1000) - 0.5) * 0.52
  const distStep = 0.45 + ((h2 % 1000) / 1000) * 0.85
  const dist = Math.max(
    SUB_NODE_MIN_DIST,
    Math.min(SUB_NODE_MAX_DIST, parentDist + distStep)
  )
  const angle = parentAngle + angleJitter
  return {
    x: hx + Math.cos(angle) * dist,
    y: hy + Math.sin(angle) * dist
  }
}

function computeNodeBaseSize(reach: number | null, scale: number): number {
  const safeReach = typeof reach === 'number' && reach > 0 ? reach : 1
  const raw = 3.0 + Math.min(3.5, Math.log10(safeReach + 1) * 0.52)
  return Math.max(SUB_NODE_MIN_SIZE, Math.min(SUB_NODE_MAX_SIZE, raw * scale))
}

function computeHubSize(count: number): number {
  const raw = 7.5 + Math.log2(Math.max(2, count - 1)) * 3.8
  return Math.min(HUB_MAX_SIZE, raw)
}

function computeCoalescencePull(count: number): number {
  return Math.min(1, 0.4 + (count - CLUSTER_MIN_ITEMS) * 0.15)
}

function formatHubPillLabel(name: string, count: number): string {
  const suffix = ` · ${count}`
  const maxTotal = 32
  const maxNameLen = Math.max(12, maxTotal - suffix.length)
  const clean = name.trim() || 'Storyline'
  const truncated =
    clean.length > maxNameLen ? `${clean.slice(0, maxNameLen - 1).trimEnd()}…` : clean
  return `${truncated}${suffix}`
}

export function NebulaGraph({
  t0Utc,
  frames,
  storylines,
  enabledSources,
  currentHour,
  focusedStorylineId,
  selectedNodeId,
  onSelectNode,
  onFocusStoryline,
  onJumpToHour
}: NebulaGraphProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const graphRef = useRef<Graph | null>(null)
  const sigmaRef = useRef<Sigma | null>(null)
  const hadNodesRef = useRef(false)
  const rafIdRef = useRef<number | null>(null)

  const [settingsOpen, setSettingsOpen] = useState(false)
  const [hoveredNode, setHoveredNode] = useState<GraphNodeData | null>(null)
  const [hoveredHub, setHoveredHub] = useState<OverlayHub | null>(null)
  const [overlayHubs, setOverlayHubs] = useState<OverlayHub[]>([])
  const [overlayPoints, setOverlayPoints] = useState<OverlayPoint[]>([])
  const [overlayEdges, setOverlayEdges] = useState<OverlayEdge[]>([])

  const [settings, setSettings] = useState<GraphViewSettings>({
    nodeSizeScale: 1,
    showLinks: true,
    linkOpacity: 0.2,
    showLabels: false,
    enabledPlatforms: []
  })

  // Synchronize default platform filter list whenever event sources or frames load (include 'web' official snapshots)
  const availablePlatforms = useMemo(() => {
    const set = new Set<string>(enabledSources.map((s) => s.toLowerCase()))
    for (const f of frames) {
      for (const n of f.nodes_added) {
        if (n.platform) set.add(n.platform.toLowerCase())
      }
    }
    return Array.from(set)
  }, [enabledSources, frames])

  useEffect(() => {
    if (availablePlatforms.length > 0) {
      setSettings((prev) =>
        prev.enabledPlatforms.length === 0
          ? { ...prev, enabledPlatforms: availablePlatforms }
          : prev
      )
    }
  }, [availablePlatforms])

  const { storylineColourIndexMap, storylineNameMap, storylineOrderMap } = useMemo(() => {
    const colourMap = new Map<string, number>()
    const nameMap = new Map<string, string>()
    const orderMap = new Map<string, number>()

    const nonUnassigned: Array<{ id: string; colourIndex: number }> = []
    const seen = new Set<string>()

    storylines.forEach((s, idx) => {
      const colourIndex = typeof s.colour_index === 'number' ? s.colour_index : idx
      colourMap.set(s.id, colourIndex)
      nameMap.set(s.id, s.name)
      if (s.id !== 'unassigned' && !seen.has(s.id)) {
        seen.add(s.id)
        nonUnassigned.push({ id: s.id, colourIndex })
      }
    })

    for (const frame of frames) {
      for (const n of frame.nodes_added) {
        const sid = n.storyline_id ?? 'unassigned'
        if (sid !== 'unassigned' && !seen.has(sid)) {
          seen.add(sid)
          nonUnassigned.push({ id: sid, colourIndex: colourMap.get(sid) ?? 999 })
        }
      }
    }

    nonUnassigned.sort((a, b) => a.colourIndex - b.colourIndex || a.id.localeCompare(b.id))
    nonUnassigned.forEach((entry, index) => {
      orderMap.set(entry.id, index)
    })

    if (!nameMap.has('unassigned')) {
      nameMap.set('unassigned', 'Unassigned')
    }

    return {
      storylineColourIndexMap: colourMap,
      storylineNameMap: nameMap,
      storylineOrderMap: orderMap
    }
  }, [storylines, frames])

  // Accumulate emergent hubs (count >= 3), sub-nodes, similarity bridges (count === 2), and spokes up to currentHour
  const {
    visibleHubs,
    visibleNodes,
    visibleEdges,
    spokeEdges,
    simEdges,
    emergingStorylineCount,
    hubMap,
    nodeMap,
    spokeEdgeMap,
    simEdgeMap
  } = useMemo(() => {
    const allowedPlatforms =
      settings.enabledPlatforms.length > 0
        ? new Set(settings.enabledPlatforms.map((p) => p.toLowerCase()))
        : null

    const rawNodesById = new Map<string, GraphNodeData>()
    const rawEdges: GraphEdgeData[] = []

    const sortedFrames = [...frames]
      .filter((f) => f.hour <= currentHour)
      .sort((a, b) => a.hour - b.hour)

    for (const frame of sortedFrames) {
      for (const n of frame.nodes_added) {
        if (n.hour > currentHour) continue
        if (allowedPlatforms && !allowedPlatforms.has(n.platform.toLowerCase())) continue
        rawNodesById.set(n.id, n)
      }

      for (const e of frame.edges_added) {
        if (e.hour <= currentHour) {
          rawEdges.push(e)
        }
      }
    }

    // Group visible sub-nodes by storylineId
    const groupedByStoryline = new Map<string, GraphNodeData[]>()
    for (const n of rawNodesById.values()) {
      const sid = n.storyline_id ?? 'unassigned'
      const bucket = groupedByStoryline.get(sid)
      if (bucket) {
        bucket.push(n)
      } else {
        groupedByStoryline.set(sid, [n])
      }
    }

    // Build parent reply/quote lookup within the same storyline so sub-threads stay tightly fanned
    const parentByChildId = new Map<string, string>()
    for (const e of rawEdges) {
      if (e.type !== 'replied_to' && e.type !== 'quoted') continue
      if (e.source === e.target || parentByChildId.has(e.source)) continue
      const srcNode = rawNodesById.get(e.source)
      const tgtNode = rawNodesById.get(e.target)
      if (!srcNode || !tgtNode) continue
      const srcSid = srcNode.storyline_id ?? 'unassigned'
      const tgtSid = tgtNode.storyline_id ?? 'unassigned'
      if (srcSid === tgtSid) {
        parentByChildId.set(e.source, e.target)
      }
    }

    const hubsById = new Map<string, PositionedHub>()
    const nodesById = new Map<string, PositionedNode>()
    const spokes: SpokeEdgeMeta[] = []
    const spokesByKey = new Map<string, SpokeEdgeMeta>()
    const sims: SimEdgeMeta[] = []
    const simsByKey = new Map<string, SimEdgeMeta>()
    let emergingCount = 0

    for (const [storylineId, items] of groupedByStoryline.entries()) {
      if (items.length === 0) continue

      const storylineColour = getStorylineColour(
        storylineId,
        storylineColourIndexMap.get(storylineId)
      )
      const dimmed = focusedStorylineId !== null && focusedStorylineId !== storylineId
      const isClustered = items.length >= CLUSTER_MIN_ITEMS

      if (!isClustered) {
        emergingCount += 1

        // Pre-cluster stage (count < 3): scatter small data points across the field
        for (const n of items) {
          const scatter = computeScatterCoords(n)
          const isRecent = currentHour - n.hour >= 0 && currentHour - n.hour < 2
          const isNewArrival = n.hour === currentHour
          const baseColour = n.is_response ? RESPONSE_GOLD_COLOUR : storylineColour

          nodesById.set(n.id, {
            data: n,
            storylineId,
            x: scatter.x,
            y: scatter.y,
            size: computeNodeBaseSize(n.reach, settings.nodeSizeScale),
            baseColour,
            dimmed,
            isRecent,
            isNewArrival
          })
        }

        // At count === 2, surface a similarity bridge edge before the hub condenses
        if (items.length === 2) {
          const sortedPair = [...items].sort((a, b) => a.id.localeCompare(b.id))
          const simKey = `__sim__:${sortedPair[0].id}:${sortedPair[1].id}`
          const simMeta: SimEdgeMeta = {
            key: simKey,
            source: sortedPair[0].id,
            target: sortedPair[1].id,
            storylineId,
            colour: storylineColour,
            dimmed
          }
          sims.push(simMeta)
          simsByKey.set(simKey, simMeta)
        }
        continue
      }

      // Emergent cluster stage (count >= 3): pull items from scatter field toward hub orbit
      const hubId = `${HUB_PREFIX}${storylineId}`
      const { hx, hy } = computeHubPosition(storylineId, storylineOrderMap)
      const pull = computeCoalescencePull(items.length)

      hubsById.set(hubId, {
        id: hubId,
        storylineId,
        name: storylineNameMap.get(storylineId) ?? storylineId,
        count: items.length,
        hx,
        hy,
        size: computeHubSize(items.length),
        colour: storylineColour,
        dimmed
      })

      // Memoized cluster orbit positioning keeping replies clustered near their parent post
      const resolvedClusterCoords = new Map<string, { x: number; y: number }>()
      const resolveClusterCoords = (
        nodeId: string,
        visiting: Set<string>,
        depth: number
      ): { x: number; y: number } => {
        const cached = resolvedClusterCoords.get(nodeId)
        if (cached) return cached

        const node = rawNodesById.get(nodeId)
        if (!node) return { x: hx, y: hy }

        const parentId = parentByChildId.get(nodeId)
        if (parentId && depth < 6 && !visiting.has(parentId)) {
          visiting.add(nodeId)
          const parentPos = resolveClusterCoords(parentId, visiting, depth + 1)
          const coords = computeReplySubNodeCoords(node, parentPos, hx, hy)
          resolvedClusterCoords.set(nodeId, coords)
          return coords
        }

        const rootCoords = computeRootSubNodeCoords(node, hx, hy)
        resolvedClusterCoords.set(nodeId, rootCoords)
        return rootCoords
      }

      for (const n of items) {
        const scatter = computeScatterCoords(n)
        const cluster = resolveClusterCoords(n.id, new Set<string>(), 0)
        const targetX = scatter.x + (cluster.x - scatter.x) * pull
        const targetY = scatter.y + (cluster.y - scatter.y) * pull

        const isRecent = currentHour - n.hour >= 0 && currentHour - n.hour < 2
        const isNewArrival = n.hour === currentHour
        const baseColour = n.is_response ? RESPONSE_GOLD_COLOUR : storylineColour

        nodesById.set(n.id, {
          data: n,
          storylineId,
          x: targetX,
          y: targetY,
          size: computeNodeBaseSize(n.reach, settings.nodeSizeScale),
          baseColour,
          dimmed,
          isRecent,
          isNewArrival
        })

        const spokeKey = `spoke:${storylineId}:${n.id}`
        const spokeMeta: SpokeEdgeMeta = {
          key: spokeKey,
          hubId,
          nodeId: n.id,
          storylineId,
          colour: storylineColour,
          pull
        }
        spokes.push(spokeMeta)
        spokesByKey.set(spokeKey, spokeMeta)
      }
    }

    const filteredEdges = rawEdges.filter(
      (e) => nodesById.has(e.source) && nodesById.has(e.target) && e.source !== e.target
    )

    return {
      visibleHubs: Array.from(hubsById.values()),
      visibleNodes: Array.from(nodesById.values()),
      visibleEdges: filteredEdges,
      spokeEdges: spokes,
      simEdges: sims,
      emergingStorylineCount: emergingCount,
      hubMap: hubsById,
      nodeMap: nodesById,
      spokeEdgeMap: spokesByKey,
      simEdgeMap: simsByKey
    }
  }, [
    frames,
    currentHour,
    focusedStorylineId,
    settings.enabledPlatforms,
    settings.nodeSizeScale,
    storylineColourIndexMap,
    storylineNameMap,
    storylineOrderMap
  ])

  // Keep latest state in refs for persistent Sigma reducers and event handlers
  const hubMapRef = useRef(hubMap)
  hubMapRef.current = hubMap
  const nodeMapRef = useRef(nodeMap)
  nodeMapRef.current = nodeMap
  const visibleEdgesRef = useRef(visibleEdges)
  visibleEdgesRef.current = visibleEdges
  const spokeEdgeMapRef = useRef(spokeEdgeMap)
  spokeEdgeMapRef.current = spokeEdgeMap
  const simEdgeMapRef = useRef(simEdgeMap)
  simEdgeMapRef.current = simEdgeMap
  const focusedStorylineIdRef = useRef(focusedStorylineId)
  focusedStorylineIdRef.current = focusedStorylineId
  const selectedNodeIdRef = useRef(selectedNodeId)
  selectedNodeIdRef.current = selectedNodeId
  const hoveredNodeIdRef = useRef<string | null>(null)
  hoveredNodeIdRef.current = hoveredHub?.id ?? hoveredNode?.id ?? null
  const currentHourRef = useRef(currentHour)
  currentHourRef.current = currentHour
  const settingsRef = useRef(settings)
  settingsRef.current = settings
  const onSelectNodeRef = useRef(onSelectNode)
  onSelectNodeRef.current = onSelectNode
  const onFocusStorylineRef = useRef(onFocusStoryline)
  onFocusStorylineRef.current = onFocusStoryline

  const triggerHubFocus = useCallback((storylineId: string) => {
    if (onFocusStorylineRef.current) {
      onFocusStorylineRef.current(storylineId)
      return
    }
    onSelectNodeRef.current({
      id: storylineId,
      type: 'storyline',
      platform: 'storyline',
      kind: 'storyline',
      created_at: '',
      hour: currentHourRef.current,
      storyline_id: storylineId,
      reach: null,
      is_official: false,
      self_reported: false,
      conflict: null,
      lang: null,
      label: storylineId
    })
  }, [])

  const syncOverlay = useCallback(() => {
    const sigma = sigmaRef.current
    const graph = graphRef.current
    if (!sigma || !graph) return

    const currentSelected = selectedNodeIdRef.current
    const currentHovered = hoveredNodeIdRef.current
    const currentFocused = focusedStorylineIdRef.current

    const getLivePos = (id: string, fallbackX: number, fallbackY: number) => {
      if (graph.hasNode(id)) {
        const gx = graph.getNodeAttribute(id, 'x')
        const gy = graph.getNodeAttribute(id, 'y')
        if (typeof gx === 'number' && typeof gy === 'number') {
          return { x: gx, y: gy }
        }
      }
      return { x: fallbackX, y: fallbackY }
    }

    const hubs: OverlayHub[] = []
    for (const hub of hubMapRef.current.values()) {
      const livePos = getLivePos(hub.id, hub.hx, hub.hy)
      const liveSize = graph.hasNode(hub.id)
        ? Number(graph.getNodeAttribute(hub.id, 'size') ?? hub.size)
        : hub.size
      const vp = sigma.graphToViewport(livePos)
      const renderedSize = Math.max(8, sigma.scaleSize(liveSize))
      hubs.push({
        id: hub.id,
        storylineId: hub.storylineId,
        name: hub.name,
        count: hub.count,
        vx: vp.x,
        vy: vp.y,
        hy: livePos.y,
        size: renderedSize,
        colour: hub.colour,
        dimmed: hub.dimmed,
        isFocused: currentFocused === hub.storylineId,
        isHovered: currentHovered === hub.id
      })
    }

    const points: OverlayPoint[] = []
    let recentGlowCount = 0

    for (const item of nodeMapRef.current.values()) {
      const isSelected = item.data.id === currentSelected
      const isHovered = item.data.id === currentHovered
      const includeRecentGlow =
        item.isRecent && !item.dimmed && recentGlowCount < MAX_RECENT_GLOW_OVERLAY
      const shouldDrawSpecial =
        item.data.is_official ||
        Boolean(item.data.is_response) ||
        item.data.conflict !== null ||
        isSelected ||
        isHovered ||
        item.isNewArrival ||
        includeRecentGlow

      if (!shouldDrawSpecial) continue
      if (includeRecentGlow) recentGlowCount += 1

      const livePos = getLivePos(item.data.id, item.x, item.y)
      const vp = sigma.graphToViewport(livePos)
      const renderedSize = Math.max(2.5, sigma.scaleSize(item.size))
      points.push({
        id: item.data.id,
        vx: vp.x,
        vy: vp.y,
        size: renderedSize,
        isOfficial: item.data.is_official,
        isResponse: Boolean(item.data.is_response),
        hasConflict: item.data.conflict !== null,
        isSelected,
        isHovered,
        isRecent: includeRecentGlow,
        isNewArrival: item.isNewArrival,
        dimmed: item.dimmed
      })
    }

    const specialEdges: OverlayEdge[] = []
    if (settingsRef.current.showLinks) {
      visibleEdgesRef.current.forEach((edge, idx) => {
        if (edge.type !== 'links_to' && edge.type !== 'contradicts') return
        const src = nodeMapRef.current.get(edge.source)
        const dst = nodeMapRef.current.get(edge.target)
        if (!src || !dst) return
        const p1 = sigma.graphToViewport(getLivePos(edge.source, src.x, src.y))
        const p2 = sigma.graphToViewport(getLivePos(edge.target, dst.x, dst.y))
        const highlighted =
          currentHovered !== null &&
          (edge.source === currentHovered || edge.target === currentHovered)
        specialEdges.push({
          key: `ov-${idx}-${edge.source}-${edge.target}`,
          x1: p1.x,
          y1: p1.y,
          x2: p2.x,
          y2: p2.y,
          kind: edge.type,
          dimmed: src.dimmed || dst.dimmed,
          highlighted
        })
      })
    }

    setOverlayHubs(hubs)
    setOverlayPoints(points)
    setOverlayEdges(specialEdges)
  }, [])

  // Mount persistent Graph + Sigma instance once (eliminates WebGL thrash & camera reset during replay)
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const graph = new Graph({ multi: true, allowSelfLoops: false })
    graphRef.current = graph

    const sigma = new Sigma(graph, container, {
      renderLabels: settingsRef.current.showLabels,
      labelColor: { color: '#cbd5e1' },
      labelDensity: 0.5,
      labelSize: 11,
      defaultNodeColor: '#64748b',
      defaultEdgeColor: 'rgba(148, 163, 184, 0.20)',
      allowInvalidContainer: true,
      zIndex: true,
      nodeReducer: (node, data) => {
        const hub = hubMapRef.current.get(node)
        const hoveredId = hoveredNodeIdRef.current
        if (hub) {
          const isHovered = hoveredId === node
          const isFocused = focusedStorylineIdRef.current === hub.storylineId
          const hoveredSub = hoveredId ? nodeMapRef.current.get(hoveredId) : undefined
          const sameStorylineAsHovered =
            hoveredSub !== undefined && hoveredSub.storylineId === hub.storylineId

          const liveSize = typeof data.size === 'number' ? data.size : hub.size
          let color = hub.colour
          let size = liveSize
          let zIndex = isFocused ? 4 : 3

          if (hub.dimmed) {
            color = hexToRgba(hub.colour, 0.15)
            zIndex = 0
          } else if (hoveredId !== null && !isHovered && !sameStorylineAsHovered) {
            color = hexToRgba(hub.colour, 0.28)
            zIndex = 0
          }

          if (isHovered) {
            size = liveSize * 1.1
            zIndex = 5
          }

          return {
            ...data,
            color,
            size,
            zIndex,
            label: '',
            highlighted: false
          }
        }

        const meta = nodeMapRef.current.get(node)
        if (!meta) return data

        const selectedId = selectedNodeIdRef.current
        const isHovered = hoveredId === node
        const isSelected = selectedId === node
        const isNeighbor =
          hoveredId !== null && graph.hasNode(hoveredId) && graph.areNeighbors(hoveredId, node)

        let color = meta.baseColour
        let size = meta.size
        let zIndex = meta.isRecent ? 2 : 1

        if (meta.dimmed) {
          color = hexToRgba(meta.baseColour, 0.12)
          zIndex = 0
        } else if (hoveredId !== null && !isHovered && !isNeighbor) {
          color = hexToRgba(meta.baseColour, 0.28)
          zIndex = 0
        }

        if (meta.data.is_response && !meta.dimmed) {
          zIndex = 3
        }
        if (isHovered || isSelected) {
          size = meta.size * 1.25
          zIndex = 4
        }

        return {
          ...data,
          color,
          size,
          zIndex,
          highlighted: isHovered || isSelected
        }
      },
      edgeReducer: (edge, data) => {
        if (!settingsRef.current.showLinks) {
          return { ...data, hidden: true }
        }
        const srcId = graph.source(edge)
        const dstId = graph.target(edge)
        const hoveredId = hoveredNodeIdRef.current
        const isIncidentHover = hoveredId !== null && (srcId === hoveredId || dstId === hoveredId)

        const simMeta = simEdgeMapRef.current.get(edge)
        if (simMeta) {
          if (simMeta.dimmed) {
            return {
              ...data,
              hidden: false,
              size: 0.6,
              color: hexToRgba(simMeta.colour, 0.06),
              zIndex: 0
            }
          }
          if (isIncidentHover) {
            return {
              ...data,
              hidden: false,
              size: 1.4,
              color: hexToRgba(simMeta.colour, 0.72),
              zIndex: 2
            }
          }
          return {
            ...data,
            hidden: false,
            size: 1.0,
            color: hexToRgba(simMeta.colour, 0.4),
            zIndex: 1
          }
        }

        const spokeMeta = spokeEdgeMapRef.current.get(edge)
        if (spokeMeta) {
          const hubNode = hubMapRef.current.get(spokeMeta.hubId)
          const subNode = nodeMapRef.current.get(spokeMeta.nodeId)
          const isDimmed = Boolean(hubNode?.dimmed || subNode?.dimmed)

          if (isDimmed) {
            return {
              ...data,
              hidden: false,
              size: 0.4,
              color: hexToRgba(spokeMeta.colour, 0.03),
              zIndex: 0
            }
          }
          if (isIncidentHover) {
            return {
              ...data,
              hidden: false,
              size: 1.15,
              color: hexToRgba(spokeMeta.colour, 0.6),
              zIndex: 2
            }
          }
          if (hoveredId !== null) {
            return {
              ...data,
              hidden: false,
              size: 0.45,
              color: hexToRgba(spokeMeta.colour, 0.05),
              zIndex: 0
            }
          }
          const baseSpokeAlpha = (0.08 + 0.12 * spokeMeta.pull) *
            (focusedStorylineIdRef.current === spokeMeta.storylineId ? 1.35 : 1)
          return {
            ...data,
            hidden: false,
            size: 0.45 + 0.3 * spokeMeta.pull,
            color: hexToRgba(spokeMeta.colour, Math.min(0.28, baseSpokeAlpha)),
            zIndex: 1
          }
        }

        const srcNode = nodeMapRef.current.get(srcId)
        const dstNode = nodeMapRef.current.get(dstId)
        const isDimmed = Boolean(srcNode?.dimmed || dstNode?.dimmed)
        const isFocusedClusterEdge =
          focusedStorylineIdRef.current !== null && !isDimmed

        if (isDimmed) {
          return {
            ...data,
            hidden: false,
            size: 0.5,
            color: 'rgba(148, 163, 184, 0.05)',
            zIndex: 0
          }
        }
        if (isIncidentHover || isFocusedClusterEdge) {
          return {
            ...data,
            hidden: false,
            size: isIncidentHover ? 1.25 : 0.85,
            color: isIncidentHover
              ? 'rgba(203, 213, 225, 0.55)'
              : 'rgba(203, 213, 225, 0.35)',
            zIndex: 2
          }
        }
        const calmAlpha =
          hoveredId !== null
            ? Math.min(0.1, settingsRef.current.linkOpacity)
            : Math.min(0.22, settingsRef.current.linkOpacity)
        return {
          ...data,
          hidden: false,
          size: 0.7,
          color: `rgba(148, 163, 184, ${calmAlpha.toFixed(2)})`,
          zIndex: 1
        }
      }
    })

    sigma.setCustomBBox(GRAPH_BBOX)
    sigmaRef.current = sigma

    sigma.on('afterRender', syncOverlay)
    sigma.on('clickNode', ({ node }) => {
      const hub = hubMapRef.current.get(node)
      if (hub) {
        triggerHubFocus(hub.storylineId)
        return
      }
      const found = nodeMapRef.current.get(node)
      onSelectNodeRef.current(found ? found.data : null)
    })
    sigma.on('clickStage', () => {
      onSelectNodeRef.current(null)
    })
    sigma.on('enterNode', ({ node }) => {
      const hub = hubMapRef.current.get(node)
      if (hub) {
        const vp = sigma.graphToViewport({ x: hub.hx, y: hub.hy })
        setHoveredNode(null)
        setHoveredHub({
          id: hub.id,
          storylineId: hub.storylineId,
          name: hub.name,
          count: hub.count,
          vx: vp.x,
          vy: vp.y,
          hy: hub.hy,
          size: sigma.scaleSize(hub.size),
          colour: hub.colour,
          dimmed: hub.dimmed,
          isFocused: focusedStorylineIdRef.current === hub.storylineId,
          isHovered: true
        })
        container.style.cursor = 'pointer'
        return
      }
      const found = nodeMapRef.current.get(node)
      setHoveredHub(null)
      setHoveredNode(found ? found.data : null)
      container.style.cursor = 'pointer'
    })
    sigma.on('leaveNode', () => {
      setHoveredHub(null)
      setHoveredNode(null)
      container.style.cursor = 'grab'
    })

    return () => {
      if (rafIdRef.current !== null) {
        window.cancelAnimationFrame(rafIdRef.current)
        rafIdRef.current = null
      }
      sigma.kill()
      sigmaRef.current = null
      graphRef.current = null
    }
  }, [syncOverlay, triggerHubFocus])

  // Incrementally sync Graphology nodes & edges and smoothly animate coalescence transitions
  useEffect(() => {
    const graph = graphRef.current
    const sigma = sigmaRef.current
    if (!graph || !sigma) return

    if (rafIdRef.current !== null) {
      window.cancelAnimationFrame(rafIdRef.current)
      rafIdRef.current = null
    }

    const desiredNodeIds = new Set<string>([
      ...visibleHubs.map((h) => h.id),
      ...visibleNodes.map((n) => n.data.id)
    ])

    for (const existingId of graph.nodes()) {
      if (!desiredNodeIds.has(existingId)) {
        graph.dropNode(existingId)
      }
    }

    const hubAnimations: Array<{ id: string; fromSize: number; toSize: number }> = []
    for (const hub of visibleHubs) {
      if (graph.hasNode(hub.id)) {
        const prevSize = Number(graph.getNodeAttribute(hub.id, 'size') ?? hub.size)
        graph.setNodeAttribute(hub.id, 'x', hub.hx)
        graph.setNodeAttribute(hub.id, 'y', hub.hy)
        graph.setNodeAttribute(hub.id, 'color', hub.colour)
        graph.setNodeAttribute(hub.id, 'label', '')
        if (Math.abs(prevSize - hub.size) > 0.1) {
          hubAnimations.push({ id: hub.id, fromSize: prevSize, toSize: hub.size })
        } else {
          graph.setNodeAttribute(hub.id, 'size', hub.size)
        }
      } else {
        const startSize = Math.max(4, hub.size * 0.45)
        graph.addNode(hub.id, {
          x: hub.hx,
          y: hub.hy,
          size: startSize,
          color: hub.colour,
          label: ''
        })
        hubAnimations.push({ id: hub.id, fromSize: startSize, toSize: hub.size })
      }
    }

    const nodeAnimations: Array<{
      id: string
      fromX: number
      fromY: number
      toX: number
      toY: number
    }> = []

    for (const item of visibleNodes) {
      if (graph.hasNode(item.data.id)) {
        const prevX = Number(graph.getNodeAttribute(item.data.id, 'x') ?? item.x)
        const prevY = Number(graph.getNodeAttribute(item.data.id, 'y') ?? item.y)
        graph.setNodeAttribute(item.data.id, 'size', item.size)
        graph.setNodeAttribute(item.data.id, 'color', item.baseColour)
        graph.setNodeAttribute(item.data.id, 'label', item.data.label)

        if (Math.hypot(item.x - prevX, item.y - prevY) > 0.02) {
          nodeAnimations.push({
            id: item.data.id,
            fromX: prevX,
            fromY: prevY,
            toX: item.x,
            toY: item.y
          })
        } else {
          graph.setNodeAttribute(item.data.id, 'x', item.x)
          graph.setNodeAttribute(item.data.id, 'y', item.y)
        }
      } else {
        graph.addNode(item.data.id, {
          x: item.x,
          y: item.y,
          size: item.size,
          color: item.baseColour,
          label: item.data.label
        })
      }
    }

    const desiredEdgeKeys = new Set<string>()
    for (const spoke of spokeEdges) {
      desiredEdgeKeys.add(spoke.key)
    }
    for (const sim of simEdges) {
      desiredEdgeKeys.add(sim.key)
    }
    const desiredGraphEdges = new Map<string, GraphEdgeData>()
    visibleEdges.forEach((edge, idx) => {
      if (edge.type === 'links_to' || edge.type === 'contradicts') return
      const key = `e-${idx}-${edge.source}-${edge.target}-${edge.type}`
      desiredEdgeKeys.add(key)
      desiredGraphEdges.set(key, edge)
    })

    for (const existingEdge of graph.edges()) {
      if (!desiredEdgeKeys.has(existingEdge)) {
        graph.dropEdge(existingEdge)
      }
    }

    for (const sim of simEdges) {
      if (!graph.hasEdge(sim.key) && graph.hasNode(sim.source) && graph.hasNode(sim.target)) {
        graph.addEdgeWithKey(sim.key, sim.source, sim.target, {
          size: 1.0,
          color: hexToRgba(sim.colour, 0.4)
        })
      }
    }

    for (const spoke of spokeEdges) {
      if (!graph.hasEdge(spoke.key) && graph.hasNode(spoke.hubId) && graph.hasNode(spoke.nodeId)) {
        graph.addEdgeWithKey(spoke.key, spoke.hubId, spoke.nodeId, {
          size: 0.65,
          color: hexToRgba(spoke.colour, 0.16)
        })
      }
    }

    for (const [key, edge] of desiredGraphEdges.entries()) {
      if (!graph.hasEdge(key) && graph.hasNode(edge.source) && graph.hasNode(edge.target)) {
        graph.addEdgeWithKey(key, edge.source, edge.target, {
          size: 0.7,
          color: 'rgba(148, 163, 184, 0.20)'
        })
      }
    }

    sigma.setCustomBBox(GRAPH_BBOX)
    sigma.refresh()
    if (visibleNodes.length > 0 && !hadNodesRef.current) {
      hadNodesRef.current = true
      sigma.getCamera().setState({ x: 0.5, y: 0.5, ratio: 1.12 })
    } else if (visibleNodes.length === 0) {
      hadNodesRef.current = false
    }
    syncOverlay()

    if (nodeAnimations.length > 0 || hubAnimations.length > 0) {
      const startTime = performance.now()
      const step = (now: number) => {
        const g = graphRef.current
        const s = sigmaRef.current
        if (!g || !s) return

        const progress = Math.min(1, (now - startTime) / TRANSITION_DURATION_MS)
        const eased = 1 - Math.pow(1 - progress, 3)

        for (const anim of nodeAnimations) {
          if (!g.hasNode(anim.id)) continue
          g.setNodeAttribute(anim.id, 'x', anim.fromX + (anim.toX - anim.fromX) * eased)
          g.setNodeAttribute(anim.id, 'y', anim.fromY + (anim.toY - anim.fromY) * eased)
        }
        for (const anim of hubAnimations) {
          if (!g.hasNode(anim.id)) continue
          g.setNodeAttribute(anim.id, 'size', anim.fromSize + (anim.toSize - anim.fromSize) * eased)
        }

        s.refresh()
        syncOverlay()

        if (progress < 1) {
          rafIdRef.current = window.requestAnimationFrame(step)
        } else {
          rafIdRef.current = null
        }
      }

      rafIdRef.current = window.requestAnimationFrame(step)
    }

    return () => {
      if (rafIdRef.current !== null) {
        window.cancelAnimationFrame(rafIdRef.current)
        rafIdRef.current = null
      }
    }
  }, [visibleHubs, visibleNodes, visibleEdges, spokeEdges, simEdges, syncOverlay])

  // Refresh Sigma styling without rebuilding graph when hover, focus, selection, or display settings change
  useEffect(() => {
    const sigma = sigmaRef.current
    if (!sigma) return
    sigma.setSetting('renderLabels', settings.showLabels)
    sigma.refresh()
    syncOverlay()
  }, [
    focusedStorylineId,
    selectedNodeId,
    hoveredNode,
    hoveredHub,
    settings.showLabels,
    settings.showLinks,
    settings.linkOpacity,
    syncOverlay
  ])

  const handleResetView = () => {
    sigmaRef.current?.getCamera().animate({ x: 0.5, y: 0.5, ratio: 1.12 }, { duration: 250 })
  }

  const togglePlatform = (platform: string) => {
    setSettings((prev) => {
      const exists = prev.enabledPlatforms.includes(platform)
      const next = exists
        ? prev.enabledPlatforms.filter((p) => p !== platform)
        : [...prev.enabledPlatforms, platform]
      return { ...prev, enabledPlatforms: next }
    })
  }

  const { firstDataHour, latestDataHour } = useMemo(() => {
    let first: number | null = null
    let latest: number | null = null
    for (const f of frames) {
      if (f.nodes_added.length > 0) {
        if (first === null || f.hour < first) first = f.hour
        if (latest === null || f.hour > latest) latest = f.hour
      }
    }
    return { firstDataHour: first, latestDataHour: latest }
  }, [frames])

  const clusterCount = visibleHubs.length
  const focusedStorylineName = focusedStorylineId
    ? (storylineNameMap.get(focusedStorylineId) ?? focusedStorylineId)
    : null

  const isEmpty = visibleNodes.length === 0

  return (
    <section className="vf-graph-stage" aria-label="Narrative graph canvas">
      <div ref={containerRef} className="vf-sigma-container" />

      {/* Viewport-synced SVG overlay for hub radial glow, hub label pills, recency glow rings, diamonds, conflict rings, focus rings, ripples, and dashed/dotted edges */}
      <svg className="vf-graph-overlay" aria-hidden="true">
        {overlayEdges.map((edge) => (
          <line
            key={edge.key}
            x1={edge.x1}
            y1={edge.y1}
            x2={edge.x2}
            y2={edge.y2}
            stroke={edge.kind === 'contradicts' ? '#facc15' : '#94a3b8'}
            strokeWidth={edge.highlighted ? 1.8 : edge.kind === 'contradicts' ? 1.4 : 1.0}
            strokeDasharray={edge.kind === 'contradicts' ? '2 4' : '5 4'}
            strokeOpacity={edge.dimmed ? 0.06 : edge.highlighted ? 0.72 : 0.3}
          />
        ))}

        {overlayPoints.map((pt) => {
          const r = pt.size
          const opacity = pt.dimmed ? 0.13 : 0.95
          return (
            <g key={pt.id} transform={`translate(${pt.vx}, ${pt.vy})`} opacity={opacity}>
              {pt.isRecent ? (
                <circle
                  r={r + 3.5}
                  fill="rgba(248, 250, 252, 0.08)"
                  stroke="rgba(248, 250, 252, 0.88)"
                  strokeWidth={1.3}
                />
              ) : null}
              {pt.isNewArrival ? (
                <circle
                  className="vf-arrival-ripple"
                  r={r + 4}
                  fill="none"
                  stroke="#f8fafc"
                  strokeWidth={1.2}
                />
              ) : null}
              {pt.hasConflict ? (
                <circle r={r + 2.5} fill="none" stroke="#facc15" strokeWidth={1.6} />
              ) : null}
              {pt.isOfficial || pt.isResponse ? (
                <polygon
                  points={`0,${-(r + 2)} ${r + 2},0 0,${r + 2} ${-(r + 2)},0`}
                  fill={pt.isResponse ? '#facc15' : '#f8fafc'}
                  stroke="#0b1120"
                  strokeWidth={1.2}
                />
              ) : null}
              {pt.isSelected || pt.isHovered ? (
                <circle
                  r={r + 5.5}
                  fill="none"
                  stroke={pt.isSelected ? '#38bdf8' : '#e2e8f0'}
                  strokeWidth={pt.isSelected ? 2 : 1.4}
                />
              ) : null}
            </g>
          )
        })}

        {overlayHubs.map((hub) => {
          const r = hub.size
          const opacity = hub.dimmed ? 0.15 : 1
          const pillText = formatHubPillLabel(hub.name, hub.count)
          const pillWidth = Math.max(82, Math.round(pillText.length * 6.3 + 28))
          const placeAbove = hub.hy < -6
          const pillY = placeAbove ? -(r + 20) : r + 18

          return (
            <g
              key={hub.id}
              transform={`translate(${hub.vx}, ${hub.vy})`}
              opacity={opacity}
            >
              <circle r={r + 14} fill={hexToRgba(hub.colour, 0.08)} />
              <circle r={r + 7} fill={hexToRgba(hub.colour, 0.18)} />
              <circle
                r={r + 3}
                fill="none"
                stroke={hexToRgba(hub.colour, hub.isFocused || hub.isHovered ? 0.95 : 0.6)}
                strokeWidth={hub.isFocused || hub.isHovered ? 2 : 1.3}
              />
              <circle r={r} fill="none" stroke="#0b1120" strokeWidth={1.5} />

              <g
                className={`vf-hub-pill${hub.isFocused ? ' is-focused' : ''}`}
                onClick={(e) => {
                  e.stopPropagation()
                  triggerHubFocus(hub.storylineId)
                }}
                onMouseEnter={() => {
                  setHoveredNode(null)
                  setHoveredHub(hub)
                }}
                onMouseLeave={() => {
                  setHoveredHub((prev) => (prev?.id === hub.id ? null : prev))
                }}
              >
                <rect
                  className="vf-hub-pill-bg"
                  x={-pillWidth / 2}
                  y={pillY - 11}
                  width={pillWidth}
                  height={22}
                  rx={11}
                  stroke={hexToRgba(hub.colour, hub.isFocused || hub.isHovered ? 0.95 : 0.55)}
                  strokeWidth={hub.isFocused || hub.isHovered ? 1.5 : 1}
                />
                <circle cx={-pillWidth / 2 + 11} cy={pillY} r={3.5} fill={hub.colour} />
                <text
                  className="vf-hub-pill-text"
                  x={-pillWidth / 2 + 20}
                  y={pillY + 0.5}
                  dominantBaseline="central"
                  textAnchor="start"
                  fontSize={11}
                  fontWeight={600}
                >
                  {pillText}
                </text>
              </g>
            </g>
          )
        })}
      </svg>

      {/* Top-left: Graph view settings toggle + camera reset (FR-3.5) */}
      <div className="vf-graph-top-left">
        <div className="vf-graph-controls-row">
          <button
            type="button"
            className="vf-overlay-btn"
            aria-expanded={settingsOpen}
            onClick={() => setSettingsOpen((prev) => !prev)}
          >
            Graph view
          </button>
          <button
            type="button"
            className="vf-overlay-btn"
            onClick={handleResetView}
            title="Reset graph zoom and pan"
          >
            Reset view
          </button>
        </div>

        {settingsOpen ? (
          <div className="vf-settings-popover" role="dialog" aria-label="Graph view settings">
            <label className="vf-setting-row">
              <span>Node size</span>
              <input
                type="range"
                min={0.5}
                max={2}
                step={0.1}
                value={settings.nodeSizeScale}
                onChange={(e) =>
                  setSettings((prev) => ({ ...prev, nodeSizeScale: Number(e.target.value) }))
                }
              />
            </label>
            <label className="vf-setting-row">
              <span>Link opacity</span>
              <input
                type="range"
                min={0.05}
                max={0.9}
                step={0.05}
                value={settings.linkOpacity}
                onChange={(e) =>
                  setSettings((prev) => ({ ...prev, linkOpacity: Number(e.target.value) }))
                }
              />
            </label>
            <label className="vf-setting-check">
              <input
                type="checkbox"
                checked={settings.showLinks}
                onChange={(e) =>
                  setSettings((prev) => ({ ...prev, showLinks: e.target.checked }))
                }
              />
              <span>Show links</span>
            </label>
            <label className="vf-setting-check">
              <input
                type="checkbox"
                checked={settings.showLabels}
                onChange={(e) =>
                  setSettings((prev) => ({ ...prev, showLabels: e.target.checked }))
                }
              />
              <span>Post labels</span>
            </label>

            {enabledSources.length > 0 ? (
              <div className="vf-setting-platforms">
                <span className="vf-setting-subtitle">Platforms</span>
                <div className="vf-setting-platform-list">
                  {enabledSources.map((src) => (
                    <label key={src} className="vf-setting-check">
                      <input
                        type="checkbox"
                        checked={settings.enabledPlatforms.includes(src)}
                        onChange={() => togglePlatform(src)}
                      />
                      <span>{formatPlatformLabel(src)}</span>
                    </label>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>

      {/* Top-center: Narrative context pill */}
      {!isEmpty ? (
        <div className="vf-graph-context-pill" role="status" aria-live="polite">
          {focusedStorylineId ? (
            <>
              <span
                className="vf-context-pill-dot"
                style={{
                  backgroundColor: getStorylineColour(
                    focusedStorylineId,
                    storylineColourIndexMap.get(focusedStorylineId)
                  )
                }}
              />
              <span>
                Focused: <strong>{focusedStorylineName}</strong> · Click again or Esc to clear
              </span>
            </>
          ) : (
            <span>
              {visibleNodes.length} {visibleNodes.length === 1 ? 'item' : 'items'} ·{' '}
              {clusterCount} {clusterCount === 1 ? 'narrative cluster' : 'narrative clusters'}
              {emergingStorylineCount > 0 ? ` · ${emergingStorylineCount} emerging` : ''}
            </span>
          )}
        </div>
      ) : null}

      {/* Top-right: Clock (FR-3.6) */}
      <div className="vf-graph-clock" aria-live="polite">
        <div className="vf-clock-hour">{formatHourOffset(currentHour)}</div>
        <div className="vf-clock-time">{formatT0ShortIst(t0Utc, currentHour)}</div>
      </div>

      {/* Hover tooltip */}
      {hoveredHub ? (
        <div className="vf-graph-tooltip" role="status">
          <div className="vf-tooltip-title">{hoveredHub.name}</div>
          <div className="vf-tooltip-meta">
            <span>Narrative cluster</span>
            <span>·</span>
            <span>
              {hoveredHub.count} {hoveredHub.count === 1 ? 'item' : 'items'}
            </span>
            <span>·</span>
            <span>
              {focusedStorylineId === hoveredHub.storylineId
                ? 'Click to clear focus'
                : 'Click to focus'}
            </span>
          </div>
        </div>
      ) : hoveredNode ? (
        <div className="vf-graph-tooltip" role="status">
          <div className="vf-tooltip-title">{hoveredNode.label}</div>
          <div className="vf-tooltip-meta">
            <span>{formatPlatformLabel(hoveredNode.platform)}</span>
            <span>·</span>
            <span>{formatHourOffset(hoveredNode.hour)}</span>
            {hoveredNode.is_official || hoveredNode.self_reported ? (
              <>
                <span>·</span>
                <span>Self-reported</span>
              </>
            ) : null}
          </div>
        </div>
      ) : null}

      {/* Empty / Collecting state (design.md) */}
      {isEmpty ? (
        <div className="vf-empty-canvas" role="status">
          <div className="vf-empty-title">Collecting since {formatUtcTimestamp(t0Utc)}</div>
          <p className="vf-empty-subtitle">
            {frames.length === 0
              ? 'Waiting for replay frames or live items at this playhead.'
              : firstDataHour !== null && firstDataHour > currentHour
                ? `No items recorded up to ${formatHourOffset(currentHour)}. First collected items appear at ${formatHourOffset(firstDataHour)}.`
                : `No items recorded up to ${formatHourOffset(currentHour)}.`}
          </p>
          {onJumpToHour && latestDataHour !== null && latestDataHour > currentHour ? (
            <button
              type="button"
              className="vf-overlay-btn"
              style={{ marginTop: '0.75rem', pointerEvents: 'auto' }}
              onClick={() => onJumpToHour(latestDataHour)}
            >
              Jump to latest ({formatHourOffset(latestDataHour)})
            </button>
          ) : null}
        </div>
      ) : null}

      {/* Bottom-right: Legend (FR-3.6) */}
      <div className="vf-graph-legend" aria-label="Graph legend">
        <div className="vf-legend-item">
          <span className="vf-legend-swatch vf-swatch-recent" />
          <span>&lt;2h recent</span>
        </div>
        <div className="vf-legend-item">
          <span className="vf-legend-diamond" />
          <span>Official (self-reported)</span>
        </div>
        <div className="vf-legend-item">
          <span className="vf-legend-diamond vf-swatch-gold" />
          <span>Response</span>
        </div>
        <div className="vf-legend-item">
          <span className="vf-legend-ring" />
          <span>Disclosed conflict</span>
        </div>
        <div className="vf-legend-item">
          <span className="vf-legend-line-dashed" />
          <span>Platform jump</span>
        </div>
        <div className="vf-legend-item">
          <span className="vf-legend-line-dotted" />
          <span>Contradicts</span>
        </div>
      </div>
    </section>
  )
}
