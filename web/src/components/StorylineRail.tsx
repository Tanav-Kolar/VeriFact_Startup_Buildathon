import type { StorylineRow } from '../types/api'
import { formatCount, formatHourOffset, formatPlatformLabel } from '../utils/format'
import { getStorylineColour } from '../utils/palette'

export interface StorylineRailProps {
  storylines: StorylineRow[]
  enabledSources: string[]
  focusedStorylineId: string | null
  onToggleFocus: (storylineId: string) => void
  loading?: boolean
}

export function StorylineRail({
  storylines,
  enabledSources,
  focusedStorylineId,
  onToggleFocus,
  loading = false
}: StorylineRailProps) {
  const hasAnyFocus = focusedStorylineId !== null

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
      ) : storylines.length === 0 ? (
        <p className="vf-muted-empty">No storylines configured for this event.</p>
      ) : (
        <ul className="vf-storyline-list">
          {storylines.map((row) => {
            const isUnassigned = row.id === 'unassigned'
            const isFocused = focusedStorylineId === row.id
            const count = row.count
            const notYetSeen = (count === null || count === 0) && row.first_seen === null
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
                    isFocused ? 'is-focused' : '',
                    hasAnyFocus && !isFocused ? 'is-dimmed' : '',
                    notYetSeen ? 'is-unseen' : '',
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
                    {notYetSeen ? (
                      <span className="vf-unseen-label">Not yet seen</span>
                    ) : row.first_seen ? (
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
