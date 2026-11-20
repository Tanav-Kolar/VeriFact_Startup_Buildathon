// VeriFact Check: options page. Both addresses start empty; nothing is hardcoded in the extension.
"use strict";

const checkInput = document.getElementById("check");
const webInput = document.getElementById("web");
const status = document.getElementById("status");

function cleanBase(value) {
  try {
    const url = new URL(value.trim());
    if (url.protocol !== "https:" && url.protocol !== "http:") return "";
    return url.origin + url.pathname.replace(/\/+$/, "");
  } catch (_err) {
    return "";
  }
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
    status.textContent = "Enter full http(s) addresses.";
    return;
  }
  // Called from the click's user gesture, as chrome.permissions.request requires.
  const granted = await chrome.permissions.request({ origins: [new URL(checkBaseUrl).origin + "/*"] });
  if (!granted) {
    status.textContent = "Permission for the Check service address was not granted, so it was not saved.";
    return;
  }
  await chrome.storage.sync.set({ checkBaseUrl, webBaseUrl });
  status.textContent = "Saved.";
});
