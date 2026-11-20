import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import {
  createLiveEventSource,
  fetchClaimCard,
  fetchEventDetail,
  fetchFrames,
  fetchItemDetail,
  fetchSignals,
  fetchStorylineDetail,
  fetchStorylines,
  fetchTrust
} from '../api/client'
import { Header } from '../components/Header'
import { Inspector } from '../components/Inspector'
import { NebulaGraph } from '../components/NebulaGraph'
import { StorylineRail } from '../components/StorylineRail'
import { Timeline } from '../components/Timeline'
import { TrustStrip } from '../components/TrustStrip'
import type {
  ClaimCardData,
  EventDetail,
  FrameData,
  GraphEdgeData,
  GraphNodeData,
  ItemDetail,
  StorylineDetail,
  StorylineRow,
  TimingSignal,
  TrustResponse
} from '../types/api'
import {
  computeHourFromT0,
  resolveInitialPlayheadHour,
  resolveTurningPointHour
} from '../utils/format'

const FRAME_PREFETCH_CHUNK = 12
const STORYLINE_PREFETCH_AHEAD = 3

function mergeFrames(existing: FrameData[], incoming: FrameData[]): FrameData[] {
  const byHour = new Map<number, FrameData>()
  for (const frame of existing) {
    byHour.set(frame.hour, frame)
  }
  for (const frame of incoming) {
    const prev = byHour.get(frame.hour)
    if (!prev) {
      byHour.set(frame.hour, frame)
      continue
    }
    const seenIds = new Set(prev.nodes_added.map((n) => n.id))
    const mergedNodes = [...prev.nodes_added]
    for (const node of frame.nodes_added) {
      if (!seenIds.has(node.id)) {
        seenIds.add(node.id)
        mergedNodes.push(node)
      }
    }
    byHour.set(frame.hour, {
      hour: frame.hour,
      nodes_added: mergedNodes,
      edges_added: frame.edges_added.length > 0 ? frame.edges_added : prev.edges_added,
      storyline_counts:
        Object.keys(frame.storyline_counts).length > 0
          ? frame.storyline_counts
          : prev.storyline_counts
    })
  }
  return Array.from(byHour.values()).sort((a, b) => a.hour - b.hour)
}

