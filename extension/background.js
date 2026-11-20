// VeriFact Check: service worker.
// Right-click on selected text -> POST the selection to the Check service -> show the claim card.
// Only the selected text is sent. Never the page URL, cookies, or anything that identifies the user.

"use strict";

const MENU_ID = "verifact-check-selection";
const REQUEST_TIMEOUT_MS = 60000;

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ID,
      title: "Check this claim with VeriFact",
      contexts: ["selection"],
    });
  });
});

chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());

// Accept only http(s) addresses; return the origin plus any path, without a trailing slash.
function cleanBase(value) {
  try {
    const url = new URL(String(value || "").trim());
    if (url.protocol !== "https:" && url.protocol !== "http:") return "";
    return url.origin + url.pathname.replace(/\/+$/, "");
  } catch (_err) {
    return "";
  }
}

async function getSettings() {
  // Empty defaults: nothing is hardcoded, the user sets both addresses in the options page.
  const stored = await chrome.storage.sync.get({ checkBaseUrl: "", webBaseUrl: "" });
  return { checkBaseUrl: cleanBase(stored.checkBaseUrl), webBaseUrl: cleanBase(stored.webBaseUrl) };
}

function send(tabId, message) {
  return chrome.tabs.sendMessage(tabId, message).catch(() => undefined);
}

async function runCheck(checkBaseUrl, text) {
  const origin = new URL(checkBaseUrl).origin + "/*";
  if (!(await chrome.permissions.contains({ origins: [origin] }))) {
    throw new Error("The extension has no permission for that address yet. Open its options and press Save.");
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(`${checkBaseUrl}/check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
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
    return body;
  } catch (err) {
    if (err.name === "AbortError") throw new Error("The Check service took too long to answer.");
    if (err instanceof TypeError) throw new Error("Could not reach the Check service. Check the address in the options.");
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== MENU_ID || !tab || tab.id == null) return;
  const text = (info.selectionText || "").trim();
  if (!text) return;

  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  const settings = await getSettings();
  if (!settings.checkBaseUrl) {
    await send(tab.id, { type: "vf-error", message: "Set the Check service address in the extension options first." });
    return;
  }
  await send(tab.id, { type: "vf-loading" });
  try {
    const result = await runCheck(settings.checkBaseUrl, text);
    await send(tab.id, { type: "vf-result", result, webBaseUrl: settings.webBaseUrl });
  } catch (err) {
    await send(tab.id, { type: "vf-error", message: err.message });
  }
});
