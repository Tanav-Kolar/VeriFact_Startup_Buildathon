// VeriFact Check: claim card overlay, injected on demand by background.js.
// Every string from the service or the page is written with textContent. Nothing here ever parses
// markup, so source text cannot inject HTML or script into the page.

(() => {
  "use strict";
  if (window.__verifactOverlay) return;
  window.__verifactOverlay = true;

  const STATUS = {
    supported: { icon: "●", label: "Supported" },
    contradicted: { icon: "○", label: "Contradicted" },
    mixed: { icon: "◐", label: "Mixed" },
    unverified: { icon: "◌", label: "Unverified" },
  };
  const STANCE = { supports: "Supports the claim", contradicts: "Contradicts the claim", neutral: "Neutral" };
  const SOURCE = {
    company_statement: "Company statement",
    press: "Press",
    social: "Social",
    investor_with_conflict: "Investor with disclosed conflict",
  };
  const DEEP_LINK = /^\/e\/[A-Za-z0-9._~-]+(\?[A-Za-z0-9._~%=&-]*)?$/;

  const CSS = `
    :host { all: initial; }
    .panel { position: fixed; top: 16px; right: 16px; z-index: 2147483647; width: 380px; max-width: calc(100vw - 32px);
      max-height: 80vh; overflow: auto; box-sizing: border-box; padding: 16px; border-radius: 12px;
      background: #0f1420; color: #e6e9f2; border: 1px solid #2a3350; box-shadow: 0 12px 40px rgba(0,0,0,.5);
      font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
    .panel:focus { outline: 2px solid #d4a64a; outline-offset: 2px; }
    h1 { margin: 0 24px 8px 0; font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: #9aa4c0; font-weight: 600; }
    .close { position: absolute; top: 8px; right: 8px; background: none; border: 0; color: #9aa4c0; font-size: 20px; cursor: pointer; padding: 4px 8px; }
    .close:focus-visible, a:focus-visible { outline: 2px solid #d4a64a; outline-offset: 2px; }
    .claim { margin: 0 0 12px; font-size: 15px; }
    .status { display: inline-block; padding: 2px 10px; border: 1px solid #5b6a99; border-radius: 999px; font-weight: 600; }
    .counts, .note, .muted { color: #9aa4c0; font-size: 13px; margin: 8px 0; }
    .tag { display: inline-block; margin: 0 6px 0 0; padding: 1px 8px; border: 1px solid #d4a64a; border-radius: 6px; color: #d4a64a; font-size: 12px; }
    ul { list-style: none; padding: 0; margin: 12px 0 0; }
    li { padding: 10px 0; border-top: 1px solid #2a3350; }
    .meta { font-size: 12px; color: #9aa4c0; margin-bottom: 4px; }
    blockquote { margin: 4px 0; padding-left: 10px; border-left: 2px solid #5b6a99; }
    blockquote.conflict { border-left-color: #d4a64a; }
    .disclosure { font-size: 12px; color: #d4a64a; margin: 2px 0; }
    a { color: #8fb4ff; }
    .story { display: inline-block; margin-top: 14px; padding: 8px 14px; border-radius: 8px; background: #1d2742; border: 1px solid #5b6a99; text-decoration: none; font-weight: 600; }
  `;

  let host = null;
  let panel = null;

  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function safeHttpUrl(value) {
    try {
      const url = new URL(String(value));
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
    } catch (_err) {
      return null;
    }
  }

  function close() {
    if (host) host.remove();
    host = panel = null;
  }

  function open() {
    if (host) {
      while (panel.lastChild) panel.removeChild(panel.lastChild);
    } else {
      host = document.createElement("div");
      const shadow = host.attachShadow({ mode: "closed" });
      const style = document.createElement("style");
      style.textContent = CSS;
      panel = el("div", null, "panel");
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-label", "VeriFact claim card");
      panel.tabIndex = -1;
      panel.addEventListener("keydown", (event) => {
        if (event.key === "Escape") close();
      });
      shadow.append(style, panel);
      document.documentElement.appendChild(host);
    }
    const button = el("button", "×", "close");
    button.type = "button";
    button.setAttribute("aria-label", "Close");
    button.addEventListener("click", close);
    panel.append(button, el("h1", "VeriFact Check"));
    return panel;
  }

  function showMessage(text) {
    open().append(el("p", text, "muted"));
    panel.focus();
  }

  function evidenceItem(evidence) {
    const item = el("li");
    item.append(el("div", `${SOURCE[evidence.source_type] || "Source"} · ${STANCE[evidence.stance] || "Neutral"}`, "meta"));
    const quote = el("blockquote", `“${evidence.span}”`);
    item.append(quote);
    if (evidence.conflict) {
      quote.className = "conflict";
      item.append(el("div", `Disclosed conflict: ${evidence.conflict.person}, ${evidence.conflict.relationship}`, "disclosure"));
    }
    const href = safeHttpUrl(evidence.url);
    if (href) {
      const link = el("a", new URL(href).hostname);
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.append(link);
    }
    return item;
  }

  function claimCard(card, webBaseUrl) {
    const root = el("section");
    root.append(el("p", card.text, "claim"));
    const status = STATUS[card.status] || STATUS.unverified;
    root.append(el("span", `${status.icon} Evidence: ${status.label}`, "status"));
    const c = card.counts || {};
    root.append(el("p", `${c.supporting ?? "[n]"} supporting · ${c.contradicting ?? "[n]"} contradicting · ${c.neutral ?? "[n]"} neutral`, "counts"));
    if (card.self_reported) {
      const note = el("p", null, "note");
      note.append(el("span", "Self-reported", "tag"), document.createTextNode(card.self_reported_note || ""));
      root.append(note);
    }
    if (Array.isArray(card.evidence) && card.evidence.length) {
      const list = el("ul");
      card.evidence.forEach((e) => list.append(evidenceItem(e)));
      root.append(list);
    } else {
      root.append(el("p", "No verifiable evidence was found for this claim.", "muted"));
    }
    if (typeof card.deep_link === "string" && DEEP_LINK.test(card.deep_link) && webBaseUrl) {
      const link = el("a", "See the whole story", "story");
      link.href = webBaseUrl + card.deep_link;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      root.append(link);
    }
    return root;
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === "vf-loading") {
      showMessage("Checking…");
    } else if (message.type === "vf-error") {
      showMessage(message.message || "The check could not be completed.");
    } else if (message.type === "vf-result") {
      const result = message.result || {};
      const view = open();
      if (result.notice) view.append(el("p", result.notice, "note"));
      const claims = Array.isArray(result.claims) ? result.claims : [];
      if (!claims.length && !result.notice) view.append(el("p", "No checkable factual claim was found in that selection.", "muted"));
      claims.forEach((card) => view.append(claimCard(card, message.webBaseUrl)));
      panel.focus();
    }
  });
})();
