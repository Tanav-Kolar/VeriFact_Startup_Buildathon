import { useEffect, useMemo, useRef, useState } from 'react'
import Graph from 'graphology'
import Sigma from 'sigma'
import type { FrameData, GraphEdgeData, GraphNodeData, StorylineRow } from '../types/api'
import { formatHourOffset, formatPlatformLabel, formatT0ShortIst, formatUtcTimestamp } from '../utils/format'
import {
  DIMMED_EDGE_COLOUR,
  DIMMED_NODE_COLOUR,
  RECENT_NODE_COLOUR,
  RESPONSE_GOLD_COLOUR,
  getStorylineColour,
  hashString
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
}

interface PositionedNode {
  data: GraphNodeData
  x: number
  y: number
  size: number
  colour: string
  dimmed: boolean
  isRecent: boolean
  isNewArrival: boolean
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
}

function computeHubPosition(
  storylineId: string | null,
  storylineOrder: Map<string, number>
): { hx: number; hy: number } {
  if (!storylineId || storylineId === 'unassigned') {
    return { hx: 0, hy: 0 }
  }
  const total = Math.max(1, storylineOrder.size)
  const index = storylineOrder.get(storylineId) ?? hashString(storylineId) % total
  const angle = (index / total) * Math.PI * 2 - Math.PI / 2
  const radius = 12
  return {
    hx: Math.cos(angle) * radius,
    hy: Math.sin(angle) * radius
  }
}

function computeNodeCoords(
  node: GraphNodeData,
  storylineOrder: Map<string, number>
): { x: number; y: number } {
  const { hx, hy } = computeHubPosition(node.storyline_id, storylineOrder)
  const h1 = hashString(node.id)
  const h2 = hashString(`${node.id}:r`)
  const platformBias = (hashString(node.platform) % 360) * (Math.PI / 180)
  const localAngle = platformBias + ((h1 % 1000) / 1000) * Math.PI * 1.4 - Math.PI * 0.7
  const dist = 1.1 + ((h2 % 1000) / 1000) * 5.2
  return {
    x: hx + Math.cos(localAngle) * dist,
    y: hy + Math.sin(localAngle) * dist
  }
}

function computeNodeBaseSize(reach: number | null, scale: number): number {
  const safeReach = typeof reach === 'number' && reach > 0 ? reach : 1
  const raw = 3.2 + Math.log10(safeReach + 1) * 2.2
  return Math.max(2.5, Math.min(20, raw * scale))
}

