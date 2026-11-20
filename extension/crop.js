// Maps a rectangle drawn in CSS pixels onto the captured screenshot.
//
// chrome.tabs.captureVisibleTab returns the page in DEVICE pixels. On a Retina screen
// (devicePixelRatio 2) that bitmap is twice as wide as the viewport, so a rectangle in CSS pixels
// has to be scaled up before cropping. The legacy extension skipped this and cut the wrong region.
// Scaling by bitmap size over viewport size is the same as devicePixelRatio, and it also holds
// when the page is zoomed.
function cropRect(area, viewport, bitmap) {
  const scaleX = bitmap.width / viewport.width;
  const scaleY = bitmap.height / viewport.height;
  const sx = Math.min(Math.max(Math.round(area.x * scaleX), 0), bitmap.width - 1);
  const sy = Math.min(Math.max(Math.round(area.y * scaleY), 0), bitmap.height - 1);
  const sw = Math.min(Math.round(area.width * scaleX), bitmap.width - sx);
  const sh = Math.min(Math.round(area.height * scaleY), bitmap.height - sy);
  if (!(sw >= 1 && sh >= 1)) return null; // empty, inverted or not a number
  return { sx, sy, sw, sh };
}

// Loaded with importScripts() in the service worker, and with require() in the tests.
if (typeof module !== "undefined") module.exports = { cropRect };
