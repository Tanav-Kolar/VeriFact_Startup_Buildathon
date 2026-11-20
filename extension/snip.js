// VeriFact Check: the snip overlay, injected by background.js after the right-click.
// Like the legacy extension: the page dims, the cursor becomes a crosshair, and dragging draws the
// area to check. The rectangle is sent in CSS pixels; background.js scales it for the screenshot.

(() => {
  "use strict";
  if (document.getElementById("verifact-selection-overlay")) return;

  const MIN_SIZE_PX = 8; // a stray click is not an area

  const overlay = document.createElement("div");
  overlay.id = "verifact-selection-overlay";
  Object.assign(overlay.style, {
    position: "fixed", top: "0", left: "0", width: "100%", height: "100%",
    background: "rgba(0, 0, 0, 0.5)", cursor: "crosshair", zIndex: "2147483646",
  });

  const box = document.createElement("div");
  Object.assign(box.style, {
    position: "absolute", border: "2px dashed #fff", background: "rgba(255, 255, 255, 0.2)",
    boxSizing: "border-box",
  });

  let startX = 0;
  let startY = 0;
  let dragging = false;

  function rectTo(event) {
    return {
      x: Math.min(startX, event.clientX),
      y: Math.min(startY, event.clientY),
      width: Math.abs(event.clientX - startX),
      height: Math.abs(event.clientY - startY),
    };
  }

  function cleanUp() {
    document.removeEventListener("keydown", onKeyDown, true);
    overlay.remove();
  }

  function onKeyDown(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      cleanUp();
    }
  }

  overlay.addEventListener("mousedown", (event) => {
    event.preventDefault();
    dragging = true;
    startX = event.clientX;
    startY = event.clientY;
    Object.assign(box.style, { left: `${startX}px`, top: `${startY}px`, width: "0px", height: "0px" });
    overlay.appendChild(box);
  });

  overlay.addEventListener("mousemove", (event) => {
    if (!dragging) return;
    const rect = rectTo(event);
    Object.assign(box.style, {
      left: `${rect.x}px`, top: `${rect.y}px`, width: `${rect.width}px`, height: `${rect.height}px`,
    });
  });

  overlay.addEventListener("mouseup", (event) => {
    if (!dragging) return;
    dragging = false;
    const area = rectTo(event);
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    cleanUp();
    if (area.width < MIN_SIZE_PX || area.height < MIN_SIZE_PX) return;
    // Let the page repaint without the dim layer before the screenshot is taken. The legacy
    // extension captured straight away, which could leave the overlay in the picture.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      chrome.runtime.sendMessage({ type: "vf-snip", area, viewport });
    }));
  });

  document.addEventListener("keydown", onKeyDown, true);
  document.documentElement.appendChild(overlay);
})();