export function NebulaGraph({
  t0Utc,
  frames,
  storylines,
  enabledSources,
  currentHour,
  focusedStorylineId,
  selectedNodeId,
  onSelectNode
}: NebulaGraphProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const sigmaRef = useRef<Sigma | null>(null)

  const [settingsOpen, setSettingsOpen] = useState(false)
  const [hoveredNode, setHoveredNode] = useState<GraphNodeData | null>(null)
  const [overlayPoints, setOverlayPoints] = useState<OverlayPoint[]>([])
  const [overlayEdges, setOverlayEdges] = useState<OverlayEdge[]>([])

  const [settings, setSettings] = useState<GraphViewSettings>({
    nodeSizeScale: 1,
    showLinks: true,
    linkOpacity: 0.35,
    showLabels: false,
    enabledPlatforms: []
  })

  // Synchronize default platform filter list whenever event sources load
  useEffect(() => {
    if (enabledSources.length > 0) {
      setSettings((prev) =>
        prev.enabledPlatforms.length === 0
          ? { ...prev, enabledPlatforms: enabledSources }
          : prev
      )
    }
  }, [enabledSources])

  const storylineColourIndexMap = useMemo(() => {
    const map = new Map<string, number>()
    storylines.forEach((s, idx) => {
      map.set(s.id, typeof s.colour_index === 'number' ? s.colour_index : idx)
    })
    return map
  }, [storylines])

  const storylineOrderMap = useMemo(() => {
    const map = new Map<string, number>()
    let idx = 0
    for (const s of storylines) {
      if (s.id !== 'unassigned') {
        map.set(s.id, idx)
        idx += 1
      }
    }
    return map
  }, [storylines])

  // Accumulate nodes and edges strictly up to currentHour (FR-4.5 / PRD release criterion 3)
  const { visibleNodes, visibleEdges, nodeMap } = useMemo(() => {
    const allowedPlatforms =
      settings.enabledPlatforms.length > 0
        ? new Set(settings.enabledPlatforms.map((p) => p.toLowerCase()))
        : null

    const nodesById = new Map<string, PositionedNode>()
    const rawEdges: GraphEdgeData[] = []

    const sortedFrames = [...frames]
      .filter((f) => f.hour <= currentHour)
      .sort((a, b) => a.hour - b.hour)

    for (const frame of sortedFrames) {
      for (const n of frame.nodes_added) {
        if (n.hour > currentHour) continue
        if (allowedPlatforms && !allowedPlatforms.has(n.platform.toLowerCase())) continue

        const nodeStoryline = n.storyline_id ?? 'unassigned'
        const dimmed = focusedStorylineId !== null && nodeStoryline !== focusedStorylineId
        const isRecent = currentHour - n.hour >= 0 && currentHour - n.hour < 2
        const isNewArrival = n.hour === currentHour

        let colour = getStorylineColour(nodeStoryline, storylineColourIndexMap.get(nodeStoryline))
        if (n.is_response) {
          colour = RESPONSE_GOLD_COLOUR
        } else if (isRecent) {
          colour = RECENT_NODE_COLOUR
        }
        if (dimmed) {
          colour = DIMMED_NODE_COLOUR
        }

        const coords = computeNodeCoords(n, storylineOrderMap)
        nodesById.set(n.id, {
          data: n,
          x: coords.x,
          y: coords.y,
          size: computeNodeBaseSize(n.reach, settings.nodeSizeScale),
          colour,
          dimmed,
          isRecent,
          isNewArrival
        })
      }

      for (const e of frame.edges_added) {
        if (e.hour <= currentHour) {
          rawEdges.push(e)
        }
      }
    }

    const filteredEdges = rawEdges.filter(
      (e) => nodesById.has(e.source) && nodesById.has(e.target) && e.source !== e.target
    )

    return {
      visibleNodes: Array.from(nodesById.values()),
      visibleEdges: filteredEdges,
      nodeMap: nodesById
    }
  }, [
    frames,
    currentHour,
    focusedStorylineId,
    settings.enabledPlatforms,
    settings.nodeSizeScale,
    storylineColourIndexMap,
    storylineOrderMap
  ])

  // Keep latest callbacks/data in refs for Sigma event listeners
  const nodeMapRef = useRef(nodeMap)
  nodeMapRef.current = nodeMap
  const visibleEdgesRef = useRef(visibleEdges)
  visibleEdgesRef.current = visibleEdges
  const selectedNodeIdRef = useRef(selectedNodeId)
  selectedNodeIdRef.current = selectedNodeId
  const showLinksRef = useRef(settings.showLinks)
  showLinksRef.current = settings.showLinks
  const onSelectNodeRef = useRef(onSelectNode)
  onSelectNodeRef.current = onSelectNode

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const graph = new Graph({ multi: true, allowSelfLoops: false })

    for (const item of visibleNodes) {
      graph.addNode(item.data.id, {
        x: item.x,
        y: item.y,
        size: item.size,
        color: item.colour,
        label: item.data.label
      })
    }

    if (settings.showLinks) {
      visibleEdges.forEach((edge, idx) => {
        if (edge.type === 'links_to' || edge.type === 'contradicts') {
          // Drawn cleanly with dashed/dotted stroke on the aligned SVG overlay
          return
        }
        const sourceNode = nodeMap.get(edge.source)
        const targetNode = nodeMap.get(edge.target)
        const isDimmed = Boolean(sourceNode?.dimmed || targetNode?.dimmed)
        const alpha = isDimmed ? 0.08 : settings.linkOpacity
        const edgeColour = isDimmed
          ? DIMMED_EDGE_COLOUR
          : `rgba(148, 163, 184, ${alpha.toFixed(2)})`

        graph.addEdgeWithKey(`e-${idx}-${edge.source}-${edge.target}`, edge.source, edge.target, {
          size: 0.8,
          color: edgeColour
        })
      })
    }

    const sigma = new Sigma(graph, container, {
      renderLabels: settings.showLabels,
      labelColor: { color: '#cbd5e1' },
      labelDensity: 0.5,
      labelSize: 11,
      defaultNodeColor: '#64748b',
      defaultEdgeColor: 'rgba(148, 163, 184, 0.2)',
      allowInvalidContainer: true,
      zIndex: true
    })

    sigmaRef.current = sigma

    const syncOverlay = () => {
      const points: OverlayPoint[] = []
      const currentSelected = selectedNodeIdRef.current
      for (const item of nodeMapRef.current.values()) {
        const shouldDrawSpecial =
          item.data.is_official ||
          Boolean(item.data.is_response) ||
          item.data.conflict !== null ||
          item.data.id === currentSelected ||
          item.isNewArrival
        if (!shouldDrawSpecial) continue

        const vp = sigma.graphToViewport({ x: item.x, y: item.y })
        points.push({
          id: item.data.id,
          vx: vp.x,
          vy: vp.y,
          size: item.size,
          isOfficial: item.data.is_official,
          isResponse: Boolean(item.data.is_response),
          hasConflict: item.data.conflict !== null,
          isSelected: item.data.id === currentSelected,
          isNewArrival: item.isNewArrival,
          dimmed: item.dimmed
        })
      }

      const specialEdges: OverlayEdge[] = []
      if (showLinksRef.current) {
        visibleEdgesRef.current.forEach((edge, idx) => {
          if (edge.type !== 'links_to' && edge.type !== 'contradicts') return
          const src = nodeMapRef.current.get(edge.source)
          const dst = nodeMapRef.current.get(edge.target)
          if (!src || !dst) return
          const p1 = sigma.graphToViewport({ x: src.x, y: src.y })
          const p2 = sigma.graphToViewport({ x: dst.x, y: dst.y })
          specialEdges.push({
            key: `ov-${idx}-${edge.source}-${edge.target}`,
            x1: p1.x,
            y1: p1.y,
            x2: p2.x,
            y2: p2.y,
            kind: edge.type,
            dimmed: src.dimmed || dst.dimmed
          })
        })
      }

      setOverlayPoints(points)
      setOverlayEdges(specialEdges)
    }

    sigma.on('afterRender', syncOverlay)
    sigma.on('clickNode', ({ node }) => {
      const found = nodeMapRef.current.get(node)
      onSelectNodeRef.current(found ? found.data : null)
    })
    sigma.on('clickStage', () => {
      onSelectNodeRef.current(null)
    })
    sigma.on('enterNode', ({ node }) => {
      const found = nodeMapRef.current.get(node)
      setHoveredNode(found ? found.data : null)
      container.style.cursor = 'pointer'
    })
    sigma.on('leaveNode', () => {
      setHoveredNode(null)
      container.style.cursor = 'grab'
    })

    syncOverlay()

    return () => {
      sigma.kill()
      sigmaRef.current = null
    }
  }, [visibleNodes, visibleEdges, nodeMap, settings.showLabels, settings.showLinks, settings.linkOpacity, selectedNodeId])

  const togglePlatform = (platform: string) => {
    setSettings((prev) => {
      const exists = prev.enabledPlatforms.includes(platform)
      const next = exists
        ? prev.enabledPlatforms.filter((p) => p !== platform)
        : [...prev.enabledPlatforms, platform]
      return { ...prev, enabledPlatforms: next }
    })
  }

  const isEmpty = visibleNodes.length === 0

  return (
    <section className="vf-graph-stage" aria-label="Narrative graph canvas">
      <div ref={containerRef} className="vf-sigma-container" />

      {/* Viewport-synced SVG overlay for diamonds, conflict rings, focus rings, ripples, and dashed/dotted edges */}
      <svg className="vf-graph-overlay" aria-hidden="true">
        {overlayEdges.map((edge) => (
          <line
            key={edge.key}
            x1={edge.x1}
            y1={edge.y1}
            x2={edge.x2}
            y2={edge.y2}
            stroke={edge.kind === 'contradicts' ? '#facc15' : '#e2e8f0'}
            strokeWidth={edge.kind === 'contradicts' ? 1.5 : 1.25}
            strokeDasharray={edge.kind === 'contradicts' ? '2 4' : '5 4'}
            strokeOpacity={edge.dimmed ? 0.12 : 0.72}
          />
        ))}

        {overlayPoints.map((pt) => {
          const r = pt.size + 2
          const opacity = pt.dimmed ? 0.14 : 0.95
          return (
            <g key={pt.id} transform={`translate(${pt.vx}, ${pt.vy})`} opacity={opacity}>
              {pt.isNewArrival ? (
                <circle className="vf-arrival-ripple" r={r + 4} fill="none" stroke="#f8fafc" strokeWidth={1.2} />
              ) : null}
              {pt.hasConflict ? (
                <circle r={r + 3.5} fill="none" stroke="#facc15" strokeWidth={1.6} />
              ) : null}
              {pt.isOfficial || pt.isResponse ? (
                <polygon
                  points={`0,${-(r + 2)} ${r + 2},0 0,${r + 2} ${-(r + 2)},0`}
                  fill={pt.isResponse ? '#facc15' : '#f8fafc'}
                  stroke="#0b1120"
                  strokeWidth={1.2}
                />
              ) : null}
              {pt.isSelected ? (
                <circle r={r + 6} fill="none" stroke="#38bdf8" strokeWidth={2} />
              ) : null}
            </g>
          )
        })}
      </svg>

      {/* Top-left: Graph view settings toggle (FR-3.5) */}
      <div className="vf-graph-top-left">
        <button
          type="button"
          className="vf-overlay-btn"
          aria-expanded={settingsOpen}
          onClick={() => setSettingsOpen((prev) => !prev)}
        >
          Graph view
        </button>

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

      {/* Top-right: Clock (FR-3.6) */}
      <div className="vf-graph-clock" aria-live="polite">
        <div className="vf-clock-hour">{formatHourOffset(currentHour)}</div>
        <div className="vf-clock-time">{formatT0ShortIst(t0Utc, currentHour)}</div>
      </div>

      {/* Hover tooltip */}
      {hoveredNode ? (
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
              : `No items recorded up to ${formatHourOffset(currentHour)}.`}
          </p>
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
