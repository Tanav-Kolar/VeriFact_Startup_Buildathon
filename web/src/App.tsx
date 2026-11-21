import { BrowserRouter, HashRouter, Navigate, Route, Routes } from 'react-router-dom'
import { isStaticMode } from './api/client'
import { BeforeAfterPage } from './pages/BeforeAfterPage'
import { BriefPage } from './pages/BriefPage'
import { CheckPage } from './pages/CheckPage'
import { EventRedirectPage } from './pages/EventRedirectPage'
import { WorkspacePage } from './pages/WorkspacePage'

export function App() {
  const Router = isStaticMode() ? HashRouter : BrowserRouter
  return (
    <Router>
      <Routes>
        <Route path="/" element={<EventRedirectPage />} />
        <Route path="/e/:eventId" element={<WorkspacePage />} />
        <Route path="/e/:eventId/before-after" element={<BeforeAfterPage />} />
        <Route path="/e/:eventId/brief" element={<BriefPage />} />
        <Route path="/check" element={<CheckPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Router>
  )
}
export default App
