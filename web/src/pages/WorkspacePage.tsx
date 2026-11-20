import { useCallback, useEffect, useMemo, useState } from 'react'
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
import { computeHourFromT0 } from '../utils/format'

export function WorkspacePage() {
  const { eventId = '' } = useParams<{ eventId: string }>()
  const [searchParams, setSearchParams] = useSearchParams()

  const initialHourParam = searchParams.get('t')
  const initialStorylineParam = searchParams.get('storyline')
  const initialClaimParam = searchParams.get('claim')

  const [eventDetail, setEventDetail] = useState<EventDetail | null>(null)
  const [frames, setFrames] = useState<FrameData[]>([])
  const [storylines, setStorylines] = useState<StorylineRow[]>([])
  const [signals, setSignals] = useState<TimingSignal[]>([])
  const [trust, setTrust] = useState<TrustResponse | null>(null)
  const [isLive, setIsLive] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [hour, setHour] = useState<number>(() => {
    const parsed = initialHourParam !== null ? Number(initialHourParam) : 0
    return Number.isFinite(parsed) ? Math.trunc(parsed) : 0
  })
  const [isPlaying, setIsPlaying] = useState(false)

  const [focusedStorylineId, setFocusedStorylineId] = useState<string | null>(initialStorylineParam)
  const [focusedStorylineDetail, setFocusedStorylineDetail] = useState<StorylineDetail | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [selectedItem, setSelectedItem] = useState<ItemDetail | null>(null)
  const [selectedClaim, setSelectedClaim] = useState<ClaimCardData | null>(null)
  const [inspectorLoading, setInspectorLoading] = useState(false)

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
        setEventDetail(detail)
        setFrames(loadedFrames)
        setSignals(loadedSignals)
        setTrust(loadedTrust)
      })
      .catch((err: unknown) => {
        if (!active) return
        setLoadError(err instanceof Error ? err.message : 'Unable to load event')
      })

    return () => {
      active = false
    }
  }, [eventId])

  // Load deep-linked claim if ?claim= is present
  useEffect(() => {
    if (!eventId || !initialClaimParam) return
    let active = true
    setInspectorLoading(true)
    fetchClaimCard(eventId, initialClaimParam, hour)
      .then((claim) => {
        if (!active) return
        setSelectedClaim(claim)
        setSelectedItem(null)
      })
      .catch(() => {
        // Ignore invalid deep-linked claim id without crashing workspace
      })
      .finally(() => {
        if (active) setInspectorLoading(false)
      })
    return () => {
      active = false
    }
  }, [eventId, initialClaimParam]) // intentionally run once on deep-link params

  // Refresh storylines at current playhead hour
  useEffect(() => {
    if (!eventId) return
    let active = true
    fetchStorylines(eventId, hour)
      .then((rows) => {
        if (active) setStorylines(rows)
      })
      .catch(() => {
        // Preserve existing rows on transient error
      })
    return () => {
      active = false
    }
  }, [eventId, hour])

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
        setFrames((prev) => {
          const existing = prev.find((f) => f.hour === node.hour)
          if (existing) {
            return prev.map((f) =>
              f.hour === node.hour
                ? {
                    ...f,
                    nodes_added: [...f.nodes_added, node],
                    edges_added: [...f.edges_added, ...(parsed.edges ?? [])]
                  }
                : f
            )
          }
          return [
            ...prev,
            {
              hour: node.hour,
              nodes_added: [node],
              edges_added: parsed.edges ?? [],
              storyline_counts: {}
            }
          ]
        })
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

  const { minHour, maxHour } = useMemo(() => {
    const frameHours = frames.map((f) => f.hour)
    const min = frameHours.length > 0 ? Math.min(0, ...frameHours) : 0
    const tpHours = (eventDetail?.turning_points ?? [])
      .map((tp) => computeHourFromT0(eventDetail?.t0_utc, tp.at_utc))
      .filter((h): h is number => h !== null)
    const max = Math.max(24, eventDetail?.now_hour ?? 0, ...frameHours, ...tpHours)
    return { minHour: min, maxHour: max }
  }, [frames, eventDetail])

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
    const tpHour = computeHourFromT0(eventDetail?.t0_utc, firstTp.at_utc)
    if (tpHour !== null) {
      setIsPlaying(false)
      handleChangeHour(tpHour)
    }
  }, [eventDetail, handleChangeHour])

  const handleJumpToNow = useCallback(() => {
    if (eventDetail?.now_hour === null || eventDetail?.now_hour === undefined) return
    setIsPlaying(false)
    handleChangeHour(eventDetail.now_hour)
  }, [eventDetail, handleChangeHour])

  const handleToggleFocus = useCallback((storylineId: string) => {
    setFocusedStorylineId((prev) => (prev === storylineId ? null : storylineId))
  }, [])

  const handleSelectItemId = useCallback(
    (itemId: string) => {
      if (!eventId) return
      setSelectedNodeId(itemId)
      setInspectorLoading(true)
      fetchItemDetail(eventId, itemId)
        .then((detail) => {
          setSelectedItem(detail)
          setSelectedClaim(null)
        })
        .catch(() => {
          // Keep current selection if item fetch fails
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
        return
      }
      if (node.type === 'claim') {
        setSelectedNodeId(node.id)
        setInspectorLoading(true)
        fetchClaimCard(eventId, node.id, hour)
          .then((claim) => {
            setSelectedClaim(claim)
            setSelectedItem(null)
          })
          .finally(() => setInspectorLoading(false))
        return
      }
      handleSelectItemId(node.id)
    },
    [eventId, hour, handleSelectItemId]
  )

  const handleSelectClaimId = useCallback(
    (claimId: string) => {
      if (!eventId) return
      setInspectorLoading(true)
      fetchClaimCard(eventId, claimId, hour)
        .then((claim) => {
          setSelectedClaim(claim)
          setSelectedItem(null)
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
  }, [])

  // Keyboard shortcuts: Left/Right +-1h, Space play/pause, R response, Esc clear focus (design.md)
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (
        target &&
        (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
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
        setIsPlaying((prev) => !prev)
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
  }, [hour, handleChangeHour, handleJumpToResponse, handleClearAll])

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
        />

        <Inspector
          selectedItem={selectedItem}
          selectedClaim={selectedClaim}
          focusedStoryline={focusedStorylineDetail}
          signals={signals}
          loading={inspectorLoading}
          onSelectClaimId={handleSelectClaimId}
          onSelectItemId={handleSelectItemId}
          onClearSelection={() => {
            setSelectedNodeId(null)
            setSelectedItem(null)
            setSelectedClaim(null)
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
        onTogglePlay={() => setIsPlaying((prev) => !prev)}
        onJumpToResponse={handleJumpToResponse}
        onJumpToNow={handleJumpToNow}
      />

      <TrustStrip trust={trust} />
    </div>
  )
}
