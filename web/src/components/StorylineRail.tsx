import type { StorylineRow } from '../types/api'
import { formatCount, formatHourOffset, formatPlatformLabel } from '../utils/format'
import { getStorylineColour } from '../utils/palette'

export const STORYLINE_EMERGE_THRESHOLD = 3

export interface StorylineRailProps {
  storylines: StorylineRow[]
  enabledSources: string[]
  focusedStorylineId: string | null
  onToggleFocus: (storylineId: string) => void
  earlySignalCount?: number
  loading?: boolean
}

export function StorylineRail({
  storylines,
  enabledSources,
  focusedStorylineId,
  onToggleFocus,
  earlySignalCount = 0,
  loading = false
}: StorylineRailProps) {
  const hasAnyFocus = focusedStorylineId !== null

  const emergedStorylines = storylines
    .filter((row) => (row.count ?? 0) >= STORYLINE_EMERGE_THRESHOLD)
    .sort((a, b) => (b.count ?? 0) - (a.count ?? 0) || a.id.localeCompare(b.id))

  return (
    <aside className="vf-rail" aria-label="Storylines">
      <div className="vf-panel-header">
        <h2>Storylines</h2>
        {hasAnyFocus ? (
          <button
            type="button"
            className="vf-clear-focus-btn"
            onClick={() => onToggleFocus(focusedStorylineId)}
          >
            Clear focus
          </button>
        ) : null}
      </div>

      {loading && storylines.length === 0 ? (
        <p className="vf-muted-empty">Loading storylines…</p>
      ) : emergedStorylines.length === 0 ? (
        <div className="vf-rail-listening" role="status" aria-live="polite">
          <div className="vf-listening-title">Listening for emerging narratives…</div>
          <p className="vf-listening-subtitle">
            Storylines emerge here once ≥ {STORYLINE_EMERGE_THRESHOLD} related items cluster on the
            graph.
          </p>
          {earlySignalCount > 0 ? (
            <span className="vf-early-signals-badge">
              {earlySignalCount} early {earlySignalCount === 1 ? 'signal' : 'signals'} (&lt;{' '}
              {STORYLINE_EMERGE_THRESHOLD} items each)
            </span>
          ) : null}
        </div>
      ) : (
        <ul className="vf-storyline-list">
          {emergedStorylines.map((row) => {
            const isUnassigned = row.id === 'unassigned'
            const isFocused = focusedStorylineId === row.id
            const count = row.count
            const colour = getStorylineColour(row.id, row.colour_index)
            const reachedSet = new Set(row.platforms_reached.map((p) => p.platform.toLowerCase()))
            const badgePlatforms =
              enabledSources.length > 0
                ? enabledSources
                : row.platforms_reached.map((p) => p.platform)

            return (
              <li key={row.id}>
                <button
                  type="button"
                  onClick={() => onToggleFocus(row.id)}
                  aria-pressed={isFocused}
                  className={[
                    'vf-storyline-row',
                    'is-emerged',
                    isFocused ? 'is-focused' : '',
                    hasAnyFocus && !isFocused ? 'is-dimmed' : '',
                    isUnassigned ? 'is-unassigned' : ''
                  ]
                    .filter(Boolean)
                    .join(' ')}
                >
                  <div className="vf-storyline-row-top">
                    <span className="vf-storyline-name-wrap">
                      <span
                        className="vf-colour-dot"
                        style={{ backgroundColor: colour }}
                        aria-hidden="true"
                      />
                      <span className="vf-storyline-name">{row.name}</span>
                    </span>
                    <span className="vf-storyline-count">{formatCount(count)}</span>
                  </div>

                  <div className="vf-storyline-row-meta">
                    {row.first_seen ? (
                      <span className="vf-first-seen">
                        FIRST {formatPlatformLabel(row.first_seen.platform)}{' '}
                        {formatHourOffset(row.first_seen.hour)}
                      </span>
                    ) : (
                      <span className="vf-unseen-label">Seen</span>
                    )}
                  </div>

                  {!isUnassigned && badgePlatforms.length > 0 ? (
                    <div className="vf-platform-badges" aria-label="Platforms reached">
                      {badgePlatforms.map((platform) => {
                        const lit = reachedSet.has(platform.toLowerCase())
                        return (
                          <span
                            key={platform}
                            className={`vf-platform-badge ${lit ? 'is-lit' : ''}`}
                          >
                            {formatPlatformLabel(platform)}
                          </span>
                        )
                      })}
                    </div>
                  ) : null}
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </aside>
  )
}
