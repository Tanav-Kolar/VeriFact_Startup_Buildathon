import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FrameData, GraphEdgeData, GraphNodeData, StorylineRow } from '../types/api'
import {
  formatCount,
  formatHourOffset,
  formatPlatformLabel,
  formatT0ShortIst,
  formatUtcTimestamp
} from '../utils/format'
import {
  RESPONSE_GOLD_COLOUR,
  getStorylineColour,
  hashString,
  hexToRgba
} from '../utils/palette'

export interface NebulaGraphProps {
  t0Utc: string | null
  frames: FrameData[]
  storylines: StorylineRow[]
  enabledSources: string[]
  enabledPlatforms?: string[]
  onTogglePlatform?: (platform: string) => void
  onResetPlatforms?: () => void
  currentHour: number
  focusedStorylineId: string | null
  selectedNodeId: string | null
  onSelectNode: (node: GraphNodeData | null) => void
  onFocusStoryline?: (storylineId: string) => void
  onJumpToHour?: (hour: number) => void
}

interface SimNode {
  data: GraphNodeData
  storylineId: string
  x: number
  y: number
  z: number
  vx: number
  vy: number
  vz: number
  pinned: boolean
  phase: number
  rippleStart: number
  inCount: number
  outCount: number
  simIndex: number
}

interface SimEdge {
  key: string
  source: string
  target: string
  type: GraphEdgeData['type'] | 'similarity'
  storylineId?: string
  colour?: string
}

interface ProjectedNodeInfo {
  kind: 'node'
  node: SimNode
  sx: number
  sy: number
  cz: number
  scale: number
  radius: number
}

interface ProjectedHubInfo {
  kind: 'hub'
  storylineId: string
  name: string
  count: number
  colour: string
  sx: number
  sy: number
  cz: number
  scale: number
  radius: number
  dimmed: boolean
  pillX: number
  pillY: number
  pillW: number
  pillH: number
  pillText: string
}

type HoverTarget =
  | { kind: 'node'; node: SimNode; sx: number; sy: number }
  | { kind: 'hub'; storylineId: string; name: string; count: number; colour: string; sx: number; sy: number }

interface CameraState {
  yaw: number
  pitch: number
  distance: number
  tx: number
  ty: number
  tz: number
  targetYaw: number
  targetPitch: number
  targetDistance: number
  targetTx: number
  targetTy: number
  targetTz: number
}

const CLUSTER_MIN_ITEMS = 3
const SPATIAL_CELL_SIZE = 68
const MAX_NEIGHBOR_REPEL_CHECKS = 24
const DEFAULT_CAM = {
  yaw: 0.42,
  pitch: 0.28,
  distance: 520,
  tx: 0,
  ty: 0,
  tz: 0
}

const STARFIELD = Array.from({ length: 85 }, (_, idx) => {
  const h1 = hashString(`star-x-${idx}`)
  const h2 = hashString(`star-y-${idx}`)
  const h3 = hashString(`star-z-${idx}`)
  return {
    x: (((h1 % 1000) / 1000) - 0.5) * 920,
    y: (((h2 % 1000) / 1000) - 0.5) * 660,
    z: (((h3 % 1000) / 1000) - 0.5) * 920,
    r: 0.7 + ((h1 % 10) / 10) * 1.3,
    a: 0.14 + ((h2 % 10) / 10) * 0.25
  }
})

function computeStoryline3DAnchor(
  storylineId: string,
  storylineOrder: Map<string, number>
): [number, number, number] {
  if (!storylineId || storylineId === 'unassigned') {
    return [0, 12, 0]
  }
  const total = Math.max(1, storylineOrder.size)
  const index = storylineOrder.get(storylineId) ?? hashString(storylineId) % total
  // Fibonacci 3D sphere distribution so clusters never overlap in 3D
  const goldenAngle = Math.PI * (3 - Math.sqrt(5))
  const yNorm = total === 1 ? 0 : 1 - (index / Math.max(1, total - 1)) * 1.65 - 0.12
  const clampedY = Math.max(-0.85, Math.min(0.85, yNorm))
  const radial = Math.sqrt(Math.max(0.05, 1 - clampedY * clampedY))
  const theta = goldenAngle * index + 0.4
  const sphereR = 148
  return [
    Math.cos(theta) * radial * sphereR,
    clampedY * (sphereR * 0.72),
    Math.sin(theta) * radial * sphereR
  ]
}

