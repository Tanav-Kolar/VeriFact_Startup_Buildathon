// VeriFact Check: service worker.
// Right-click on selected text -> POST the selection to the Check service -> show the claim card.
// Only the selected text is sent. Never the page URL, cookies, or anything that identifies the user.

"use strict";

const MENU_ID = "verifact-check-selection";
// Chrome can stop a service worker whose fetch has gone unanswered for 30 s, which would leave the
// card on "Checking..." forever. Give up first, so the user always gets an answer or an error.
const REQUEST_TIMEOUT_MS = 25000;

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

// Accept https addresses, and http only for localhost, so selected text never travels in clear text
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

async function runCheck(checkBaseUrl, text) {
  if (!(await chrome.permissions.contains({ origins: [originPattern(checkBaseUrl)] }))) {
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

  let settings;
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
    settings = await getSettings();
  } catch (_err) {
    // Pages Chrome does not let extensions touch: chrome:// pages, the PDF viewer, the Web Store.
    await signalFailure(tab.id, "VeriFact Check cannot run on this page.");
    return;
  }
  await chrome.action.setBadgeText({ tabId: tab.id, text: "" }).catch(() => undefined);
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
