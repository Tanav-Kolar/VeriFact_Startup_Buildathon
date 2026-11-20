import { NavLink } from 'react-router-dom'
import { formatIstTimestamp, formatUtcTimestamp } from '../utils/format'

export interface HeaderProps {
  eventId: string | null
  eventName: string | null
  t0Utc: string | null
  configFile: string | null
  isLive?: boolean
}

export function Header({ eventId, eventName, t0Utc, configFile, isLive = false }: HeaderProps) {
  const baseEventPath = eventId ? `/e/${encodeURIComponent(eventId)}` : '/'

  return (
    <header className="vf-header">
      <div className="vf-header-left">
        <div className="vf-brand">
          <span className="vf-brand-mark" aria-hidden="true">◈</span>
          <span className="vf-brand-name">VeriFact</span>
        </div>
        <div className="vf-header-meta">
          <h1 className="vf-event-title">{eventName ?? 'Select an event'}</h1>
          <div className="vf-event-submeta">
            <span>T0: {formatUtcTimestamp(t0Utc)}</span>
            <span aria-hidden="true">·</span>
            <span>{formatIstTimestamp(t0Utc)}</span>
            {configFile ? (
              <>
                <span aria-hidden="true">·</span>
                <code className="vf-config-pill">{configFile}</code>
              </>
            ) : null}
          </div>
        </div>
      </div>

      <nav className="vf-nav" aria-label="Workspace navigation">
        {isLive ? (
          <span className="vf-live-badge" aria-label="Live ingestion active">
            <span className="vf-live-dot" aria-hidden="true" />
            Live
          </span>
        ) : null}
        <NavLink
          to={baseEventPath}
          end
          className={({ isActive }) => `vf-nav-link ${isActive ? 'is-active' : ''}`}
        >
          Graph
        </NavLink>
        <NavLink
          to={eventId ? `${baseEventPath}/before-after` : '/'}
          className={({ isActive }) => `vf-nav-link ${isActive ? 'is-active' : ''}`}
        >
          Before/after
        </NavLink>
        <NavLink
          to={eventId ? `${baseEventPath}/brief` : '/'}
          className={({ isActive }) => `vf-nav-link ${isActive ? 'is-active' : ''}`}
        >
          Brief
        </NavLink>
        <NavLink
          to={eventId ? `/check?event=${encodeURIComponent(eventId)}` : '/check'}
          className={({ isActive }) => `vf-nav-link ${isActive ? 'is-active' : ''}`}
        >
          Check a claim
        </NavLink>
      </nav>
    </header>
  )
}
