// VeriFact Check: service worker.
// Right-click -> snip an area of the page -> crop the screenshot -> POST the image to the Check
// service -> show the report. Only the cropped image is sent. Never the page URL, cookies, or
// anything that identifies the user.

"use strict";

importScripts("crop.js"); // defines cropRect()

const MENU_ID = "verifact-check-area";
// The service can take a while: it runs several model calls. Chrome stops a service worker that
// sits idle for 30 s, so a periodic extension API call keeps it alive while a request is open.
const REQUEST_TIMEOUT_MS = 90000;
const KEEP_ALIVE_MS = 20000;

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ID,
      title: "Check this area with VeriFact",
      contexts: ["page", "selection", "image", "video"],
    });
  });
});

chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());

// Accept https addresses, and http only for localhost, so the image never travels in clear text
// to a remote host. Returns the origin plus any path, without a trailing slash, or "".
function cleanBase(value) {
  try {
    const url = new URL(String(value || "").trim());
    const local = ["localhost", "127.0.0.1"].includes(url.hostname);
    if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) return "";
    return url.origin + url.pathname.replace(/\/+$/, "");
  } catch (_err) {
    return "";
  }
}

// The permission pattern for an address. Chrome match patterns carry no port, and this is the form
// the manifest declares in optional_host_permissions.
function originPattern(base) {
  const url = new URL(base);
  return `${url.protocol}//${url.hostname}/*`;
}

// Mark the toolbar button for this tab. The mark clears when the tab navigates.
async function signalFailure(tabId, message) {
  await chrome.action.setBadgeText({ tabId, text: "!" }).catch(() => undefined);
  await chrome.action.setTitle({ tabId, title: message }).catch(() => undefined);
}

async function getSettings() {
  // Empty defaults: nothing is hardcoded, the user sets both addresses in the options page.
  const stored = await chrome.storage.sync.get({ checkBaseUrl: "", webBaseUrl: "" });
  return { checkBaseUrl: cleanBase(stored.checkBaseUrl), webBaseUrl: cleanBase(stored.webBaseUrl) };
}

function send(tabId, message) {
  return chrome.tabs.sendMessage(tabId, message).catch(() => undefined);
}

// The loading spinner as a data URL, so the page never needs access to an extension URL.
async function loadingIconUrl() {
  try {
    const bytes = new Uint8Array(await (await fetch(chrome.runtime.getURL("icons/loading-icon.png"))).arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    // The legacy file is named .png but holds a JPEG, so go by its first bytes, not its name.
    const type = bytes[0] === 0xff && bytes[1] === 0xd8 ? "image/jpeg" : "image/png";
    return `data:${type};base64,${btoa(binary)}`;
  } catch (_err) {
    return "";
  }
}

// Crop the screenshot to the snipped area. cropRect scales the CSS-pixel rectangle by the
// screenshot's real size (devicePixelRatio), which the legacy extension did not.
async function cropToBlob(dataUrl, area, viewport) {
  const source = await (await fetch(dataUrl)).blob();
  const bitmap = await createImageBitmap(source);
  try {
    const rect = cropRect(area, viewport, { width: bitmap.width, height: bitmap.height });
    if (!rect) throw new Error("That area is too small to check. Drag a larger rectangle.");
    const canvas = new OffscreenCanvas(rect.sw, rect.sh);
    canvas.getContext("2d").drawImage(bitmap, rect.sx, rect.sy, rect.sw, rect.sh, 0, 0, rect.sw, rect.sh);
    return await canvas.convertToBlob({ type: "image/png" });
  } finally {
    bitmap.close();
  }
}

async function runCheck(checkBaseUrl, image) {
  if (!(await chrome.permissions.contains({ origins: [originPattern(checkBaseUrl)] }))) {
    throw new Error("The extension has no permission for that address yet. Open its options and press Save.");
  }
  const formData = new FormData();
  formData.append("image", image, "screenshot.png"); // the image only: no text, no page address
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  const keepAlive = setInterval(() => chrome.runtime.getPlatformInfo(), KEEP_ALIVE_MS);
  try {
    const response = await fetch(`${checkBaseUrl}/check`, {
      method: "POST",
      body: formData, // the browser sets the multipart Content-Type
      credentials: "omit",
      referrerPolicy: "no-referrer",
      cache: "no-store",
      signal: controller.signal,
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      const message = body && body.error && body.error.message;
      throw new Error(message || `The Check service answered with status ${response.status}.`);
    }
    if (!body || typeof body !== "object") throw new Error("The Check service sent an unreadable answer.");
    return body;
  } catch (err) {
    if (err.name === "AbortError") throw new Error("The Check service took too long to answer.");
    if (err instanceof TypeError) throw new Error("Could not reach the Check service. Check the address in the options.");
    throw err;
  } finally {
    clearTimeout(timer);
    clearInterval(keepAlive);
  }
}

// The right-click: load the report window, then start the snip overlay.
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== MENU_ID || !tab || tab.id == null) return;
  let settings;
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
    settings = await getSettings();
    if (settings.checkBaseUrl) {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["snip.js"] });
    }
  } catch (_err) {
    // Pages Chrome does not let extensions touch: chrome:// pages, the PDF viewer, the Web Store.
    await signalFailure(tab.id, "VeriFact Check cannot run on this page.");
    return;
  }
  await chrome.action.setBadgeText({ tabId: tab.id, text: "" }).catch(() => undefined);
  if (!settings.checkBaseUrl) {
    await send(tab.id, { type: "vf-error", message: "Set the Check service address in the extension options first." });
  }
});

// The newest snip on each tab wins, so a slow earlier check cannot overwrite a later one.
const latestSnip = new Map();
let snipCounter = 0;

async function handleSnip(message, tab) {
  const id = ++snipCounter;
  latestSnip.set(tab.id, id);
  const current = () => latestSnip.get(tab.id) === id;
  try {
    const settings = await getSettings();
    if (!settings.checkBaseUrl) throw new Error("Set the Check service address in the extension options first.");
    await send(tab.id, { type: "vf-loading", iconUrl: await loadingIconUrl() });
    const screenshot = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    const image = await cropToBlob(screenshot, message.area, message.viewport);
    const result = await runCheck(settings.checkBaseUrl, image);
    if (current()) await send(tab.id, { type: "vf-result", result, webBaseUrl: settings.webBaseUrl });
  } catch (err) {
    if (current()) await send(tab.id, { type: "vf-error", message: err.message });
  }
}

function isRect(value, keys) {
  return value && typeof value === "object" && keys.every((k) => Number.isFinite(value[k]));
}

chrome.runtime.onMessage.addListener((message, sender) => {
  if (sender.id !== chrome.runtime.id || !sender.tab) return;
  if (!message || message.type !== "vf-snip") return;
  if (!isRect(message.area, ["x", "y", "width", "height"]) || !isRect(message.viewport, ["width", "height"])) return;
  handleSnip(message, sender.tab);
});
