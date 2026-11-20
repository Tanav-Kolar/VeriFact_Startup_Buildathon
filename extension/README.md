# VeriFact Check extension (Spec 15)

Select text on any page, right-click, choose **Check this claim with VeriFact**, and a claim card
opens on the page. **See the whole story** opens the matching storyline in the web app. It shows
evidence with counts, never a verdict.

## Install (unpacked)

1. `chrome://extensions` -> turn on Developer mode -> **Load unpacked** -> pick this folder.
2. The options page opens from the toolbar button. Enter the **Check service address** (the
   deployed `check` service, or `http://127.0.0.1:8081` locally) and the **Web app address**, then
   press **Save**. Chrome asks permission for that one address.

Both addresses start empty. Nothing is hardcoded. Addresses must be `https://`; `http://` is
accepted for `localhost` and `127.0.0.1` only, so selected text never travels in clear text to a
remote host.

If a page does not allow extensions (`chrome://` pages, the PDF viewer, the Web Store), the toolbar
button shows a `!` for that tab with the reason as its tooltip.

## What is sent

Only the selected text, as `POST {check address}/check` with `{"text": "..."}`. Never the page
address, cookies, an account, or any install identifier (the request uses `credentials: "omit"`
and no referrer).

## Files

| File | Role |
| --- | --- |
| `manifest.json` | MV3. Hosts are `optional_host_permissions`, granted per address from the options page |
| `background.js` | Context menu, calls `check`, passes the result to the page |
| `content.js` | The claim card overlay (closed shadow DOM). Text goes in with `textContent` only |
| `options.html`, `options.js` | The two addresses, stored in `chrome.storage.sync` |

The card follows `steering_docs/api.md`: status as a text label plus a shape icon (never colour
alone, never red/green), counts, a "Self-reported" tag, each quote with its disclosed conflict
beside it, and the source link.

## Not in v1

- The screenshot flow (crop with `devicePixelRatio`). The text flow ships first.
- The `&t={hour}` part of the deep link. The extension cannot know the hour without inventing it,
  so the link carries `storyline` and `claim` as the service returns them.
- Toolbar icons.
