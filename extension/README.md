# VeriFact Check extension (Spec 15)

Works like the legacy extension. Right-click anywhere on a page, choose **Check this area with
VeriFact**, drag a rectangle over any part of the page, and a report window opens with the analysis
of the claims in that area. The cropped picture is read by Gemini on the Check service.

## Install (unpacked)

1. `chrome://extensions` -> turn on Developer mode -> **Load unpacked** -> pick this folder.
2. Pin the extension from the puzzle-piece menu and click it. The options page opens.
3. Enter the **Check service address** (the deployed `check` service, or `http://127.0.0.1:8081`
   locally) and, optionally, the **Web app address**, then press **Save**. Chrome asks permission
   for that one address.

Both addresses start empty. Nothing is hardcoded. Addresses must be `https://`; `http://` is
accepted for `localhost` and `127.0.0.1` only. After editing the code, press the reload icon on the
extension's card in `chrome://extensions`.

## How it works

| Step | Where |
| --- | --- |
| Right-click menu (page, selection, image, video) | `background.js` |
| Dim page, crosshair, drag a rectangle. `Esc` cancels | `snip.js` |
| Screenshot with `chrome.tabs.captureVisibleTab`, crop with `OffscreenCanvas` | `background.js` |
| Rectangle scaled by the screenshot's real size (`devicePixelRatio`) | `crop.js` |
| `POST {check address}/check` with the image only | `background.js` |
| Loading window and report window | `content.js` |

The report window is the legacy one: **Analysis Report** header, **Overall Assessment** with the tag,
**Analyzed Claims** (supporting and opposing evidence with source links, conclusion), and **Source
Credibility Assessment**. The stylesheet is the legacy `display.css`, in a closed shadow DOM so the
page's own CSS cannot change it.

## Differences from the legacy extension

- **Retina fix.** The legacy crop used CSS pixels on a device-pixel screenshot and cut the wrong
  region on Retina screens. `crop.js` scales the rectangle; it is tested for 1x, 2x and 3x.
- **No page address.** The legacy extension sent the tab's URL with the image. This one sends the
  image only (principle P5.4: no browsing URL beyond the content being checked).
- **No `innerHTML`.** The legacy report wrote model output as markup. This one builds the window
  with DOM calls and `textContent`, so a source or model reply cannot inject HTML or script.
- **The overlay is hidden before the screenshot,** so the dim layer cannot end up in the picture.
- **No Google Fonts request** on every page. The font falls back to the system sans-serif.
- **Menu title** is "Check this area with VeriFact" (the legacy one used a banned UI word).
- **Extras:** `Esc` cancels the snip, tiny drags are ignored, the newest check on a tab wins, pages
  that block extensions (`chrome://`, the PDF viewer) show a `!` on the toolbar button, and a
  "See the whole story" link appears when the service matched a claim from the event config.

## Deliberate deviation from the project rules

The report shows a verdict tag (True, False, Misleading, Partially True, Unverified), colour-banded
credibility scores, and red/green colours, exactly like the legacy extension. That breaks
principles P1.4 and P1.5 and overrules `DECISIONS.md` #6. It was chosen explicitly by the project
owner, but `project_context/DECISIONS.md` should record it.

## Not verified

Nothing here has run in a real browser yet. The crop maths is tested in Node. The overlay, the
screenshot, the service worker staying alive during a long request, and the report window's
appearance are untested.
