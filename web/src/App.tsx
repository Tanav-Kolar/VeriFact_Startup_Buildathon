import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { BeforeAfterPage } from './pages/BeforeAfterPage'
import { BriefPage } from './pages/BriefPage'
import { CheckPage } from './pages/CheckPage'
import { EventRedirectPage } from './pages/EventRedirectPage'
import { WorkspacePage } from './pages/WorkspacePage'

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<EventRedirectPage />} />
        <Route path="/e/:eventId" element={<WorkspacePage />} />
        <Route path="/e/:eventId/before-after" element={<BeforeAfterPage />} />
        <Route path="/e/:eventId/brief" element={<BriefPage />} />
        <Route path="/check" element={<CheckPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
export default App
