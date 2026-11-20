# VeriFact Web Frontend (`web/`)

Event-agnostic Nebula narrative workspace built with **Vite**, **React 18**, **TypeScript**, **Sigma.js (WebGL)**, and **graphology**.

## Features
- **Workspace (`/e/:eventId`):** Storyline rail, Sigma.js WebGL graph canvas, top-right clock, bottom-right legend, right inspector panel with reusable `ClaimCard`, keyboard-operable time slider (`ArrowLeft`/`ArrowRight`, `Space`, `R`, `Esc`), and trust strip.
- **Before / After (`/e/:eventId/before-after`):** Descriptive comparison around configured turning points (always labelled `"Descriptive comparison, not a causal estimate."`).
- **Brief (`/e/:eventId/brief`):** Printable A4 one-page brief layout.
- **Check a Claim (`/check`):** Consumer claim-checking view sharing the same `ClaimCard` component.

## Local Development

1. Copy the env template:
   ```bash
   cp .env.example .env
   # Set VITE_API_BASE_URL=http://127.0.0.1:8000
   ```
2. Install dependencies:
   ```bash
   npm install
   ```
3. Run the dev server (requires the local API on port 8000):
   ```bash
   npm run dev
   ```
4. Type-check and build for production:
   ```bash
   npm run typecheck
   npm run build
   ```

## Environment Variables
| Variable | Description | Default |
| --- | --- | --- |
| `VITE_API_BASE_URL` | Base URL of the VeriFact `api` service | `http://localhost:8000` |
| `VITE_CHECK_BASE_URL` | Optional base URL of the `check` service | Falls back to `VITE_API_BASE_URL` |

## Container Build (Cloud Run)
```bash
docker build \
  --build-arg VITE_API_BASE_URL=https://api.example.com \
  --build-arg VITE_CHECK_BASE_URL=https://check.example.com \
  -t verifact-web .
```