function computeScatter3D(nodeId: string): [number, number, number] {
  const h1 = hashString(`${nodeId}:sx`)
  const h2 = hashString(`${nodeId}:sy`)
  const h3 = hashString(`${nodeId}:sz`)
  const theta = ((h1 % 10000) / 10000) * Math.PI * 2
  const u = ((h2 % 10000) / 10000) * 2 - 1
  const r = 72 + Math.cbrt((h3 % 10000) / 10000) * 145
  const phi = Math.acos(Math.max(-1, Math.min(1, u)))
  return [
    r * Math.sin(phi) * Math.cos(theta),
    r * Math.cos(phi) * 0.78,
    r * Math.sin(phi) * Math.sin(theta)
  ]
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

function projectPoint(
  wx: number,
  wy: number,
  wz: number,
  width: number,
  height: number,
  cam: CameraState
): { sx: number; sy: number; cz: number; scale: number } | null {
  const dx = wx - cam.tx
  const dy = wy - cam.ty
  const dz = wz - cam.tz

  const cosY = Math.cos(cam.yaw)
  const sinY = Math.sin(cam.yaw)
  const x1 = dx * cosY - dz * sinY
  const z1 = dx * sinY + dz * cosY

  const cosP = Math.cos(cam.pitch)
  const sinP = Math.sin(cam.pitch)
  const y2 = dy * cosP - z1 * sinP
  const z2 = dy * sinP + z1 * cosP

  const cz = z2 + cam.distance
  if (cz <= 25) return null

  const focal = 580
  const scale = focal / cz
  return {
    sx: width * 0.5 + x1 * scale,
    sy: height * 0.5 + y2 * scale,
    cz,
    scale
  }
}

function drawArrowHead(
  ctx: CanvasRenderingContext2D,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  radiusOffset: number,
  size: number
) {
  const dx = x2 - x1
  const dy = y2 - y1
  const len = Math.hypot(dx, dy)
  if (len < radiusOffset + 6) return
  const ux = dx / len
  const uy = dy / len
  const tipX = x2 - ux * (radiusOffset + 2)
  const tipY = y2 - uy * (radiusOffset + 2)
  const backX = tipX - ux * size
  const backY = tipY - uy * size
  const perpX = -uy * (size * 0.52)
  const perpY = ux * (size * 0.52)
  ctx.beginPath()
  ctx.moveTo(tipX, tipY)
  ctx.lineTo(backX + perpX, backY + perpY)
  ctx.lineTo(backX - perpX, backY - perpY)
  ctx.closePath()
  ctx.fill()
}

function computeLocalNeighborhood(
  centerId: string | null,
  depth: number,
  includeIn: boolean,
  includeOut: boolean,
  baseNodeIds: Set<string>,
  baseEdges: GraphEdgeData[]
): Set<string> {
  const result = new Set<string>()
  if (!centerId || !baseNodeIds.has(centerId)) return result
  result.add(centerId)
  let frontier = [centerId]

  for (let step = 0; step < depth; step += 1) {
    const frontierSet = new Set(frontier)
    const nextFrontier: string[] = []
    for (let i = 0; i < baseEdges.length; i += 1) {
      const e = baseEdges[i]
      if (includeOut && frontierSet.has(e.source) && !result.has(e.target)) {
        result.add(e.target)
        nextFrontier.push(e.target)
      }
      if (includeIn && frontierSet.has(e.target) && !result.has(e.source)) {
        result.add(e.source)
        nextFrontier.push(e.source)
      }
    }
    if (nextFrontier.length === 0) break
    frontier = nextFrontier
  }
  return result
}

export function NebulaGraph({
  t0Utc,
  frames,
  storylines,
  enabledSources,
  enabledPlatforms: externalPlatforms,
  onTogglePlatform,
  onResetPlatforms,
  currentHour,
  focusedStorylineId,
  selectedNodeId,
  onSelectNode,
  onFocusStoryline,
  onJumpToHour
}: NebulaGraphProps) {
  const stageRef = useRef<HTMLElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  // Controls drawer & filter state
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [internalPlatforms, setInternalPlatforms] = useState<string[]>([])
  const [showUnassigned, setShowUnassigned] = useState(true)
  const [officialOnly, setOfficialOnly] = useState(false)
  const [conflictsOnly, setConflictsOnly] = useState(false)
  const [contradictionsOnly, setContradictionsOnly] = useState(false)
  const [hiddenStorylines, setHiddenStorylines] = useState<Set<string>>(() => new Set())

  // Display state
  const [showArrows, setShowArrows] = useState(true)
  const [showLinks, setShowLinks] = useState(true)
  const [showHalos, setShowHalos] = useState(true)
  const [autoOrbit, setAutoOrbit] = useState(true)
  const [textFadeThreshold, setTextFadeThreshold] = useState(1.15)
  const [nodeSizeScale, setNodeSizeScale] = useState(1.0)
  const [linkOpacity, setLinkOpacity] = useState(0.35)

  // 3D Physics state
  const [physicsPaused, setPhysicsPaused] = useState(false)
  const [centerForce, setCenterForce] = useState(0.45)
  const [repelForce, setRepelForce] = useState(11.0)
  const [linkForce, setLinkForce] = useState(0.45)
  const [linkDistance, setLinkDistance] = useState(58)

  // Local graph state
  const [localMode, setLocalMode] = useState(false)
  const [localCenterId, setLocalCenterId] = useState<string | null>(null)
  const [localDepth, setLocalDepth] = useState<1 | 2 | 3>(1)
  const [localIncoming, setLocalIncoming] = useState(true)
  const [localOutgoing, setLocalOutgoing] = useState(true)

  // Hover card state
  const [hoverTarget, setHoverTarget] = useState<HoverTarget | null>(null)

  // Available platforms & live counts at currentHour
  const { availablePlatforms, platformCountsAtHour } = useMemo(() => {
    const platSet = new Set<string>(enabledSources.map((s) => s.toLowerCase()))
    const counts = new Map<string, number>()
    const seenIds = new Set<string>()

    for (const f of frames) {
      for (const n of f.nodes_added) {
        if (!n.platform) continue
        const plat = n.platform.toLowerCase()
        platSet.add(plat)
        if (f.hour <= currentHour && n.hour <= currentHour && !seenIds.has(n.id)) {
          seenIds.add(n.id)
          counts.set(plat, (counts.get(plat) ?? 0) + 1)
        }
      }
    }
    return {
      availablePlatforms: Array.from(platSet),
      platformCountsAtHour: counts
    }
  }, [enabledSources, frames, currentHour])

  useEffect(() => {
    if (!externalPlatforms && availablePlatforms.length > 0) {
      setInternalPlatforms((prev) => (prev.length === 0 ? availablePlatforms : prev))
    }
  }, [externalPlatforms, availablePlatforms])

  const activePlatforms = externalPlatforms ?? internalPlatforms

  const handlePlatformToggle = useCallback(
    (platform: string) => {
      if (onTogglePlatform) {
        onTogglePlatform(platform)
        return
      }
      const plat = platform.toLowerCase()
      setInternalPlatforms((prev) =>
        prev.includes(plat) ? prev.filter((p) => p !== plat) : [...prev, plat]
      )
    },
    [onTogglePlatform]
  )

  const handlePlatformReset = useCallback(() => {
    if (onResetPlatforms) {
      onResetPlatforms()
      return
    }
    setInternalPlatforms(availablePlatforms)
  }, [onResetPlatforms, availablePlatforms])

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

  // Persistent 3D simulation store across hour scrubbing & playback
  const simNodeStoreRef = useRef<Map<string, SimNode>>(new Map())
  const simAlphaRef = useRef(0.88)
  const prevNodeCountRef = useRef(0)
  const prevEmergedSetRef = useRef<Set<string>>(new Set())

  const camRef = useRef<CameraState>({
    yaw: DEFAULT_CAM.yaw,
    pitch: DEFAULT_CAM.pitch,
    distance: DEFAULT_CAM.distance,
    tx: DEFAULT_CAM.tx,
    ty: DEFAULT_CAM.ty,
    tz: DEFAULT_CAM.tz,
    targetYaw: DEFAULT_CAM.yaw,
    targetPitch: DEFAULT_CAM.pitch,
    targetDistance: DEFAULT_CAM.distance,
    targetTx: DEFAULT_CAM.tx,
    targetTy: DEFAULT_CAM.ty,
    targetTz: DEFAULT_CAM.tz
  })

  // Screen projection caches for hit-testing
  const projectedNodesRef = useRef<Map<string, ProjectedNodeInfo>>(new Map())
  const projectedHubsRef = useRef<ProjectedHubInfo[]>([])

  // Recompute filtered graph & emergent clusters up to currentHour
  const {
    activeNodes,
    activeEdges,
    emergedStorylines,
    emergingStorylineCount,
    clusterByStoryline,
    searchMatches,
    localCenterLabel,
    allStorylineCounts
  } = useMemo(() => {
    const allowedPlatforms =
      activePlatforms.length > 0
        ? new Set(activePlatforms.map((p) => p.toLowerCase()))
        : new Set<string>()
    const q = searchQuery.trim().toLowerCase()

    const rawNodesById = new Map<string, GraphNodeData>()
    const rawEdges: GraphEdgeData[] = []

    const sortedFrames = [...frames]
      .filter((f) => f.hour <= currentHour)
      .sort((a, b) => a.hour - b.hour)

    for (const frame of sortedFrames) {
      for (const n of frame.nodes_added) {
        if (n.hour > currentHour) continue
        rawNodesById.set(n.id, n)
      }
      for (const e of frame.edges_added) {
        if (e.hour <= currentHour) {
          rawEdges.push(e)
        }
      }
    }

    // Count all items per storyline (matching enabled platforms) for the Drawer Groups list
    const groupCounts = new Map<string, number>()
    for (const n of rawNodesById.values()) {
      if (allowedPlatforms.size > 0 && !allowedPlatforms.has(n.platform.toLowerCase())) continue
      const sid = n.storyline_id ?? 'unassigned'
      groupCounts.set(sid, (groupCounts.get(sid) ?? 0) + 1)
    }

    // Apply all filters strictly at hour <= currentHour
    const baseNodes: GraphNodeData[] = []
    const baseNodeIds = new Set<string>()
    const matches = new Set<string>()

    for (const n of rawNodesById.values()) {
      if (!allowedPlatforms.has(n.platform.toLowerCase())) continue
      const sid = n.storyline_id ?? 'unassigned'
      if (!showUnassigned && sid === 'unassigned') continue
      if (hiddenStorylines.has(sid)) continue
      if (officialOnly && !n.is_official && !n.self_reported) continue
      if (conflictsOnly && !n.conflict) continue

      baseNodes.push(n)
      baseNodeIds.add(n.id)

      if (q.length > 0) {
        const stName = storylineNameMap.get(sid) ?? sid
        const hay = `${n.label} ${n.platform} ${sid} ${stName}`.toLowerCase()
        if (hay.includes(q)) {
          matches.add(n.id)
        }
      }
    }

    let baseEdges = rawEdges.filter(
      (e) => baseNodeIds.has(e.source) && baseNodeIds.has(e.target) && e.source !== e.target
    )

    if (contradictionsOnly) {
      baseEdges = baseEdges.filter((e) => e.type === 'contradicts')
      const linked = new Set<string>()
      for (const e of baseEdges) {
        linked.add(e.source)
        linked.add(e.target)
      }
      const narrowed = baseNodes.filter((n) => linked.has(n.id))
      baseNodes.length = 0
      baseNodeIds.clear()
      for (const n of narrowed) {
        baseNodes.push(n)
        baseNodeIds.add(n.id)
      }
    }

    let finalRawNodes = baseNodes
    let finalRawEdges = baseEdges
    let resolvedLocalLabel: string | null = null

    if (localMode) {
      const center =
        localCenterId && baseNodeIds.has(localCenterId)
          ? localCenterId
          : selectedNodeId && baseNodeIds.has(selectedNodeId)
            ? selectedNodeId
            : (baseNodes[0]?.id ?? null)

      if (center) {
        const centerNode = rawNodesById.get(center)
        resolvedLocalLabel = centerNode?.label ?? center
        const localIds = computeLocalNeighborhood(
          center,
          localDepth,
          localIncoming,
          localOutgoing,
          baseNodeIds,
          baseEdges
        )
        finalRawNodes = baseNodes.filter((n) => localIds.has(n.id))
        finalRawEdges = baseEdges.filter((e) => localIds.has(e.source) && localIds.has(e.target))
      }
    }

    // Group active nodes by storyline to detect emergent clusters (>= 3 items) vs pre-cluster points
    const byStoryline = new Map<string, GraphNodeData[]>()
    for (const n of finalRawNodes) {
      const sid = n.storyline_id ?? 'unassigned'
      const bucket = byStoryline.get(sid)
      if (bucket) bucket.push(n)
      else byStoryline.set(sid, [n])
    }

    const clusters = new Map<
      string,
      {
        storylineId: string
        name: string
        colour: string
        count: number
        pull: number
        anchor: [number, number, number]
      }
    >()
    const emergedList: string[] = []
    let emergingCount = 0
    const simBridgeEdges: SimEdge[] = []

    for (const [sid, items] of byStoryline.entries()) {
      const colour = getStorylineColour(sid, storylineColourIndexMap.get(sid))
      if (items.length >= CLUSTER_MIN_ITEMS) {
        const pull = Math.min(1, 0.45 + (items.length - CLUSTER_MIN_ITEMS) * 0.14)
        clusters.set(sid, {
          storylineId: sid,
          name: storylineNameMap.get(sid) ?? sid,
          colour,
          count: items.length,
          pull,
          anchor: computeStoryline3DAnchor(sid, storylineOrderMap)
        })
        emergedList.push(sid)
      } else {
        emergingCount += 1
        if (items.length === 2) {
          const sorted = [...items].sort((a, b) => a.id.localeCompare(b.id))
          simBridgeEdges.push({
            key: `__sim__:${sorted[0].id}:${sorted[1].id}`,
            source: sorted[0].id,
            target: sorted[1].id,
            type: 'similarity',
            storylineId: sid,
            colour
          })
        }
      }
    }

    // Sync persistent SimNode objects (preserving 3D positions & velocities!)
    const store = simNodeStoreRef.current
    const nowMs = performance.now()
    const inDeg = new Map<string, number>()
    const outDeg = new Map<string, number>()
    for (const e of finalRawEdges) {
      outDeg.set(e.source, (outDeg.get(e.source) ?? 0) + 1)
      inDeg.set(e.target, (inDeg.get(e.target) ?? 0) + 1)
    }

    const simNodes: SimNode[] = []
    for (let i = 0; i < finalRawNodes.length; i += 1) {
      const n = finalRawNodes[i]
      const sid = n.storyline_id ?? 'unassigned'
      let sim = store.get(n.id)
      if (!sim) {
        const [sx, sy, sz] = computeScatter3D(n.id)
        sim = {
          data: n,
          storylineId: sid,
          x: sx,
          y: sy,
          z: sz,
          vx: 0,
          vy: 0,
          vz: 0,
          pinned: false,
          phase: (hashString(n.id) % 628) / 100,
          rippleStart: n.hour === currentHour ? nowMs : -1,
          inCount: inDeg.get(n.id) ?? 0,
          outCount: outDeg.get(n.id) ?? 0,
          simIndex: i
        }
        store.set(n.id, sim)
      } else {
        sim.data = n
        sim.storylineId = sid
        sim.inCount = inDeg.get(n.id) ?? 0
        sim.outCount = outDeg.get(n.id) ?? 0
        sim.simIndex = i
        if (n.hour === currentHour && sim.rippleStart < 0) {
          sim.rippleStart = nowMs
        }
      }
      simNodes.push(sim)
    }

    const combinedEdges: SimEdge[] = [
      ...finalRawEdges.map((e, idx) => ({
        key: `e-${idx}-${e.source}-${e.target}-${e.type}`,
        source: e.source,
        target: e.target,
        type: e.type
      })),
      ...simBridgeEdges
    ]

    return {
      activeNodes: simNodes,
      activeEdges: combinedEdges,
      emergedStorylines: emergedList,
      emergingStorylineCount: emergingCount,
      clusterByStoryline: clusters,
      searchMatches: matches,
      localCenterLabel: resolvedLocalLabel,
      allStorylineCounts: groupCounts
    }
  }, [
    frames,
    currentHour,
    activePlatforms,
    searchQuery,
    showUnassigned,
    hiddenStorylines,
    officialOnly,
    conflictsOnly,
    contradictionsOnly,
    localMode,
    localCenterId,
    selectedNodeId,
    localDepth,
    localIncoming,
    localOutgoing,
    storylineColourIndexMap,
    storylineNameMap,
    storylineOrderMap
  ])

  // Reheat 3D physics smoothly when node count changes or a new storyline crosses >= 3 threshold
  useEffect(() => {
    const nextEmerged = new Set(emergedStorylines)
    let newlyEmerged = false
    for (const sid of nextEmerged) {
      if (!prevEmergedSetRef.current.has(sid)) {
        newlyEmerged = true
        break
      }
    }
    if (activeNodes.length !== prevNodeCountRef.current || newlyEmerged) {
      simAlphaRef.current = Math.max(simAlphaRef.current, newlyEmerged ? 0.65 : 0.45)
    }
    prevNodeCountRef.current = activeNodes.length
    prevEmergedSetRef.current = nextEmerged
  }, [activeNodes.length, emergedStorylines])

  // Live refs for the 60fps animation loop and pointer event handlers
  const renderStateRef = useRef({
    activeNodes,
    activeEdges,
    clusterByStoryline,
    searchMatches,
    currentHour,
    focusedStorylineId,
    selectedNodeId,
    showArrows,
    showLinks,
    showHalos,
    autoOrbit,
    textFadeThreshold,
    nodeSizeScale,
    linkOpacity,
    physicsPaused,
    centerForce,
    repelForce,
    linkForce,
    linkDistance,
    localMode,
    searchQuery,
    storylineColourIndexMap,
    storylineNameMap
  })
  renderStateRef.current = {
    activeNodes,
    activeEdges,
    clusterByStoryline,
    searchMatches,
    currentHour,
    focusedStorylineId,
    selectedNodeId,
    showArrows,
    showLinks,
    showHalos,
    autoOrbit,
    textFadeThreshold,
    nodeSizeScale,
    linkOpacity,
    physicsPaused,
    centerForce,
    repelForce,
    linkForce,
    linkDistance,
    localMode,
    searchQuery,
    storylineColourIndexMap,
    storylineNameMap
  }

  const pointerStateRef = useRef({
    draggedNodeId: null as string | null,
    isOrbitDragging: false,
    isPanDragging: false,
    lastX: 0,
    lastY: 0,
    movedDistance: 0,
    hoveredNodeId: null as string | null,
    hoveredHubId: null as string | null,
    lastClickTime: 0,
    lastClickNodeId: null as string | null
  })

  const enterLocalGraph = useCallback(
    (nodeId: string | null) => {
      const targetId = nodeId ?? renderStateRef.current.activeNodes[0]?.data.id ?? null
      if (!targetId) return
      const sim = simNodeStoreRef.current.get(targetId)
      setLocalMode(true)
      setLocalCenterId(targetId)
      if (sim) {
        camRef.current.targetTx = sim.x
        camRef.current.targetTy = sim.y
        camRef.current.targetTz = sim.z
      }
      camRef.current.targetDistance = 260
      simAlphaRef.current = Math.max(simAlphaRef.current, 0.55)
    },
    []
  )

  const exitLocalGraph = useCallback(() => {
    setLocalMode(false)
    camRef.current.targetTx = 0
    camRef.current.targetTy = 0
    camRef.current.targetTz = 0
    camRef.current.targetDistance = 520
  }, [])

  const resetCameraView = useCallback(() => {
    const cam = camRef.current
    cam.targetYaw = DEFAULT_CAM.yaw
    cam.targetPitch = DEFAULT_CAM.pitch
    cam.targetDistance = renderStateRef.current.localMode ? 280 : DEFAULT_CAM.distance
    cam.targetTx = 0
    cam.targetTy = 0
    cam.targetTz = 0
  }, [])

  const triggerHubFocus = useCallback(
    (storylineId: string) => {
      if (onFocusStoryline) {
        onFocusStoryline(storylineId)
        return
      }
      onSelectNode({
        id: storylineId,
        type: 'storyline',
        platform: 'storyline',
        kind: 'storyline',
        created_at: '',
        hour: renderStateRef.current.currentHour,
        storyline_id: storylineId,
        reach: null,
        is_official: false,
        self_reported: false,
        conflict: null,
        lang: null,
        label: storylineId
      })
    },
    [onFocusStoryline, onSelectNode]
  )

  // 60 FPS 3D Force Physics + Canvas Perspective Renderer
  useEffect(() => {
    let rafId = 0
    const spatialGrid = new Map<number, SimNode[]>()
    const gridBucketsPool: SimNode[][] = []

    const stepPhysics3D = (nowMs: number) => {
      const rs = renderStateRef.current
      if (rs.physicsPaused || rs.activeNodes.length === 0) return

      const nodes = rs.activeNodes
      const edges = rs.activeEdges
      const nLen = nodes.length
      const alpha = simAlphaRef.current
      const repelStrength = rs.repelForce * 20
      const centerPull = rs.centerForce * 0.018

      // 1. Emergent cluster gravity + gentle ambient 3D buoyancy
      const clusterSums = new Map<
        string,
        { x: number; y: number; z: number; count: number; anchor: [number, number, number] }
      >()

      for (let i = 0; i < nLen; i += 1) {
        const n = nodes[i]
        const cluster = rs.clusterByStoryline.get(n.storylineId)

        if (cluster) {
          const acc = clusterSums.get(n.storylineId)
          if (acc) {
            acc.x += n.x
            acc.y += n.y
            acc.z += n.z
            acc.count += 1
          } else {
            clusterSums.set(n.storylineId, {
              x: n.x,
              y: n.y,
              z: n.z,
              count: 1,
              anchor: cluster.anchor
            })
          }
        }

        if (n.pinned) {
          n.vx = 0
          n.vy = 0
          n.vz = 0
          continue
        }

        if (cluster) {
          // Coalesce toward 3D storyline anchor scaled by pull (0.45 -> 1.0)
          const [ax, ay, az] = cluster.anchor
          const pullStrength = centerPull * (0.55 + cluster.pull * 0.85)
          n.vx += ((ax - n.x) * pullStrength - n.x * centerPull * 0.16) * alpha
          n.vy += ((ay - n.y) * pullStrength - n.y * centerPull * 0.16) * alpha
          n.vz += ((az - n.z) * pullStrength - n.z * centerPull * 0.16) * alpha
        } else {
          // Pre-cluster (< 3 items): float freely with very gentle origin centering
          const gentleOrigin = centerPull * 0.14
          n.vx += -n.x * gentleOrigin * alpha
          n.vy += -n.y * gentleOrigin * alpha
          n.vz += -n.z * gentleOrigin * alpha
        }

        // Subtle weightless ambient 3D floating drift
        const t = nowMs * 0.00075 + n.phase
        n.vx += Math.sin(t) * 0.011
        n.vy += Math.cos(t * 0.85) * 0.011
        n.vz += Math.sin(t * 0.7 + 1.1) * 0.011
      }

      // 2. Cluster-to-cluster centroid separation (keeps emerged 3D nebulae well separated)
      if (clusterSums.size > 1) {
        const centroids = Array.from(clusterSums.entries())
        for (let i = 0; i < centroids.length; i += 1) {
          for (let j = i + 1; j < centroids.length; j += 1) {
            const [sidA, a] = centroids[i]
            const [sidB, b] = centroids[j]
            const dx = a.x / a.count - b.x / b.count
            const dy = a.y / a.count - b.y / b.count
            const dz = a.z / a.count - b.z / b.count
            const distSq = dx * dx + dy * dy + dz * dz + 100
            if (distSq < 28000) {
              const dist = Math.sqrt(distSq)
              const push = ((180 - Math.min(180, dist)) / 180) * 0.12 * alpha
              const ux = (dx / dist) * push
              const uy = (dy / dist) * push
              const uz = (dz / dist) * push
              for (let k = 0; k < nLen; k += 1) {
                const node = nodes[k]
                if (node.pinned) continue
                if (node.storylineId === sidA) {
                  node.vx += ux
                  node.vy += uy
                  node.vz += uz
                } else if (node.storylineId === sidB) {
                  node.vx -= ux
                  node.vy -= uy
                  node.vz -= uz
                }
              }
            }
          }
        }
      }

      // 3. Fast O(N * k) 3D Spatial Hash Repulsion (smooth 60fps for 767+ live nodes)
      let usedBuckets = 0
      spatialGrid.clear()
      for (let i = 0; i < nLen; i += 1) {
        const n = nodes[i]
        const ix = Math.floor(n.x / SPATIAL_CELL_SIZE) + 512
        const iy = Math.floor(n.y / SPATIAL_CELL_SIZE) + 512
        const iz = Math.floor(n.z / SPATIAL_CELL_SIZE) + 512
        const key = (ix * 1024 + iy) * 1024 + iz
        let bucket = spatialGrid.get(key)
        if (!bucket) {
          bucket = gridBucketsPool[usedBuckets]
          if (!bucket) {
            bucket = []
            gridBucketsPool.push(bucket)
          } else {
            bucket.length = 0
          }
          usedBuckets += 1
          spatialGrid.set(key, bucket)
        }
        bucket.push(n)
      }

      const minAlpha = Math.max(0.25, alpha)
      for (let i = 0; i < nLen; i += 1) {
        const a = nodes[i]
        const ax = Math.floor(a.x / SPATIAL_CELL_SIZE) + 512
        const ay = Math.floor(a.y / SPATIAL_CELL_SIZE) + 512
        const az = Math.floor(a.z / SPATIAL_CELL_SIZE) + 512
        let checks = 0

        for (let ox = -1; ox <= 1 && checks < MAX_NEIGHBOR_REPEL_CHECKS; ox += 1) {
          for (let oy = -1; oy <= 1 && checks < MAX_NEIGHBOR_REPEL_CHECKS; oy += 1) {
            for (let oz = -1; oz <= 1 && checks < MAX_NEIGHBOR_REPEL_CHECKS; oz += 1) {
              const key = ((ax + ox) * 1024 + (ay + oy)) * 1024 + (az + oz)
              const bucket = spatialGrid.get(key)
              if (!bucket) continue
              for (let bIdx = 0; bIdx < bucket.length; bIdx += 1) {
                const b = bucket[bIdx]
                if (b.simIndex <= a.simIndex) continue
                checks += 1
                const dx = a.x - b.x
                const dy = a.y - b.y
                const dz = a.z - b.z
                const distSq = dx * dx + dy * dy + dz * dz + 140
                if (distSq > 36000) continue
                const dist = Math.sqrt(distSq)
                const force = (repelStrength / distSq) * minAlpha
                const fx = (dx / dist) * force
                const fy = (dy / dist) * force
                const fz = (dz / dist) * force
                if (!a.pinned) {
                  a.vx += fx
                  a.vy += fy
                  a.vz += fz
                }
                if (!b.pinned) {
                  b.vx -= fx
                  b.vy -= fy
                  b.vz -= fz
                }
                if (checks >= MAX_NEIGHBOR_REPEL_CHECKS) break
              }
            }
          }
        }
      }

      // 4. Hookean 3D Edge Springs (including similarity bridges)
      const springK = rs.linkForce * 0.045
      const targetDist = rs.linkDistance
      const store = simNodeStoreRef.current
      for (let i = 0; i < edges.length; i += 1) {
        const e = edges[i]
        const src = store.get(e.source)
        const dst = store.get(e.target)
        if (!src || !dst) continue
        const dx = dst.x - src.x
        const dy = dst.y - src.y
        const dz = dst.z - src.z
        const dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1
        const desiredDist = e.type === 'similarity' ? targetDist * 0.72 : targetDist
        const strength = e.type === 'similarity' ? springK * 0.55 : springK
        const diff = (dist - desiredDist) * strength * Math.max(0.3, alpha)
        const sx = (dx / dist) * diff
        const sy = (dy / dist) * diff
        const sz = (dz / dist) * diff
        if (!src.pinned) {
          src.vx += sx
          src.vy += sy
          src.vz += sz
        }
        if (!dst.pinned) {
          dst.vx -= sx
          dst.vy -= sy
          dst.vz -= sz
        }
      }

      // 5. Integrate & Damp
      const maxV = 6.2
      for (let i = 0; i < nLen; i += 1) {
        const n = nodes[i]
        if (n.pinned) continue
        n.vx *= 0.84
        n.vy *= 0.84
        n.vz *= 0.84
        n.vx = Math.max(-maxV, Math.min(maxV, n.vx))
        n.vy = Math.max(-maxV, Math.min(maxV, n.vy))
        n.vz = Math.max(-maxV, Math.min(maxV, n.vz))
        n.x += n.vx
        n.y += n.vy
        n.z += n.vz
      }

      simAlphaRef.current = Math.max(0.18, simAlphaRef.current * 0.992)
    }

    const drawNebulaFrame = (nowMs: number) => {
      const canvas = canvasRef.current
      if (!canvas) return
      const rect = canvas.getBoundingClientRect()
      if (rect.width === 0 || rect.height === 0) return

      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      const targetW = Math.round(rect.width * dpr)
      const targetH = Math.round(rect.height * dpr)
      if (canvas.width !== targetW || canvas.height !== targetH) {
        canvas.width = targetW
        canvas.height = targetH
      }

      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

      const W = rect.width
      const H = rect.height
      const rs = renderStateRef.current
      const ps = pointerStateRef.current
      const cam = camRef.current

      // Smooth camera interpolation + gentle auto-orbit
      if (rs.autoOrbit && !ps.draggedNodeId && !ps.isOrbitDragging) {
        cam.targetYaw += 0.0012
      }
      cam.yaw += (cam.targetYaw - cam.yaw) * 0.12
      cam.pitch += (cam.targetPitch - cam.pitch) * 0.12
      cam.distance += (cam.targetDistance - cam.distance) * 0.12
      cam.tx += (cam.targetTx - cam.tx) * 0.12
      cam.ty += (cam.targetTy - cam.ty) * 0.12
      cam.tz += (cam.targetTz - cam.tz) * 0.12

      // 1. Deep Obsidian Space Backdrop
      const bgGrad = ctx.createRadialGradient(
        W * 0.5,
        H * 0.45,
        20,
        W * 0.5,
        H * 0.5,
        Math.max(W, H) * 0.72
      )
      bgGrad.addColorStop(0, '#0d152a')
      bgGrad.addColorStop(0.58, '#060a16')
      bgGrad.addColorStop(1, '#02040a')
      ctx.fillStyle = bgGrad
      ctx.fillRect(0, 0, W, H)

      // 2. 3D Starfield Motes
      for (let i = 0; i < STARFIELD.length; i += 1) {
        const s = STARFIELD[i]
        const p = projectPoint(s.x, s.y, s.z, W, H, cam)
        if (!p) continue
        const alpha = Math.max(0.03, Math.min(0.45, s.a * p.scale))
        ctx.fillStyle = `rgba(203, 213, 225, ${alpha})`
        ctx.beginPath()
        ctx.arc(p.sx, p.sy, Math.max(0.5, s.r * p.scale), 0, Math.PI * 2)
        ctx.fill()
      }

      // 3. Subtle 3D Spatial Floor Plane Grid
      ctx.save()
      ctx.strokeStyle = 'rgba(148, 163, 184, 0.06)'
      ctx.lineWidth = 1
      const gridY = 175
      const gridLimit = 260
      const gridStep = 65
      for (let g = -gridLimit; g <= gridLimit; g += gridStep) {
        const a = projectPoint(g, gridY, -gridLimit, W, H, cam)
        const b = projectPoint(g, gridY, gridLimit, W, H, cam)
        if (a && b) {
          ctx.beginPath()
          ctx.moveTo(a.sx, a.sy)
          ctx.lineTo(b.sx, b.sy)
          ctx.stroke()
        }
        const c = projectPoint(-gridLimit, gridY, g, W, H, cam)
        const d = projectPoint(gridLimit, gridY, g, W, H, cam)
        if (c && d) {
          ctx.beginPath()
          ctx.moveTo(c.sx, c.sy)
          ctx.lineTo(d.sx, d.sy)
          ctx.stroke()
        }
      }
      ctx.restore()

      // 4. Project Active Nodes & Compute Emerged Cluster Centroids (>= 3 items)
      const projectedNodes = new Map<string, ProjectedNodeInfo>()
      const renderQueue: Array<ProjectedNodeInfo | ProjectedHubInfo> = []
      const clusterCentroids = new Map<
        string,
        { x: number; y: number; z: number; count: number }
      >()

      for (let i = 0; i < rs.activeNodes.length; i += 1) {
        const sim = rs.activeNodes[i]
        const p = projectPoint(sim.x, sim.y, sim.z, W, H, cam)
        if (p) {
          // Cap sub-node sizes cleanly to 3.0–6.0px (scaled gently by reach & user scale)
          const reachRaw =
            3.0 + Math.min(3.0, Math.log10(Math.max(1, sim.data.reach ?? 10) + 1) * 0.45)
          const radius = Math.max(
            3.0,
            Math.min(6.0, reachRaw * rs.nodeSizeScale) * Math.max(0.75, Math.min(1.35, p.scale))
          )
          const info: ProjectedNodeInfo = {
            kind: 'node',
            node: sim,
            sx: p.sx,
            sy: p.sy,
            cz: p.cz,
            scale: p.scale,
            radius
          }
          projectedNodes.set(sim.data.id, info)
          renderQueue.push(info)
        }

        if (rs.clusterByStoryline.has(sim.storylineId)) {
          const acc = clusterCentroids.get(sim.storylineId)
          if (acc) {
            acc.x += sim.x
            acc.y += sim.y
            acc.z += sim.z
            acc.count += 1
          } else {
            clusterCentroids.set(sim.storylineId, {
              x: sim.x,
              y: sim.y,
              z: sim.z,
              count: 1
            })
          }
        }
      }

      // Compute Emerged Hub Spheres & Pill Geometry (count >= 3 only!)
      const projectedHubs: ProjectedHubInfo[] = []
      const hubByStorylineId = new Map<string, ProjectedHubInfo>()
      ctx.font = '600 11px Inter, -apple-system, BlinkMacSystemFont, sans-serif'

      for (const [sid, acc] of clusterCentroids.entries()) {
        const meta = rs.clusterByStoryline.get(sid)
        if (!meta || acc.count < CLUSTER_MIN_ITEMS) continue
        const cx = acc.x / acc.count
        const cy = acc.y / acc.count
        const cz = acc.z / acc.count
        const p = projectPoint(cx, cy, cz, W, H, cam)
        if (!p) continue

        const dimmed = rs.focusedStorylineId !== null && rs.focusedStorylineId !== sid

        // 5. 3D Storyline Cluster Nebula Halo
        if (rs.showHalos) {
          const haloR = Math.min(185, Math.max(60, (62 + Math.sqrt(acc.count) * 14) * p.scale))
          const haloGrad = ctx.createRadialGradient(p.sx, p.sy, 4, p.sx, p.sy, haloR)
          haloGrad.addColorStop(0, hexToRgba(meta.colour, dimmed ? 0.025 : 0.12))
          haloGrad.addColorStop(1, hexToRgba(meta.colour, 0))
          ctx.fillStyle = haloGrad
          ctx.beginPath()
          ctx.arc(p.sx, p.sy, haloR, 0, Math.PI * 2)
          ctx.fill()
        }

        const baseHubR = Math.min(20, 8 + Math.log2(Math.max(2, acc.count - 1)) * 2.8)
        const radius = Math.max(
          8,
          Math.min(22, baseHubR * Math.max(0.75, Math.min(1.3, p.scale)))
        )
        const pillText = formatHubPillLabel(meta.name, acc.count)
        const pillW = Math.max(88, Math.round(ctx.measureText(pillText).width + 28))
        const pillH = 22
        const pillX = p.sx - pillW / 2
        const pillY = p.sy + radius + 9

        const hubInfo: ProjectedHubInfo = {
          kind: 'hub',
          storylineId: sid,
          name: meta.name,
          count: acc.count,
          colour: meta.colour,
          sx: p.sx,
          sy: p.sy,
          cz: p.cz,
          scale: p.scale,
          radius,
          dimmed,
          pillX,
          pillY,
          pillW,
          pillH,
          pillText
        }
        projectedHubs.push(hubInfo)
        hubByStorylineId.set(sid, hubInfo)
        renderQueue.push(hubInfo)
      }

      projectedNodesRef.current = projectedNodes
      projectedHubsRef.current = projectedHubs

      // Compute hovered neighbor set
      const hoveredNeighbors = new Set<string>()
      if (ps.hoveredNodeId) {
        hoveredNeighbors.add(ps.hoveredNodeId)
        for (let i = 0; i < rs.activeEdges.length; i += 1) {
          const e = rs.activeEdges[i]
          if (e.source === ps.hoveredNodeId) hoveredNeighbors.add(e.target)
          if (e.target === ps.hoveredNodeId) hoveredNeighbors.add(e.source)
        }
      }

      // 6. Edges (Cluster spokes + Similarity bridges + Graph links)
      if (rs.showLinks) {
        // Subtle spoke lines from emerged hub to member nodes
        for (let i = 0; i < rs.activeNodes.length; i += 1) {
          const sim = rs.activeNodes[i]
          const hub = hubByStorylineId.get(sim.storylineId)
          const nodeP = projectedNodes.get(sim.data.id)
          if (!hub || !nodeP) continue

          const meta = rs.clusterByStoryline.get(sim.storylineId)
          const pull = meta?.pull ?? 0.6
          const isHubHover = ps.hoveredHubId === sim.storylineId
          const isNodeHover = ps.hoveredNodeId === sim.data.id
          let spokeAlpha = (0.06 + 0.1 * pull) * (rs.focusedStorylineId === sim.storylineId ? 1.4 : 1)
          if (hub.dimmed) spokeAlpha = 0.025
          else if (ps.hoveredHubId && !isHubHover) spokeAlpha = 0.03
          else if (isHubHover || isNodeHover) spokeAlpha = 0.42

          ctx.strokeStyle = hexToRgba(hub.colour, spokeAlpha)
          ctx.lineWidth = isHubHover || isNodeHover ? 1.2 : 0.7
          ctx.beginPath()
          ctx.moveTo(hub.sx, hub.sy)
          ctx.lineTo(nodeP.sx, nodeP.sy)
          ctx.stroke()
        }

        // Graph & similarity bridge edges
        for (let i = 0; i < rs.activeEdges.length; i += 1) {
          const e = rs.activeEdges[i]
          const srcP = projectedNodes.get(e.source)
          const dstP = projectedNodes.get(e.target)
          if (!srcP || !dstP) continue

          const srcDim =
            rs.focusedStorylineId !== null && srcP.node.storylineId !== rs.focusedStorylineId
          const dstDim =
            rs.focusedStorylineId !== null && dstP.node.storylineId !== rs.focusedStorylineId
          const isHoverActive = ps.hoveredNodeId !== null || ps.hoveredHubId !== null
          const isIncident =
            (ps.hoveredNodeId !== null &&
              (e.source === ps.hoveredNodeId || e.target === ps.hoveredNodeId)) ||
            (ps.hoveredHubId !== null &&
              (srcP.node.storylineId === ps.hoveredHubId ||
                dstP.node.storylineId === ps.hoveredHubId))

          let alpha = rs.linkOpacity
          if (srcDim || dstDim) alpha *= 0.14
          else if (isHoverActive && !isIncident) alpha *= 0.18
          else if (isIncident) alpha = 0.85

          ctx.save()
          ctx.lineCap = 'round'
          if (e.type === 'similarity' && e.colour) {
            ctx.strokeStyle = hexToRgba(e.colour, srcDim || dstDim ? 0.06 : isIncident ? 0.75 : 0.42)
            ctx.lineWidth = isIncident ? 1.5 : 1.0
            ctx.beginPath()
            ctx.moveTo(srcP.sx, srcP.sy)
            ctx.lineTo(dstP.sx, dstP.sy)
            ctx.stroke()
          } else if (e.type === 'contradicts') {
            ctx.strokeStyle = `rgba(250, 204, 21, ${alpha})`
            ctx.fillStyle = ctx.strokeStyle
            ctx.lineWidth = isIncident ? 2.0 : 1.35
            ctx.setLineDash([2, 4])
            const mx = (srcP.sx + dstP.sx) * 0.5
            const my = (srcP.sy + dstP.sy) * 0.5
            const dx = dstP.sx - srcP.sx
            const dy = dstP.sy - srcP.sy
            const cx = mx - dy * 0.12
            const cy = my + dx * 0.12
            ctx.beginPath()
            ctx.moveTo(srcP.sx, srcP.sy)
            ctx.quadraticCurveTo(cx, cy, dstP.sx, dstP.sy)
            ctx.stroke()
            if (rs.showArrows) {
              ctx.setLineDash([])
              drawArrowHead(ctx, cx, cy, dstP.sx, dstP.sy, dstP.radius, 5)
            }
          } else if (e.type === 'links_to') {
            ctx.strokeStyle = `rgba(203, 213, 225, ${Math.min(0.75, alpha * 0.95)})`
            ctx.fillStyle = ctx.strokeStyle
            ctx.lineWidth = isIncident ? 1.8 : 1.05
            ctx.setLineDash([5, 4])
            const mx = (srcP.sx + dstP.sx) * 0.5
            const my = (srcP.sy + dstP.sy) * 0.5
            const dx = dstP.sx - srcP.sx
            const dy = dstP.sy - srcP.sy
            const cx = mx - dy * 0.12
            const cy = my + dx * 0.12
            ctx.beginPath()
            ctx.moveTo(srcP.sx, srcP.sy)
            ctx.quadraticCurveTo(cx, cy, dstP.sx, dstP.sy)
            ctx.stroke()
            if (rs.showArrows) {
              ctx.setLineDash([])
              drawArrowHead(ctx, cx, cy, dstP.sx, dstP.sy, dstP.radius, 5)
            }
          } else {
            ctx.strokeStyle = `rgba(148, 163, 184, ${alpha * 0.62})`
            ctx.fillStyle = ctx.strokeStyle
            ctx.lineWidth = isIncident ? 1.4 : 0.75
            ctx.beginPath()
            ctx.moveTo(srcP.sx, srcP.sy)
            ctx.lineTo(dstP.sx, dstP.sy)
            ctx.stroke()
            if (rs.showArrows && (isIncident || rs.localMode)) {
              drawArrowHead(ctx, srcP.sx, srcP.sy, dstP.sx, dstP.sy, dstP.radius, 4.2)
            }
          }
          ctx.restore()
        }
      }

      // 7. Sort Spheres & Nodes Back-to-Front (3D Painter's Algorithm)
      renderQueue.sort((a, b) => b.cz - a.cz)
      const zoomFactor = 520 / Math.max(150, cam.distance)
      const hasSearch = rs.searchQuery.trim().length > 0

      for (let i = 0; i < renderQueue.length; i += 1) {
        const entry = renderQueue[i]

        if (entry.kind === 'hub') {
          const isHovered = ps.hoveredHubId === entry.storylineId
          const isFocused = rs.focusedStorylineId === entry.storylineId
          const hubAlpha = entry.dimmed
            ? 0.15
            : ps.hoveredHubId && !isHovered
              ? 0.35
              : 0.96

          ctx.save()
          ctx.globalAlpha = hubAlpha

          // Outer glow ring
          const glowR = entry.radius * 2.1
          const grad = ctx.createRadialGradient(
            entry.sx,
            entry.sy,
            entry.radius * 0.2,
            entry.sx,
            entry.sy,
            glowR
          )
          grad.addColorStop(0, hexToRgba(entry.colour, 0.38))
          grad.addColorStop(1, hexToRgba(entry.colour, 0))
          ctx.fillStyle = grad
          ctx.beginPath()
          ctx.arc(entry.sx, entry.sy, glowR, 0, Math.PI * 2)
          ctx.fill()

          // Core 3D hub sphere
          ctx.fillStyle = entry.colour
          ctx.strokeStyle = '#0b1120'
          ctx.lineWidth = 1.6
          ctx.beginPath()
          ctx.arc(entry.sx, entry.sy, entry.radius, 0, Math.PI * 2)
          ctx.fill()
          ctx.stroke()

          // 3D specular highlight
          ctx.fillStyle = 'rgba(255, 255, 255, 0.38)'
          ctx.beginPath()
          ctx.arc(
            entry.sx - entry.radius * 0.28,
            entry.sy - entry.radius * 0.28,
            Math.max(1.5, entry.radius * 0.26),
            0,
            Math.PI * 2
          )
          ctx.fill()

          if (isFocused || isHovered) {
            ctx.strokeStyle = isFocused ? '#f8fafc' : '#e2e8f0'
            ctx.lineWidth = 2
            ctx.beginPath()
            ctx.arc(entry.sx, entry.sy, entry.radius + 4, 0, Math.PI * 2)
            ctx.stroke()
          }

          ctx.restore()
          continue
        }

        // Data Point Sub-Node
        const sim = entry.node
        const n = sim.data
        const sid = sim.storylineId
        const stColour = getStorylineColour(sid, rs.storylineColourIndexMap.get(sid))

        const isRecent = rs.currentHour - n.hour >= 0 && rs.currentHour - n.hour < 2
        const isNewArrival = n.hour === rs.currentHour
        const isHovered = ps.hoveredNodeId === n.id
        const isSelected = rs.selectedNodeId === n.id
        const isSearchMatch = hasSearch && rs.searchMatches.has(n.id)

        const dimmedByStoryline = rs.focusedStorylineId !== null && sid !== rs.focusedStorylineId
        const dimmedByHover =
          (ps.hoveredNodeId !== null && !hoveredNeighbors.has(n.id)) ||
          (ps.hoveredHubId !== null && sid !== ps.hoveredHubId)
        const dimmedBySearch = hasSearch && !isSearchMatch

        const depthAlpha = Math.max(0.48, Math.min(1, 0.38 + entry.scale * 0.62))
        let nodeAlpha = depthAlpha
        if (dimmedByStoryline) nodeAlpha = 0.13
        else if (dimmedByHover) nodeAlpha = 0.18
        else if (dimmedBySearch) nodeAlpha = 0.14

        // Preserve storyline colour even on <2h recent nodes (use white outer glow ring for recency)
        const fillColour = n.is_response ? RESPONSE_GOLD_COLOUR : stColour

        ctx.save()
        ctx.globalAlpha = nodeAlpha

        // <2h recent white outer glow ring OR Response/Selected ambient halo
        if ((isRecent || n.is_response || isSelected || isSearchMatch) && !dimmedByStoryline && !dimmedByHover) {
          const glowR = entry.radius * (n.is_response ? 2.8 : 2.3)
          const g = ctx.createRadialGradient(
            entry.sx,
            entry.sy,
            entry.radius * 0.3,
            entry.sx,
            entry.sy,
            glowR
          )
          const glowHex = n.is_response ? RESPONSE_GOLD_COLOUR : isSelected ? '#38bdf8' : '#f8fafc'
          g.addColorStop(0, hexToRgba(glowHex, 0.34))
          g.addColorStop(1, hexToRgba(glowHex, 0))
          ctx.fillStyle = g
          ctx.beginPath()
          ctx.arc(entry.sx, entry.sy, glowR, 0, Math.PI * 2)
          ctx.fill()

          if (isRecent && !n.is_response) {
            ctx.strokeStyle = 'rgba(248, 250, 252, 0.88)'
            ctx.lineWidth = 1.25
            ctx.beginPath()
            ctx.arc(entry.sx, entry.sy, entry.radius + 2.4, 0, Math.PI * 2)
            ctx.stroke()
          }
        }

        // New arrival single ripple (FR-3.7)
        if (isNewArrival && sim.rippleStart > 0 && !dimmedByStoryline) {
          const elapsed = (nowMs - sim.rippleStart) / 1400
          if (elapsed >= 0 && elapsed < 1) {
            const rr = entry.radius + 4 + elapsed * 16
            ctx.strokeStyle = hexToRgba('#f8fafc', (1 - elapsed) * 0.82)
            ctx.lineWidth = 1.4
            ctx.beginPath()
            ctx.arc(entry.sx, entry.sy, rr, 0, Math.PI * 2)
            ctx.stroke()
          }
        }

        // Disclosed conflict gold outer ring (P3.2)
        if (n.conflict) {
          ctx.strokeStyle = '#facc15'
          ctx.lineWidth = 1.7
          ctx.beginPath()
          ctx.arc(entry.sx, entry.sy, entry.radius + 3.2, 0, Math.PI * 2)
          ctx.stroke()
        }

        // Official / Response diamond vs 3D sphere bead
        if (n.is_official || n.is_response) {
          const d = entry.radius + 2.0
          ctx.fillStyle = n.is_response ? '#facc15' : '#f8fafc'
          ctx.strokeStyle = '#090f1f'
          ctx.lineWidth = 1.3
          ctx.beginPath()
          ctx.moveTo(entry.sx, entry.sy - d)
          ctx.lineTo(entry.sx + d, entry.sy)
          ctx.lineTo(entry.sx, entry.sy + d)
          ctx.lineTo(entry.sx - d, entry.sy)
          ctx.closePath()
          ctx.fill()
          ctx.stroke()
        } else {
          ctx.fillStyle = fillColour
          ctx.beginPath()
          ctx.arc(entry.sx, entry.sy, entry.radius, 0, Math.PI * 2)
          ctx.fill()

          // Specular highlight dot for 3D floating bead feel
          ctx.fillStyle = 'rgba(255, 255, 255, 0.32)'
          ctx.beginPath()
          ctx.arc(
            entry.sx - entry.radius * 0.28,
            entry.sy - entry.radius * 0.28,
            Math.max(0.7, entry.radius * 0.26),
            0,
            Math.PI * 2
          )
          ctx.fill()
        }

        // Hover / Selection focus ring
        if (isSelected || isHovered || isSearchMatch) {
          ctx.strokeStyle = isSelected ? '#38bdf8' : isSearchMatch ? '#2dd4bf' : '#e2e8f0'
          ctx.lineWidth = isSelected ? 2.1 : 1.5
          ctx.beginPath()
          ctx.arc(entry.sx, entry.sy, entry.radius + 4.8, 0, Math.PI * 2)
          ctx.stroke()
        }

        // Obsidian Semantic-Zoom Node Labels
        const showImportant =
          n.is_official ||
          Boolean(n.is_response) ||
          n.conflict !== null ||
          isHovered ||
          isSelected ||
          isSearchMatch ||
          rs.localMode
        const thresholdMatch = zoomFactor * (entry.radius / 5) >= rs.textFadeThreshold
        if ((showImportant || thresholdMatch) && nodeAlpha > 0.28) {
          ctx.font = `${n.is_official || isHovered || isSelected ? '600 11.5px' : '500 10.5px'} Inter, -apple-system, BlinkMacSystemFont, sans-serif`
          ctx.textAlign = 'center'
          ctx.fillStyle = `rgba(241, 245, 249, ${Math.min(0.95, nodeAlpha + 0.1)})`
          ctx.fillText(n.label, entry.sx, entry.sy + entry.radius + 13)
        }

        ctx.restore()
      }

      // 8. Emerged Storyline Hub Label Pills (always crisp on top of edges & spheres, count >= 3 only)
      const sortedPills = [...projectedHubs].sort((a, b) => b.cz - a.cz)
      for (let i = 0; i < sortedPills.length; i += 1) {
        const hub = sortedPills[i]
        const isFocused = rs.focusedStorylineId === hub.storylineId
        const isHovered = ps.hoveredHubId === hub.storylineId
        const pillAlpha = hub.dimmed ? 0.15 : 1

        ctx.save()
        ctx.globalAlpha = pillAlpha

        ctx.fillStyle = 'rgba(11, 17, 32, 0.88)'
        ctx.strokeStyle = hexToRgba(hub.colour, isFocused || isHovered ? 0.95 : 0.62)
        ctx.lineWidth = isFocused || isHovered ? 1.5 : 1
        ctx.beginPath()
        ctx.roundRect(hub.pillX, hub.pillY, hub.pillW, hub.pillH, 11)
        ctx.fill()
        ctx.stroke()

        // Colour indicator dot inside pill
        ctx.fillStyle = hub.colour
        ctx.beginPath()
        ctx.arc(hub.pillX + 11, hub.pillY + hub.pillH / 2, 3.5, 0, Math.PI * 2)
        ctx.fill()

        ctx.font = '600 11px Inter, -apple-system, BlinkMacSystemFont, sans-serif'
        ctx.textAlign = 'left'
        ctx.textBaseline = 'middle'
        ctx.fillStyle = isFocused || isHovered ? '#ffffff' : '#e2e8f0'
        ctx.fillText(hub.pillText, hub.pillX + 20, hub.pillY + hub.pillH / 2 + 0.5)

        ctx.restore()
      }
    }

    const tick = (nowMs: number) => {
      stepPhysics3D(nowMs)
      drawNebulaFrame(nowMs)
      rafId = window.requestAnimationFrame(tick)
    }
    rafId = window.requestAnimationFrame(tick)
    return () => window.cancelAnimationFrame(rafId)
  }, [])

  // Hit testing helper (Label Pills -> Closest Hub or Node by cz)
  const hitTestAt = useCallback(
    (sx: number, sy: number):
      | { kind: 'hub'; hub: ProjectedHubInfo }
      | { kind: 'node'; info: ProjectedNodeInfo }
      | null => {
      const hubs = projectedHubsRef.current
      // Check front-most pills first
      const frontPills = [...hubs].sort((a, b) => a.cz - b.cz)
      for (let i = 0; i < frontPills.length; i += 1) {
        const h = frontPills[i]
        if (
          sx >= h.pillX &&
          sx <= h.pillX + h.pillW &&
          sy >= h.pillY &&
          sy <= h.pillY + h.pillH
        ) {
          return { kind: 'hub', hub: h }
        }
      }

      let best: { kind: 'hub'; hub: ProjectedHubInfo } | { kind: 'node'; info: ProjectedNodeInfo } | null =
        null
      let bestCz = Infinity

      for (let i = 0; i < hubs.length; i += 1) {
        const h = hubs[i]
        const dx = sx - h.sx
        const dy = sy - h.sy
        const hitR = h.radius + 5
        if (dx * dx + dy * dy <= hitR * hitR && h.cz < bestCz) {
          bestCz = h.cz
          best = { kind: 'hub', hub: h }
        }
      }

      for (const info of projectedNodesRef.current.values()) {
        const dx = sx - info.sx
        const dy = sy - info.sy
        const hitR = Math.max(9, info.radius + 5)
        if (dx * dx + dy * dy <= hitR * hitR && info.cz < bestCz) {
          bestCz = info.cz
          best = { kind: 'node', info }
        }
      }

      return best
    },
    []
  )

  const moveNodeInCameraPlane = useCallback(
    (sim: SimNode, dxScreen: number, dyScreen: number, scaleAtNode: number) => {
      const cam = camRef.current
      const cosY = Math.cos(cam.yaw)
      const sinY = Math.sin(cam.yaw)
      const cosP = Math.cos(cam.pitch)
      const sinP = Math.sin(cam.pitch)

      const worldDx = dxScreen / Math.max(0.25, scaleAtNode)
      const worldDy = dyScreen / Math.max(0.25, scaleAtNode)
      const y1 = worldDy * cosP
      const z1 = -worldDy * sinP

      sim.x += worldDx * cosY + z1 * sinY
      sim.y += y1
      sim.z += -worldDx * sinY + z1 * cosY
    },
    []
  )

  const handlePointerDown = (evt: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const sx = evt.clientX - rect.left
    const sy = evt.clientY - rect.top
    const ps = pointerStateRef.current
    ps.lastX = sx
    ps.lastY = sy
    ps.movedDistance = 0

    const hit = hitTestAt(sx, sy)
    if (hit && hit.kind === 'node' && evt.button === 0 && !evt.shiftKey) {
      ps.draggedNodeId = hit.info.node.data.id
      hit.info.node.pinned = true
      simAlphaRef.current = Math.max(simAlphaRef.current, 0.5)
      canvas.style.cursor = 'grabbing'
    } else if (evt.shiftKey || evt.button === 2) {
      ps.isPanDragging = true
      canvas.style.cursor = 'move'
    } else {
      ps.isOrbitDragging = true
      canvas.style.cursor = 'grabbing'
    }
    canvas.setPointerCapture(evt.pointerId)
  }

  const handlePointerMove = (evt: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const sx = evt.clientX - rect.left
    const sy = evt.clientY - rect.top
    const ps = pointerStateRef.current
    const dx = sx - ps.lastX
    const dy = sy - ps.lastY
    ps.movedDistance += Math.hypot(dx, dy)
    ps.lastX = sx
    ps.lastY = sy

    if (ps.draggedNodeId) {
      const sim = simNodeStoreRef.current.get(ps.draggedNodeId)
      const proj = projectedNodesRef.current.get(ps.draggedNodeId)
      if (sim && proj) {
        moveNodeInCameraPlane(sim, dx, dy, proj.scale)
        simAlphaRef.current = Math.max(simAlphaRef.current, 0.45)
      }
      setHoverTarget(null)
      return
    }

    if (ps.isOrbitDragging) {
      camRef.current.targetYaw += dx * 0.0065
      camRef.current.targetPitch = Math.max(
        -1.35,
        Math.min(1.35, camRef.current.targetPitch + dy * 0.0055)
      )
      setHoverTarget(null)
      return
    }

    if (ps.isPanDragging) {
      const factor = camRef.current.distance / 520
      const cosY = Math.cos(camRef.current.yaw)
      const sinY = Math.sin(camRef.current.yaw)
      camRef.current.targetTx -= dx * factor * cosY
      camRef.current.targetTz += dx * factor * sinY
      camRef.current.targetTy -= dy * factor
      setHoverTarget(null)
      return
    }

    const hit = hitTestAt(sx, sy)
    if (hit?.kind === 'hub') {
      ps.hoveredNodeId = null
      ps.hoveredHubId = hit.hub.storylineId
      canvas.style.cursor = 'pointer'
      setHoverTarget({
        kind: 'hub',
        storylineId: hit.hub.storylineId,
        name: hit.hub.name,
        count: hit.hub.count,
        colour: hit.hub.colour,
        sx,
        sy
      })
    } else if (hit?.kind === 'node') {
      ps.hoveredHubId = null
      ps.hoveredNodeId = hit.info.node.data.id
      canvas.style.cursor = 'pointer'
      setHoverTarget({
        kind: 'node',
        node: hit.info.node,
        sx,
        sy
      })
    } else {
      ps.hoveredHubId = null
      ps.hoveredNodeId = null
      canvas.style.cursor = 'grab'
      setHoverTarget(null)
    }
  }

  const handlePointerUp = (evt: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const sx = evt.clientX - rect.left
    const sy = evt.clientY - rect.top
    const ps = pointerStateRef.current

    if (ps.draggedNodeId) {
      const sim = simNodeStoreRef.current.get(ps.draggedNodeId)
      if (sim && !evt.shiftKey) {
        sim.pinned = false
      }
      ps.draggedNodeId = null
    }
    ps.isOrbitDragging = false
    ps.isPanDragging = false
    canvas.style.cursor = 'grab'
    if (canvas.hasPointerCapture(evt.pointerId)) {
      canvas.releasePointerCapture(evt.pointerId)
    }

    if (ps.movedDistance < 5) {
      const hit = hitTestAt(sx, sy)
      const now = performance.now()
      if (hit?.kind === 'hub') {
        triggerHubFocus(hit.hub.storylineId)
        ps.lastClickNodeId = null
      } else if (hit?.kind === 'node') {
        const nodeId = hit.info.node.data.id
        if (ps.lastClickNodeId === nodeId && now - ps.lastClickTime < 340) {
          enterLocalGraph(nodeId)
          ps.lastClickNodeId = null
        } else {
          onSelectNode(hit.info.node.data)
          ps.lastClickNodeId = nodeId
          ps.lastClickTime = now
        }
      } else {
        onSelectNode(null)
      }
    }
  }

  const handleWheel = (evt: React.WheelEvent<HTMLCanvasElement>) => {
    const delta = Math.sign(evt.deltaY) * Math.min(80, Math.abs(evt.deltaY) * 0.45)
    camRef.current.targetDistance = Math.max(
      150,
      Math.min(1150, camRef.current.targetDistance + delta)
    )
  }

  const handleResetDefaults = () => {
    setSearchQuery('')
    handlePlatformReset()
    setShowUnassigned(true)
    setOfficialOnly(false)
    setConflictsOnly(false)
    setContradictionsOnly(false)
    setHiddenStorylines(new Set())
    setShowArrows(true)
    setShowLinks(true)
    setShowHalos(true)
    setAutoOrbit(true)
    setTextFadeThreshold(1.15)
    setNodeSizeScale(1.0)
    setLinkOpacity(0.35)
    setCenterForce(0.45)
    setRepelForce(11.0)
    setLinkForce(0.45)
    setLinkDistance(58)
    setPhysicsPaused(false)
    simAlphaRef.current = 0.9
    resetCameraView()
  }

  // Count active non-default filters for the badge on "Graph view"
  const activeFilterCount = useMemo(() => {
    let count = 0
    if (searchQuery.trim().length > 0) count += 1
    if (availablePlatforms.length > 0 && activePlatforms.length < availablePlatforms.length) {
      count += 1
    }
    if (!showUnassigned) count += 1
    if (officialOnly) count += 1
    if (conflictsOnly) count += 1
    if (contradictionsOnly) count += 1
    if (hiddenStorylines.size > 0) count += 1
    return count
  }, [
    searchQuery,
    availablePlatforms.length,
    activePlatforms.length,
    showUnassigned,
    officialOnly,
    conflictsOnly,
    contradictionsOnly,
    hiddenStorylines.size
  ])

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

  const clusterCount = emergedStorylines.length
  const focusedStorylineName = focusedStorylineId
    ? (storylineNameMap.get(focusedStorylineId) ?? focusedStorylineId)
    : null
  const isEmpty = activeNodes.length === 0

  // Compute clamped hover card position inside stage bounds
  const hoverCardCoords = useMemo(() => {
    if (!hoverTarget) return null
    const rect = stageRef.current?.getBoundingClientRect()
    const stageW = rect?.width ?? 800
    const stageH = rect?.height ?? 600
    const cardW = 256
    const cardH = 152
    return {
      left: Math.max(12, Math.min(stageW - cardW - 12, hoverTarget.sx + 16)),
      top: Math.max(12, Math.min(stageH - cardH - 12, hoverTarget.sy - 20))
    }
  }, [hoverTarget])

  // Storylines list for the Controls Drawer Groups accordion
  const drawerStorylineRows = useMemo(() => {
    const ids = new Set<string>(storylines.map((s) => s.id))
    for (const sid of allStorylineCounts.keys()) ids.add(sid)
    return Array.from(ids).map((id) => ({
      id,
      name: storylineNameMap.get(id) ?? id,
      colour: getStorylineColour(id, storylineColourIndexMap.get(id)),
      count: allStorylineCounts.get(id) ?? 0
    }))
  }, [storylines, allStorylineCounts, storylineNameMap, storylineColourIndexMap])

  const allPlatformsSelected =
    availablePlatforms.length > 0 && activePlatforms.length === availablePlatforms.length

  return (
    <section
      ref={stageRef}
      className="vf-graph-stage"
      aria-label="3D Obsidian Narrative Nebula canvas"
    >
      <canvas
        ref={canvasRef}
        className="vf-nebula-canvas"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerLeave={() => {
          pointerStateRef.current.hoveredHubId = null
          pointerStateRef.current.hoveredNodeId = null
          setHoverTarget(null)
        }}
        onWheel={handleWheel}
        onContextMenu={(e) => e.preventDefault()}
      />

      {/* Top-Left: Graph View Drawer Toggle, Reset View & Media Platform Quick Filter Bar */}
      <div className="vf-graph-top-left">
        <div className="vf-graph-controls-row">
          <button
            type="button"
            className="vf-overlay-btn vf-drawer-toggle-btn"
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen((prev) => !prev)}
          >
            <span aria-hidden="true">⚙</span>
            <span>Graph view</span>
            {activeFilterCount > 0 ? (
              <span className="vf-filter-badge">{activeFilterCount}</span>
            ) : null}
          </button>

          <button
            type="button"
            className="vf-overlay-btn"
            onClick={resetCameraView}
            title="Reset 3D camera orbit and zoom"
          >
            Reset view
          </button>

          {/* Media Platform Filter Pill Bar (synced with Storylines rail) */}
          {availablePlatforms.length > 0 ? (
            <div className="vf-platform-quick-bar" role="group" aria-label="Filter by media platform">
              <button
                type="button"
                className={`vf-platform-pill ${allPlatformsSelected ? 'is-active' : ''}`}
                onClick={handlePlatformReset}
              >
                All
              </button>
              {availablePlatforms.map((plat) => {
                const isActive = activePlatforms.includes(plat)
                const count = platformCountsAtHour.get(plat) ?? 0
                return (
                  <button
                    key={plat}
                    type="button"
                    aria-pressed={isActive}
                    className={`vf-platform-pill ${isActive ? 'is-active' : ''}`}
                    onClick={() => handlePlatformToggle(plat)}
                  >
                    <span>{formatPlatformLabel(plat)}</span>
                    <span className="vf-platform-pill-count">{count}</span>
                  </button>
                )
              })}
            </div>
          ) : null}
        </div>

        {/* Collapsible Obsidian Controls Drawer */}
        {drawerOpen ? (
          <aside className="vf-obsidian-drawer" aria-label="Obsidian graph controls">
            <div className="vf-drawer-header">
              <span>Graph Controls</span>
              <button
                type="button"
                className="vf-drawer-reset-link"
                onClick={handleResetDefaults}
              >
                Reset defaults
              </button>
            </div>

            {/* 1. Filters */}
            <details className="vf-accordion" open>
              <summary className="vf-accordion-summary">
                <span>Filters</span>
                <span className="vf-chevron">▸</span>
              </summary>
              <div className="vf-accordion-body">
                <div className="vf-search-wrap">
                  <input
                    type="text"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder="Search nodes, handles, topics..."
                    className="vf-drawer-search"
                  />
                  {searchQuery.trim().length > 0 ? (
                    <span className="vf-search-count">{searchMatches.size}</span>
                  ) : null}
                  {searchQuery.length > 0 ? (
                    <button
                      type="button"
                      className="vf-search-clear"
                      title="Clear search"
                      onClick={() => setSearchQuery('')}
                    >
                      ×
                    </button>
                  ) : null}
                </div>

                <div className="vf-drawer-subsection">
                  <div className="vf-setting-subtitle">Platforms (syncs Storylines)</div>
                  <div className="vf-drawer-chips">
                    {availablePlatforms.map((plat) => {
                      const active = activePlatforms.includes(plat)
                      return (
                        <button
                          key={plat}
                          type="button"
                          className={`vf-drawer-chip ${active ? 'is-active' : ''}`}
                          onClick={() => handlePlatformToggle(plat)}
                        >
                          {formatPlatformLabel(plat)}
                        </button>
                      )
                    })}
                  </div>
                </div>

                <label className="vf-setting-check">
                  <input
                    type="checkbox"
                    checked={showUnassigned}
                    onChange={(e) => setShowUnassigned(e.target.checked)}
                  />
                  <span>Show unassigned</span>
                </label>
                <label className="vf-setting-check">
                  <input
                    type="checkbox"
                    checked={officialOnly}
                    onChange={(e) => setOfficialOnly(e.target.checked)}
                  />
                  <span>Official (self-reported) only</span>
                </label>
                <label className="vf-setting-check">
                  <input
                    type="checkbox"
                    checked={conflictsOnly}
                    onChange={(e) => setConflictsOnly(e.target.checked)}
                  />
                  <span>Disclosed conflicts only</span>
                </label>
                <label className="vf-setting-check">
                  <input
                    type="checkbox"
                    checked={contradictionsOnly}
                    onChange={(e) => setContradictionsOnly(e.target.checked)}
                  />
                  <span>Contradiction links only</span>
                </label>
              </div>
            </details>

            {/* 2. Groups (Storylines) */}
            <details className="vf-accordion">
              <summary className="vf-accordion-summary">
                <span>Groups (Storylines)</span>
                <span className="vf-chevron">▸</span>
              </summary>
              <div className="vf-accordion-body">
                {drawerStorylineRows.map((row) => {
                  const isFocused = focusedStorylineId === row.id
                  const isHidden = hiddenStorylines.has(row.id)
                  return (
                    <div
                      key={row.id}
                      className={`vf-drawer-group-row ${isFocused ? 'is-focused' : ''}`}
                    >
                      <button
                        type="button"
                        className="vf-drawer-group-btn"
                        onClick={() => triggerHubFocus(row.id)}
                      >
                        <span
                          className="vf-colour-dot"
                          style={{ backgroundColor: row.colour }}
                          aria-hidden="true"
                        />
                        <span className={isHidden ? 'vf-strike' : ''}>{row.name}</span>
                      </button>
                      <div className="vf-drawer-group-actions">
                        <span className="vf-drawer-group-count">{row.count}</span>
                        <button
                          type="button"
                          className="vf-drawer-vis-btn"
                          onClick={() =>
                            setHiddenStorylines((prev) => {
                              const next = new Set(prev)
                              if (next.has(row.id)) next.delete(row.id)
                              else next.add(row.id)
                              return next
                            })
                          }
                        >
                          {isHidden ? 'Off' : 'On'}
                        </button>
                      </div>
                    </div>
                  )
                })}
              </div>
            </details>

            {/* 3. Display */}
            <details className="vf-accordion">
              <summary className="vf-accordion-summary">
                <span>Display</span>
                <span className="vf-chevron">▸</span>
              </summary>
              <div className="vf-accordion-body">
                <label className="vf-setting-check vf-between">
                  <span>Directional arrows</span>
                  <input
                    type="checkbox"
                    checked={showArrows}
                    onChange={(e) => setShowArrows(e.target.checked)}
                  />
                </label>
                <label className="vf-setting-check vf-between">
                  <span>Show links</span>
                  <input
                    type="checkbox"
                    checked={showLinks}
                    onChange={(e) => setShowLinks(e.target.checked)}
                  />
                </label>
                <label className="vf-setting-check vf-between">
                  <span>3D Cluster halos</span>
                  <input
                    type="checkbox"
                    checked={showHalos}
                    onChange={(e) => setShowHalos(e.target.checked)}
                  />
                </label>
                <label className="vf-setting-check vf-between">
                  <span>Auto-orbit drift</span>
                  <input
                    type="checkbox"
                    checked={autoOrbit}
                    onChange={(e) => setAutoOrbit(e.target.checked)}
                  />
                </label>

                <label className="vf-setting-row">
                  <div className="vf-slider-head">
                    <span>Text fade threshold</span>
                    <span>{textFadeThreshold.toFixed(2)}</span>
                  </div>
                  <input
                    type="range"
                    min={0.4}
                    max={2.4}
                    step={0.05}
                    value={textFadeThreshold}
                    onChange={(e) => setTextFadeThreshold(Number(e.target.value))}
                  />
                </label>

                <label className="vf-setting-row">
                  <div className="vf-slider-head">
                    <span>Node size</span>
                    <span>{nodeSizeScale.toFixed(1)}×</span>
                  </div>
                  <input
                    type="range"
                    min={0.6}
                    max={1.8}
                    step={0.1}
                    value={nodeSizeScale}
                    onChange={(e) => setNodeSizeScale(Number(e.target.value))}
                  />
                </label>

                <label className="vf-setting-row">
                  <div className="vf-slider-head">
                    <span>Link opacity</span>
                    <span>{linkOpacity.toFixed(2)}</span>
                  </div>
                  <input
                    type="range"
                    min={0.08}
                    max={0.95}
                    step={0.02}
                    value={linkOpacity}
                    onChange={(e) => setLinkOpacity(Number(e.target.value))}
                  />
                </label>
              </div>
            </details>

            {/* 4. Forces (3D Physics) */}
            <details className="vf-accordion">
              <summary className="vf-accordion-summary">
                <span>Forces (3D Physics)</span>
                <span className="vf-chevron">▸</span>
              </summary>
              <div className="vf-accordion-body">
                <label className="vf-setting-row">
                  <div className="vf-slider-head">
                    <span>Center force</span>
                    <span>{centerForce.toFixed(2)}</span>
                  </div>
                  <input
                    type="range"
                    min={0.05}
                    max={1.0}
                    step={0.05}
                    value={centerForce}
                    onChange={(e) => {
                      setCenterForce(Number(e.target.value))
                      simAlphaRef.current = Math.max(simAlphaRef.current, 0.55)
                    }}
                  />
                </label>

                <label className="vf-setting-row">
                  <div className="vf-slider-head">
                    <span>Repel force</span>
                    <span>{repelForce.toFixed(1)}</span>
                  </div>
                  <input
                    type="range"
                    min={2}
                    max={24}
                    step={0.5}
                    value={repelForce}
                    onChange={(e) => {
                      setRepelForce(Number(e.target.value))
                      simAlphaRef.current = Math.max(simAlphaRef.current, 0.55)
                    }}
                  />
                </label>

                <label className="vf-setting-row">
                  <div className="vf-slider-head">
                    <span>Link force</span>
                    <span>{linkForce.toFixed(2)}</span>
                  </div>
                  <input
                    type="range"
                    min={0.05}
                    max={1.0}
                    step={0.05}
                    value={linkForce}
                    onChange={(e) => {
                      setLinkForce(Number(e.target.value))
                      simAlphaRef.current = Math.max(simAlphaRef.current, 0.55)
                    }}
                  />
                </label>

                <label className="vf-setting-row">
                  <div className="vf-slider-head">
                    <span>Link distance</span>
                    <span>{linkDistance}</span>
                  </div>
                  <input
                    type="range"
                    min={24}
                    max={130}
                    step={2}
                    value={linkDistance}
                    onChange={(e) => {
                      setLinkDistance(Number(e.target.value))
                      simAlphaRef.current = Math.max(simAlphaRef.current, 0.55)
                    }}
                  />
                </label>

                <div className="vf-drawer-btn-row">
                  <button
                    type="button"
                    className="vf-overlay-btn vf-btn-flex"
                    onClick={() => setPhysicsPaused((prev) => !prev)}
                  >
                    {physicsPaused ? 'Resume physics' : 'Freeze physics'}
                  </button>
                  <button
                    type="button"
                    className="vf-overlay-btn vf-btn-flex"
                    onClick={() => {
                      setPhysicsPaused(false)
                      simAlphaRef.current = 1.0
                    }}
                  >
                    Reheat 3D
                  </button>
                </div>
              </div>
            </details>
          </aside>
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
              {activeNodes.length} {activeNodes.length === 1 ? 'item' : 'items'} · {clusterCount}{' '}
              {clusterCount === 1 ? 'narrative cluster' : 'narrative clusters'}
              {emergingStorylineCount > 0 ? ` · ${emergingStorylineCount} emerging` : ''}
            </span>
          )}
        </div>
      ) : null}

      {/* Top-right: Clock & Live Readout (FR-3.6) */}
      <div className="vf-graph-clock" aria-live="polite">
        <div className="vf-clock-hour">{formatHourOffset(currentHour)}</div>
        <div className="vf-clock-time">{formatT0ShortIst(t0Utc, currentHour)}</div>
        <div className="vf-clock-counts">
          {activeNodes.length} items · {activeEdges.length} links
        </div>
      </div>

      {/* Bottom-left: Obsidian Floating HUD (Global / Local Graph + Camera Controls + Hint) */}
      <div className="vf-obsidian-hud-wrap">
        <div className="vf-obsidian-hud">
          <div className="vf-segmented">
            <button
              type="button"
              className={`vf-seg-btn ${!localMode ? 'is-active' : ''}`}
              onClick={exitLocalGraph}
            >
              Global
            </button>
            <button
              type="button"
              className={`vf-seg-btn ${localMode ? 'is-active' : ''}`}
              onClick={() => enterLocalGraph(selectedNodeId ?? localCenterId)}
            >
              Local
            </button>
          </div>

          {localMode ? (
            <div className="vf-hud-group">
              {([1, 2, 3] as const).map((depth) => (
                <button
                  key={depth}
                  type="button"
                  className={`vf-hud-chip ${localDepth === depth ? 'is-active' : ''}`}
                  onClick={() => setLocalDepth(depth)}
                >
                  {depth}-hop
                </button>
              ))}
              <button
                type="button"
                className={`vf-hud-chip ${localIncoming ? 'is-active' : ''}`}
                onClick={() => {
                  if (localIncoming && !localOutgoing) return
                  setLocalIncoming((prev) => !prev)
                }}
              >
                In
              </button>
              <button
                type="button"
                className={`vf-hud-chip ${localOutgoing ? 'is-active' : ''}`}
                onClick={() => {
                  if (localOutgoing && !localIncoming) return
                  setLocalOutgoing((prev) => !prev)
                }}
              >
                Out
              </button>
              {localCenterLabel ? (
                <span className="vf-local-center-chip">
                  <span>{localCenterLabel}</span>
                  <button
                    type="button"
                    title="Exit local graph"
                    onClick={exitLocalGraph}
                    className="vf-local-exit-x"
                  >
                    ×
                  </button>
                </span>
              ) : null}
            </div>
          ) : null}

          <div className="vf-hud-group">
            <button
              type="button"
              className="vf-hud-btn vf-mono"
              title="Zoom in"
              onClick={() => {
                camRef.current.targetDistance = Math.max(
                  150,
                  camRef.current.targetDistance - 90
                )
              }}
            >
              +
            </button>
            <button
              type="button"
              className="vf-hud-btn vf-mono"
              title="Zoom out"
              onClick={() => {
                camRef.current.targetDistance = Math.min(
                  1150,
                  camRef.current.targetDistance + 90
                )
              }}
            >
              −
            </button>
            <button
              type="button"
              className="vf-hud-btn"
              title="Fit visible points"
              onClick={() => {
                camRef.current.targetTx = 0
                camRef.current.targetTy = 0
                camRef.current.targetTz = 0
                camRef.current.targetDistance = localMode ? 270 : 500
              }}
            >
              Fit
            </button>
            <button
              type="button"
              className={`vf-hud-btn ${autoOrbit ? 'is-active' : ''}`}
              onClick={() => setAutoOrbit((prev) => !prev)}
            >
              {autoOrbit ? 'Auto-orbit' : 'Static orbit'}
            </button>
            <button
              type="button"
              className={`vf-hud-btn ${!physicsPaused ? 'is-active' : ''}`}
              onClick={() => setPhysicsPaused((prev) => !prev)}
            >
              {physicsPaused ? 'Paused' : 'Live physics'}
            </button>
          </div>
        </div>

        <div className="vf-hud-hint">
          Drag space to 3D orbit · Drag any point to pull · Scroll to zoom · Double-click for Local
          Graph
        </div>
      </div>

      {/* Cursor-tracked Glassmorphic Hover Card */}
      {hoverTarget && hoverCardCoords ? (
        <div
          className="vf-hover-card"
          role="status"
          style={{ left: `${hoverCardCoords.left}px`, top: `${hoverCardCoords.top}px` }}
        >
          {hoverTarget.kind === 'hub' ? (
            <>
              <div className="vf-hover-card-head">
                <span className="vf-hover-card-title">{hoverTarget.name}</span>
                <span className="vf-hover-card-badge">Cluster</span>
              </div>
              <div className="vf-hover-card-meta">
                {hoverTarget.count} {hoverTarget.count === 1 ? 'item' : 'items'}
              </div>
              <div className="vf-hover-card-storyline">
                <span
                  className="vf-colour-dot"
                  style={{ backgroundColor: hoverTarget.colour }}
                />
                <span>
                  {focusedStorylineId === hoverTarget.storylineId
                    ? 'Click to clear focus'
                    : 'Click to focus storyline'}
                </span>
              </div>
            </>
          ) : (
            <>
              <div className="vf-hover-card-head">
                <span className="vf-hover-card-title">{hoverTarget.node.data.label}</span>
                <span className="vf-hover-card-badge">
                  {formatPlatformLabel(hoverTarget.node.data.platform)}
                </span>
              </div>
              <div className="vf-hover-card-meta">
                {formatHourOffset(hoverTarget.node.data.hour)} · Reach{' '}
                {formatCount(hoverTarget.node.data.reach)} · {hoverTarget.node.inCount} in /{' '}
                {hoverTarget.node.outCount} out
              </div>
              <div className="vf-hover-card-storyline">
                <span
                  className="vf-colour-dot"
                  style={{
                    backgroundColor: getStorylineColour(
                      hoverTarget.node.storylineId,
                      storylineColourIndexMap.get(hoverTarget.node.storylineId)
                    )
                  }}
                />
                <span>
                  {storylineNameMap.get(hoverTarget.node.storylineId) ??
                    hoverTarget.node.storylineId}
                </span>
              </div>
              {hoverTarget.node.data.is_official || hoverTarget.node.data.self_reported ? (
                <span className="vf-badge vf-badge-self-reported">Self-reported</span>
              ) : null}
              {hoverTarget.node.data.conflict ? (
                <div className="vf-conflict-disclosure">
                  Disclosed conflict: {hoverTarget.node.data.conflict.person} —{' '}
                  {hoverTarget.node.data.conflict.relationship}
                </div>
              ) : null}
              <div className="vf-hover-card-hint">
                Click to inspect · Double-click for Local Graph
              </div>
            </>
          )}
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
                : 'No items match the active filters at this playhead.'}
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