export function WorkspacePage() {
  const { eventId = '' } = useParams<{ eventId: string }>()
  const [searchParams, setSearchParams] = useSearchParams()

  const initialHourParam = searchParams.get('t')
  const initialStorylineParam = searchParams.get('storyline')
  const initialClaimParam = searchParams.get('claim')
  const initialItemParam = searchParams.get('item')

  const [eventDetail, setEventDetail] = useState<EventDetail | null>(null)
  const [frames, setFrames] = useState<FrameData[]>([])
  const [storylines, setStorylines] = useState<StorylineRow[]>([])
  const [signals, setSignals] = useState<TimingSignal[]>([])
  const [trust, setTrust] = useState<TrustResponse | null>(null)
  const [isLive, setIsLive] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [hour, setHour] = useState<number>(() =>
    resolveInitialPlayheadHour(initialHourParam, [])
  )
  const [isPlaying, setIsPlaying] = useState(false)

  const [focusedStorylineId, setFocusedStorylineId] = useState<string | null>(initialStorylineParam)
  const [focusedStorylineDetail, setFocusedStorylineDetail] = useState<StorylineDetail | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(initialItemParam)
  const [selectedItem, setSelectedItem] = useState<ItemDetail | null>(null)
  const [selectedClaim, setSelectedClaim] = useState<ClaimCardData | null>(null)
  const [inspectorLoading, setInspectorLoading] = useState(false)
  const [inspectorError, setInspectorError] = useState<string | null>(null)

  const storylinesCacheRef = useRef<Map<number, StorylineRow[]>>(new Map())
  const storylinesInFlightRef = useRef<Set<number>>(new Set())
  const prefetchedFrameWindowsRef = useRef<Set<string>>(new Set())
  const initialFramesLoadedRef = useRef(false)

  // Reset per-event caches on event change
  useEffect(() => {
    storylinesCacheRef.current.clear()
    storylinesInFlightRef.current.clear()
    prefetchedFrameWindowsRef.current.clear()
    initialFramesLoadedRef.current = false
    setIsPlaying(false)
    setHour(resolveInitialPlayheadHour(searchParams.get('t'), []))
  }, [eventId])

  // Load base event metadata, frames, signals, and trust
  useEffect(() => {
    if (!eventId) return
    let active = true
    setLoadError(null)

    Promise.all([
      fetchEventDetail(eventId),
      fetchFrames(eventId),
      fetchSignals(eventId).catch(() => [] as TimingSignal[]),
      fetchTrust(eventId).catch(() => null)
    ])
      .then(([detail, loadedFrames, loadedSignals, loadedTrust]) => {
        if (!active) return
        const isFirstLoad = !initialFramesLoadedRef.current
        initialFramesLoadedRef.current = true
        setEventDetail(detail)
        setFrames(loadedFrames)
        setSignals(loadedSignals)
        setTrust(loadedTrust)
        if (isFirstLoad) {
          const explicitT = searchParams.get('t')
          const resolved = resolveInitialPlayheadHour(explicitT, loadedFrames)
          if (!explicitT) {
            const hasNodesAtResolved = loadedFrames.some(
              (f) => f.hour <= resolved && f.nodes_added.length > 0
            )
            if (!hasNodesAtResolved) {
              const latestWithNodes = loadedFrames.reduce<number | null>(
                (acc, f) =>
                  f.nodes_added.length > 0 && (acc === null || f.hour > acc) ? f.hour : acc,
                null
              )
              setHour(latestWithNodes ?? resolved)
            } else {
              setHour(resolved)
            }
          } else {
            setHour(resolved)
          }
        }
      })
      .catch((err: unknown) => {
        if (!active) return
        setLoadError(err instanceof Error ? err.message : 'Unable to load event')
      })

    return () => {
      active = false
    }
  }, [eventId])

  // Load deep-linked claim or item if ?claim= or ?item= is present
  useEffect(() => {
    if (!eventId) return
    if (initialItemParam) {
      let active = true
      setInspectorLoading(true)
      setInspectorError(null)
      fetchItemDetail(eventId, initialItemParam)
        .then((item) => {
          if (!active) return
          setSelectedItem(item)
          setSelectedClaim(null)
          setSelectedNodeId(initialItemParam)
        })
        .catch((err: unknown) => {
          if (active) {
            setInspectorError(err instanceof Error ? err.message : 'Unable to load item')
          }
        })
        .finally(() => {
          if (active) setInspectorLoading(false)
        })
      return () => {
        active = false
      }
    }

    if (initialClaimParam) {
      let active = true
      setInspectorLoading(true)
      setInspectorError(null)
      fetchClaimCard(eventId, initialClaimParam, hour)
        .then((claim) => {
          if (!active) return
          setSelectedClaim(claim)
          setSelectedItem(null)
        })
        .catch((err: unknown) => {
          if (active) {
            setInspectorError(err instanceof Error ? err.message : 'Unable to load claim')
          }
        })
        .finally(() => {
          if (active) setInspectorLoading(false)
        })
      return () => {
        active = false
      }
    }
  }, [eventId, initialClaimParam, initialItemParam]) // intentionally run on initial deep-link params

  const { minHour, maxHour } = useMemo(() => {
    const frameHours = frames.map((f) => f.hour)
    const min = frameHours.length > 0 ? Math.min(0, ...frameHours) : 0
    const tpHours = (eventDetail?.turning_points ?? [])
      .map((tp) => computeHourFromT0(eventDetail?.t0_utc, tp.at_utc))
      .filter((h): h is number => h !== null)
    const max = Math.max(24, eventDetail?.now_hour ?? 0, ...frameHours, ...tpHours)
    return { minHour: min, maxHour: max }
  }, [frames, eventDetail])

  // Prefetch next window of frames ahead of playhead when frames are sparse or paged
  useEffect(() => {
    if (!eventId || !initialFramesLoadedRef.current || frames.length === 0) return
    const nextFrom = Math.floor((hour + 1) / FRAME_PREFETCH_CHUNK) * FRAME_PREFETCH_CHUNK
    const nextTo = Math.min(maxHour, nextFrom + FRAME_PREFETCH_CHUNK - 1)
    if (nextFrom > maxHour || nextTo < nextFrom) return

    const windowKey = `${nextFrom}:${nextTo}`
    if (prefetchedFrameWindowsRef.current.has(windowKey)) return
    prefetchedFrameWindowsRef.current.add(windowKey)

    fetchFrames(eventId, nextFrom, nextTo)
      .then((chunk) => {
        if (chunk.length > 0) {
          setFrames((prev) => mergeFrames(prev, chunk))
        }
      })
      .catch(() => {
        // Prefetch is best-effort; keep existing frames
      })
  }, [eventId, hour, maxHour, frames.length])

  // Load storylines for current hour with cache + smooth lookahead prefetching
  useEffect(() => {
    if (!eventId) return
    let active = true

    const cached = storylinesCacheRef.current.get(hour)
    if (cached) {
      setStorylines(cached)
    } else if (!storylinesInFlightRef.current.has(hour)) {
      storylinesInFlightRef.current.add(hour)
      fetchStorylines(eventId, hour)
        .then((rows) => {
          storylinesCacheRef.current.set(hour, rows)
          if (active) setStorylines(rows)
        })
        .catch(() => {
          // Preserve existing rows on transient error
        })
        .finally(() => {
          storylinesInFlightRef.current.delete(hour)
        })
    }

    // Background prefetch upcoming hours so replay transitions never stall
    for (let offset = 1; offset <= STORYLINE_PREFETCH_AHEAD; offset += 1) {
      const targetHour = hour + offset
      if (targetHour > maxHour) break
      if (
        storylinesCacheRef.current.has(targetHour) ||
        storylinesInFlightRef.current.has(targetHour)
      ) {
        continue
      }
      storylinesInFlightRef.current.add(targetHour)
      fetchStorylines(eventId, targetHour)
        .then((rows) => {
          storylinesCacheRef.current.set(targetHour, rows)
        })
        .catch(() => {
          // Best-effort prefetch
        })
        .finally(() => {
          storylinesInFlightRef.current.delete(targetHour)
        })
    }

    return () => {
      active = false
    }
  }, [eventId, hour, maxHour])

  // Refresh focused storyline details when focusedStorylineId or hour changes
  useEffect(() => {
    if (!eventId || !focusedStorylineId) {
      setFocusedStorylineDetail(null)
      return
    }
    let active = true
    fetchStorylineDetail(eventId, focusedStorylineId, hour)
      .then((detail) => {
        if (active) setFocusedStorylineDetail(detail)
      })
      .catch(() => {
        if (active) setFocusedStorylineDetail(null)
      })
    return () => {
      active = false
    }
  }, [eventId, focusedStorylineId, hour])

  // Subscribe to SSE live updates; hide live indicator when API reports live: false
  useEffect(() => {
    if (!eventId) return
    const source = createLiveEventSource(eventId)

    const onItemAdded = (evt: MessageEvent<string>) => {
      try {
        const parsed = JSON.parse(evt.data) as {
          node?: GraphNodeData
          edges?: GraphEdgeData[]
          live?: boolean
        }
        if (parsed.live === false) {
          setIsLive(false)
          return
        }
        const node = parsed.node
        if (!node) return
        setIsLive(true)
        storylinesCacheRef.current.delete(node.hour)
        setFrames((prev) =>
          mergeFrames(prev, [
            {
              hour: node.hour,
              nodes_added: [node],
              edges_added: parsed.edges ?? [],
              storyline_counts: {}
            }
          ])
        )
      } catch {
        // Ignore malformed SSE payload
      }
    }

    const onStorylineCounts = (evt: MessageEvent<string>) => {
      try {
        const counts = JSON.parse(evt.data) as Record<string, number>
        setIsLive(true)
        setStorylines((prev) =>
          prev.map((row) =>
            typeof counts[row.id] === 'number' ? { ...row, count: counts[row.id] } : row
          )
        )
      } catch {
        // Ignore malformed SSE payload
      }
    }

    const onHeartbeat = (evt: MessageEvent<string>) => {
      try {
        const parsed = JSON.parse(evt.data) as { live?: boolean }
        if (parsed.live === false) {
          setIsLive(false)
        } else if (parsed.live === true) {
          setIsLive(true)
        }
      } catch {
        // Heartbeat without JSON payload
      }
    }

    source.addEventListener('item_added', onItemAdded as EventListener)
    source.addEventListener('storyline_counts', onStorylineCounts as EventListener)
    source.addEventListener('status', onHeartbeat as EventListener)
    source.addEventListener('heartbeat', onHeartbeat as EventListener)
    source.onmessage = onHeartbeat
    source.onerror = () => {
      setIsLive(false)
    }

    return () => {
      source.close()
    }
  }, [eventId])

  // Replay timer
  useEffect(() => {
    if (!isPlaying) return
    const timer = window.setInterval(() => {
      setHour((prev) => {
        if (prev >= maxHour) {
          setIsPlaying(false)
          return prev
        }
        return prev + 1
      })
    }, 650)
    return () => window.clearInterval(timer)
  }, [isPlaying, maxHour])

  const handleChangeHour = useCallback(
    (nextHour: number) => {
      const clamped = Math.max(minHour, Math.min(maxHour, nextHour))
      setHour(clamped)
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          next.set('t', String(clamped))
          return next
        },
        { replace: true }
      )
    },
    [minHour, maxHour, setSearchParams]
  )

  const handleJumpToResponse = useCallback(() => {
    const firstTp = eventDetail?.turning_points?.[0]
    if (!firstTp) return
    let responseNodeHour: number | null = null
    for (const frame of frames) {
      const match = frame.nodes_added.find((n) => n.is_response)
      if (match) {
        responseNodeHour = match.hour
        break
      }
    }
    const tpHour = resolveTurningPointHour(eventDetail?.t0_utc, firstTp, responseNodeHour)
    if (tpHour !== null) {
      setIsPlaying(false)
      handleChangeHour(tpHour)
    }
  }, [eventDetail, frames, handleChangeHour])

  const handleTogglePlay = useCallback(() => {
    if (!isPlaying && hour >= maxHour) {
      const restartHour = minHour <= 0 && maxHour >= 0 ? 0 : minHour
      handleChangeHour(restartHour)
      setIsPlaying(true)
      return
    }
    setIsPlaying((prev) => !prev)
  }, [isPlaying, hour, minHour, maxHour, handleChangeHour])

  const handleJumpToNow = useCallback(() => {
    if (eventDetail?.now_hour === null || eventDetail?.now_hour === undefined) return
    setIsPlaying(false)
    handleChangeHour(eventDetail.now_hour)
  }, [eventDetail, handleChangeHour])

  const handleToggleFocus = useCallback(
    (storylineId: string) => {
      setFocusedStorylineId((prev) => {
        const nextFocus = prev === storylineId ? null : storylineId
        setSearchParams(
          (params) => {
            const updated = new URLSearchParams(params)
            if (nextFocus) {
              updated.set('storyline', nextFocus)
            } else {
              updated.delete('storyline')
            }
            return updated
          },
          { replace: true }
        )
        return nextFocus
      })
    },
    [setSearchParams]
  )

  const handleFocusStorylineExplicit = useCallback(
    (storylineId: string) => {
      setFocusedStorylineId(storylineId)
      setSelectedNodeId(null)
      setSelectedItem(null)
      setSelectedClaim(null)
      setInspectorError(null)
      setSearchParams(
        (params) => {
          const updated = new URLSearchParams(params)
          updated.set('storyline', storylineId)
          return updated
        },
        { replace: true }
      )
    },
    [setSearchParams]
  )

  const handleSelectItemId = useCallback(
    (itemId: string) => {
      if (!eventId) return
      setSelectedNodeId(itemId)
      setInspectorLoading(true)
      setInspectorError(null)
      fetchItemDetail(eventId, itemId)
        .then((detail) => {
          setSelectedItem(detail)
          setSelectedClaim(null)
        })
        .catch((err: unknown) => {
          setSelectedItem(null)
          setInspectorError(err instanceof Error ? err.message : 'Unable to load item detail')
        })
        .finally(() => setInspectorLoading(false))
    },
    [eventId]
  )

  const handleSelectNode = useCallback(
    (node: GraphNodeData | null) => {
      if (!node) {
        setSelectedNodeId(null)
        setSelectedItem(null)
        setInspectorError(null)
        return
      }
      if (node.type === 'storyline') {
        handleToggleFocus(node.storyline_id ?? node.id)
        return
      }
      if (node.type === 'claim') {
        setSelectedNodeId(node.id)
        setInspectorLoading(true)
        setInspectorError(null)
        fetchClaimCard(eventId, node.id, hour)
          .then((claim) => {
            setSelectedClaim(claim)
            setSelectedItem(null)
          })
          .catch((err: unknown) => {
            setSelectedClaim(null)
            setInspectorError(err instanceof Error ? err.message : 'Unable to load claim')
          })
          .finally(() => setInspectorLoading(false))
        return
      }
      handleSelectItemId(node.id)
    },
    [eventId, hour, handleSelectItemId, handleToggleFocus]
  )

  const handleSelectClaimId = useCallback(
    (claimId: string) => {
      if (!eventId) return
      setInspectorLoading(true)
      setInspectorError(null)
      fetchClaimCard(eventId, claimId, hour)
        .then((claim) => {
          setSelectedClaim(claim)
          setSelectedItem(null)
        })
        .catch((err: unknown) => {
          setInspectorError(err instanceof Error ? err.message : 'Unable to load claim card')
        })
        .finally(() => setInspectorLoading(false))
    },
    [eventId, hour]
  )

  const handleClearAll = useCallback(() => {
    setFocusedStorylineId(null)
    setSelectedNodeId(null)
    setSelectedItem(null)
    setSelectedClaim(null)
    setInspectorError(null)
    setSearchParams(
      (params) => {
        if (!params.has('storyline')) return params
        const updated = new URLSearchParams(params)
        updated.delete('storyline')
        return updated
      },
      { replace: true }
    )
  }, [setSearchParams])

  // Items matching the currently focused storyline up to playhead hour (P1.6 expandable count)
  const focusedStorylineItems = useMemo(() => {
    if (!focusedStorylineId) return []
    const items: GraphNodeData[] = []
    const seen = new Set<string>()
    for (const frame of frames) {
      if (frame.hour > hour) continue
      for (const node of frame.nodes_added) {
        if (node.hour > hour || seen.has(node.id)) continue
        const nodeStoryline = node.storyline_id ?? 'unassigned'
        if (nodeStoryline === focusedStorylineId) {
          seen.add(node.id)
          items.push(node)
        }
      }
    }
    return items.sort((a, b) => b.hour - a.hour)
  }, [frames, hour, focusedStorylineId])

  // Keyboard shortcuts: Left/Right +-1h, Space play/pause, R response, Esc clear focus (design.md)
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.tagName === 'SELECT' ||
          target.isContentEditable)
      ) {
        if (target.getAttribute('type') !== 'range') return
      }

      if (event.key === 'ArrowLeft') {
        event.preventDefault()
        setIsPlaying(false)
        handleChangeHour(hour - 1)
      } else if (event.key === 'ArrowRight') {
        event.preventDefault()
        setIsPlaying(false)
        handleChangeHour(hour + 1)
      } else if (event.key === ' ') {
        event.preventDefault()
        handleTogglePlay()
      } else if (event.key === 'r' || event.key === 'R') {
        event.preventDefault()
        handleJumpToResponse()
      } else if (event.key === 'Escape') {
        event.preventDefault()
        handleClearAll()
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [hour, handleChangeHour, handleTogglePlay, handleJumpToResponse, handleClearAll])

  return (
    <div className="vf-workspace-shell">
      <Header
        eventId={eventId}
        eventName={eventDetail?.name ?? eventId}
        t0Utc={eventDetail?.t0_utc ?? null}
        configFile={eventDetail?.config_file ?? null}
        isLive={isLive}
      />

      {loadError ? (
        <div className="vf-error-banner" role="alert">
          Unable to load event <code>{eventId}</code>: {loadError}
        </div>
      ) : null}

      <main className="vf-workspace-main">
        <StorylineRail
          storylines={storylines}
          enabledSources={eventDetail?.sources ?? []}
          focusedStorylineId={focusedStorylineId}
          onToggleFocus={handleToggleFocus}
        />

        <NebulaGraph
          t0Utc={eventDetail?.t0_utc ?? null}
          frames={frames}
          storylines={storylines}
          enabledSources={eventDetail?.sources ?? []}
          currentHour={hour}
          focusedStorylineId={focusedStorylineId}
          selectedNodeId={selectedNodeId}
          onSelectNode={handleSelectNode}
          onJumpToHour={(targetHour) => {
            setIsPlaying(false)
            setHour(targetHour)
          }}
        />

        <Inspector
          selectedItem={selectedItem}
          selectedClaim={selectedClaim}
          focusedStoryline={focusedStorylineDetail}
          storylines={storylines}
          storylineItems={focusedStorylineItems}
          signals={signals}
          currentHour={hour}
          loading={inspectorLoading}
          error={inspectorError}
          onSelectClaimId={handleSelectClaimId}
          onSelectItemId={handleSelectItemId}
          onFocusStoryline={handleFocusStorylineExplicit}
          onClearSelection={() => {
            setSelectedNodeId(null)
            setSelectedItem(null)
            setSelectedClaim(null)
            setInspectorError(null)
          }}
        />
      </main>

      <Timeline
        t0Utc={eventDetail?.t0_utc ?? null}
        frames={frames}
        storylines={storylines}
        turningPoints={eventDetail?.turning_points ?? []}
        minHour={minHour}
        maxHour={maxHour}
        currentHour={hour}
        nowHour={eventDetail?.now_hour ?? null}
        isPlaying={isPlaying}
        onChangeHour={handleChangeHour}
        onTogglePlay={handleTogglePlay}
        onJumpToResponse={handleJumpToResponse}
        onJumpToNow={handleJumpToNow}
      />

      <TrustStrip
        trust={trust}
        onFocusUnassigned={() => handleFocusStorylineExplicit('unassigned')}
      />
    </div>
  )
}
