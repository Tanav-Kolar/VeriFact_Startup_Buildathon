import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchEvents } from '../api/client'
import { Header } from '../components/Header'

export function EventRedirectPage() {
  const navigate = useNavigate()
  const [error, setError] = useState<string | null>(null)
  const [empty, setEmpty] = useState(false)

  useEffect(() => {
    let active = true
    fetchEvents()
      .then((events) => {
        if (!active) return
        if (events.length > 0) {
          navigate(`/e/${encodeURIComponent(events[0].event_id)}`, { replace: true })
        } else {
          setEmpty(true)
        }
      })
      .catch((err: unknown) => {
        if (!active) return
        setError(err instanceof Error ? err.message : 'Unable to reach API')
      })

    return () => {
      active = false
    }
  }, [navigate])

  return (
    <div className="vf-page-shell">
      <Header eventId={null} eventName="Narrative Workspace" t0Utc={null} configFile={null} />
      <main className="vf-center-status">
        {error ? (
          <div className="vf-status-card">
            <h2>API unavailable</h2>
            <p>{error}</p>
            <p className="vf-muted-empty">
              Start the API service or set <code>VITE_API_BASE_URL</code> in <code>web/.env</code>.
            </p>
          </div>
        ) : empty ? (
          <div className="vf-status-card">
            <h2>No events configured</h2>
            <p>Add an event YAML file under <code>events/</code> and reload.</p>
          </div>
        ) : (
          <div className="vf-status-card">
            <h2>Loading events…</h2>
          </div>
        )}
      </main>
    </div>
  )
}
