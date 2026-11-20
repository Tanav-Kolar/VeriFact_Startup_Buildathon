// VeriFact Check: options page. Both addresses start empty; nothing is hardcoded in the extension.
"use strict";

const checkInput = document.getElementById("check");
const webInput = document.getElementById("web");
const status = document.getElementById("status");

// https addresses, and http only for localhost, so selected text never travels in clear text to a
// remote host. Returns the origin plus any path, without a trailing slash, or "".
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

// Chrome match patterns carry no port; this is the form the manifest declares.
function originPattern(base) {
  const url = new URL(base);
  return `${url.protocol}//${url.hostname}/*`;
}

chrome.storage.sync.get({ checkBaseUrl: "", webBaseUrl: "" }).then((stored) => {
  checkInput.value = stored.checkBaseUrl;
  webInput.value = stored.webBaseUrl;
});

document.getElementById("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const checkBaseUrl = cleanBase(checkInput.value);
  const webBaseUrl = webInput.value.trim() ? cleanBase(webInput.value) : "";
  if (!checkBaseUrl || (webInput.value.trim() && !webBaseUrl)) {
    status.textContent = "Enter full https:// addresses. http:// works only for localhost.";
    return;
  }
  // Called from the click's user gesture, as chrome.permissions.request requires.
  let granted = false;
  try {
    granted = await chrome.permissions.request({ origins: [originPattern(checkBaseUrl)] });
  } catch (_err) {
    status.textContent = "Chrome refused the permission request for that address.";
    return;
  }
  if (!granted) {
    status.textContent = "Permission for the Check service address was not granted, so it was not saved.";
    return;
  }
  await chrome.storage.sync.set({ checkBaseUrl, webBaseUrl });
  status.textContent = "Saved.";
});
