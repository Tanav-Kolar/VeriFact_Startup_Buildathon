// VeriFact Check: the loading and report windows, injected on demand by background.js.
//
// The look is the legacy extension's report window. The stylesheet below is the legacy
// display.css, unchanged apart from the Google Fonts @import (which would contact Google from every
// page the extension runs on). Everything is built with DOM calls and textContent: nothing from
// the service or the page is ever parsed as markup, so it cannot inject HTML or script.

(() => {
  "use strict";
  if (window.__verifactOverlay) return;
  window.__verifactOverlay = true;

  const CSS = `
:host { all: initial; }
.verifact-modal {
    position: fixed;
    top: 20px;
    right: 20px;
    background-color: white;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
    z-index: 2147483647;
    font-family: 'Inter', sans-serif;
    color: #1a202c;
    animation: slideIn 0.3s ease-out;
}
@keyframes slideIn {
    from { transform: translateX(100%); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
}
#verifact-loading-modal { width: 300px; padding: 24px; display: flex; align-items: center; gap: 16px; }
.loading-icon { width: 40px; height: 40px; animation: spin 1.5s linear infinite; flex-shrink: 0; }
@keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
.loading-text { font-size: 16px; font-weight: 500; color: #4a5568; }
#verifact-result-modal { width: 450px; max-height: calc(100vh - 40px); display: flex; flex-direction: column; }
#verifact-result-modal .header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid #e2e8f0; flex-shrink: 0; }
#verifact-result-modal .title { font-size: 16px; font-weight: 600; }
#verifact-result-modal .close-btn { background: none; border: none; font-size: 24px; line-height: 1; cursor: pointer; color: #a0aec0; padding: 4px; }
#verifact-result-modal .close-btn:hover { color: #4a5568; }
#verifact-result-modal .content { padding: 16px; overflow-y: auto; }
.summary-section { margin-bottom: 20px; }
.summary-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.summary-title { font-size: 16px; font-weight: 600; color: #2d3748; }
.summary-text { font-size: 14px; line-height: 1.6; color: #4a5568; }
.overall-tag { padding: 4px 10px; border-radius: 9999px; font-size: 12px; font-weight: 600; text-transform: uppercase; }
.tag-false { background-color: #fed7d7; color: #c53030; }
.tag-true { background-color: #c6f6d5; color: #2f855a; }
.tag-unverified { background-color: #feebc8; color: #b7791f; }
.tag-misleading { background-color: #feebc8; color: #b7791f; }
.tag-needs-context { background-color: #bee3f8; color: #2c5282; }
.tag-error { background-color: #e2e8f0; color: #4a5568; }
.claims-title { font-size: 16px; font-weight: 600; color: #2d3748; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #edf2f7; }
.claims-container { display: flex; flex-direction: column; gap: 16px; }
.no-claims { font-size: 14px; color: #718096; text-align: center; padding: 20px 0; }
.claim-card { background-color: #f7fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; }
.claim-text { font-size: 15px; font-weight: 600; color: #2d3748; margin-bottom: 16px; line-height: 1.5; }
.evidence-section { margin-top: 12px; }
.evidence-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; padding-bottom: 4px; margin-bottom: 8px; border-bottom: 1px solid #e2e8f0; }
.evidence-title.supporting { color: #38a169; }
.evidence-title.opposing { color: #e53e3e; }
.evidence-title.fact-checking { color: #4f46e5; }
.evidence-title.conclusion { color: #4a5568; margin-top: 16px; }
.evidence-list { display: flex; flex-direction: column; gap: 10px; }
.no-evidence { font-size: 13px; color: #a0aec0; font-style: italic; }
.evidence-item { font-size: 13px; line-height: 1.5; color: #4a5568; }
.evidence-summary { margin-bottom: 4px; }
.evidence-source { font-size: 12px; font-weight: 500; color: #4f46e5; text-decoration: none; word-break: break-all; }
.evidence-source:hover { text-decoration: underline; }
.fact-check-list { display: flex; flex-direction: column; gap: 10px; }
.fact-check-item { font-size: 13px; line-height: 1.5; color: #4a5568; background-color: #eef2ff; padding: 10px; border-radius: 6px; border-left: 3px solid #4f46e5; }
.fact-check-source-name { margin-bottom: 4px; font-weight: 600; color: #2d3748; font-size: 14px; }
.fact-check-summary { margin-bottom: 6px; color: #4a5568; }
.fact-check-source { font-size: 12px; font-weight: 500; color: #4f46e5; text-decoration: none; word-break: break-all; }
.fact-check-source:hover { text-decoration: underline; }
.claim-conclusion-section { margin-top: 12px; }
.claim-conclusion-text { font-size: 14px; line-height: 1.6; color: #2d3748; background-color: #edf2f7; padding: 10px; border-radius: 6px; }
.credibility-section { margin-top: 24px; padding-top: 20px; border-top: 1px solid #e2e8f0; }
.credibility-title { font-size: 16px; font-weight: 600; color: #2d3748; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #edf2f7; }
.credibility-container { display: flex; flex-direction: column; gap: 12px; }
.credibility-card { background-color: #f7fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; }
.credibility-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; gap: 10px; }
.credibility-url { font-size: 13px; font-weight: 500; color: #4f46e5; text-decoration: none; word-break: break-all; flex: 1; }
.credibility-url:hover { text-decoration: underline; }
.credibility-score-wrapper { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.credibility-score { font-size: 18px; font-weight: 700; padding: 4px 10px; border-radius: 6px; background-color: #edf2f7; min-width: 45px; text-align: center; }
.credibility-score.score-high { color: #38a169; background-color: #c6f6d5; }
.credibility-score.score-medium-high { color: #4299e1; background-color: #bee3f8; }
.credibility-score.score-medium-low { color: #ed8936; background-color: #feebc8; }
.credibility-score.score-low { color: #e53e3e; background-color: #fed7d7; }
.credibility-category { padding: 4px 10px; border-radius: 9999px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.025em; }
.category-highly-credible, .category-high-credibility { background-color: #c6f6d5; color: #2f855a; }
.category-moderate, .category-moderate-credibility { background-color: #bee3f8; color: #2c5282; }
.category-low-credibility, .category-questionable { background-color: #feebc8; color: #b7791f; }
.category-unreliable { background-color: #fed7d7; color: #c53030; }
.credibility-details { margin-top: 10px; }
.credibility-flags { margin-bottom: 10px; }
.credibility-flags-title { font-size: 12px; font-weight: 600; color: #4a5568; margin-bottom: 4px; }
.credibility-flags-list { list-style: none; padding-left: 0; margin: 0; }
.credibility-flags-list li { font-size: 12px; color: #4a5568; padding-left: 16px; position: relative; line-height: 1.6; }
.credibility-flags-list li::before { content: '\\2713'; position: absolute; left: 0; color: #38a169; font-weight: 600; }
.credibility-reasoning { font-size: 13px; line-height: 1.6; color: #2d3748; background-color: #edf2f7; padding: 10px; border-radius: 6px; border-left: 3px solid #4f46e5; }
.no-credibility { font-size: 13px; color: #a0aec0; font-style: italic; text-align: center; padding: 16px 0; }
`;

  const DEEP_LINK = /^\/e\/[A-Za-z0-9._~-]+(\?[A-Za-z0-9._~%=&-]*)?$/;

  let host = null;
  let shadow = null;

  function el(tag, text, className, attrs) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = String(text);
    if (className) node.className = className;
    for (const [name, value] of Object.entries(attrs || {})) node.setAttribute(name, value);
    return node;
  }

  function add(parent, ...children) {
    children.forEach((child) => child && parent.appendChild(child));
    return parent;
  }

  function safeHttpUrl(value) {
    try {
      const url = new URL(String(value));
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
    } catch (_err) {
      return null;
    }
  }

  function link(text, href, className) {
    const safe = safeHttpUrl(href);
    if (!safe) return el("span", text, className);
    return el("a", text, className, { href: safe, target: "_blank", rel: "noopener noreferrer" });
  }

  // One window at a time, as in the legacy extension: showing a new one removes the old one.
  function mount(modal) {
    if (!host || !host.isConnected) {
      host = document.createElement("div");
      shadow = host.attachShadow({ mode: "closed" });
      add(shadow, el("style", CSS));
      document.documentElement.appendChild(host);
    }
    Array.from(shadow.children).forEach((child) => child.tagName !== "STYLE" && child.remove());
    shadow.appendChild(modal);
  }

  function showLoading(iconUrl) {
    const modal = el("div", null, "verifact-modal", { id: "verifact-loading-modal" });
    const icon = safeIconUrl(iconUrl);
    if (icon) add(modal, el("img", null, "loading-icon", { src: icon, alt: "Loading..." }));
    add(modal, el("span", "Analyzing content...", "loading-text"));
    mount(modal);
  }

  function safeIconUrl(value) {
    return typeof value === "string" && /^data:image\/(png|jpeg);base64,[A-Za-z0-9+/=]+$/.test(value) ? value : "";
  }

  function evidenceList(items, type) {
    if (!Array.isArray(items) || !items.length) return el("p", `No ${type} evidence found.`, "no-evidence");
    const list = el("div", null, "evidence-list");
    items.forEach((evidence, index) => {
      add(list, add(el("div", null, "evidence-item"),
        el("p", evidence.summary, "evidence-summary"),
        link(`Source [${index + 1}]`, evidence.source, "evidence-source")));
    });
    return list;
  }

  function section(titleText, titleClass, body) {
    return add(el("div", null, "evidence-section"), el("h4", titleText, `evidence-title ${titleClass}`), body);
  }

  function factChecks(results) {
    const valid = (Array.isArray(results) ? results : []).filter((fc) => fc && fc.url && fc.url !== "None");
    if (!valid.length) return null;
    const list = el("div", null, "fact-check-list");
    valid.forEach((fc) => add(list, add(el("div", null, "fact-check-item"),
      el("p", fc.source || "Fact-Check Source", "fact-check-source-name"),
      el("p", fc.summary || fc.inference || "No summary available.", "fact-check-summary"),
      link("View Source", fc.url, "fact-check-source"))));
    return section("Fact-Checking Results", "fact-checking", list);
  }

  function claimCard(claim) {
    const card = el("div", null, "claim-card");
    add(card, el("p", `"${claim.claim_text}"`, "claim-text"),
      section("Supporting Evidence", "supporting", evidenceList(claim.supporting_evidence, "supporting")),
      section("Opposing Evidence", "opposing", evidenceList(claim.opposing_evidence, "opposing")),
      factChecks(claim.fact_checking_results),
      add(el("div", null, "claim-conclusion-section"),
        el("h4", "Conclusion", "evidence-title conclusion"),
        el("p", claim.conclusion, "claim-conclusion-text")));
    return card;
  }

  function credibilityCard(source) {
    const score = Number(source.credibility_score);
    const scoreClass = score >= 80 ? "score-high" : score >= 60 ? "score-medium-high" : score >= 40 ? "score-medium-low" : "score-low";
    const categoryClass = String(source.category || "").toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "");
    const details = el("div", null, "credibility-details");
    if (Array.isArray(source.flags) && source.flags.length) {
      const flags = el("ul", null, "credibility-flags-list");
      source.flags.forEach((flag) => add(flags, el("li", flag)));
      add(details, add(el("div", null, "credibility-flags"), el("p", "Trust Indicators:", "credibility-flags-title"), flags));
    }
    add(details, el("p", source.reasoning, "credibility-reasoning"));
    return add(el("div", null, "credibility-card"),
      add(el("div", null, "credibility-header"),
        link(source.url, source.url, "credibility-url"),
        add(el("div", null, "credibility-score-wrapper"),
          el("span", Number.isFinite(score) ? score : "[n]", `credibility-score ${scoreClass}`),
          el("span", source.category, `credibility-category category-${categoryClass}`))),
      details);
  }

  function showReport(data, deepLinkHref) {
    const tag = data.tag || "Unverified";
    const tagClass = tag.toLowerCase().replace(" ", "-");
    const claims = Array.isArray(data.analyzed_claims) ? data.analyzed_claims : [];

    const claimsContainer = el("div", null, "claims-container");
    if (!claims.length) add(claimsContainer, el("p", "No specific claims were analyzed.", "no-claims"));
    claims.forEach((claim) => add(claimsContainer, claimCard(claim)));

    const content = add(el("div", null, "content"),
      add(el("div", null, "summary-section"),
        add(el("div", null, "summary-header"),
          el("h3", "Overall Assessment", "summary-title"),
          el("span", tag, `overall-tag tag-${tagClass}`)),
        el("p", data.overall_summary || "No summary provided.", "summary-text")),
      add(el("div", null, "claims-section"), el("h3", "Analyzed Claims", "claims-title"), claimsContainer));

    if (Array.isArray(data.source_credibility_summary)) {
      const sources = data.source_credibility_summary;
      const container = el("div", null, "credibility-container");
      if (!sources.length) add(container, el("p", "No source credibility data available.", "no-credibility"));
      sources.forEach((source) => add(container, credibilityCard(source)));
      add(content, add(el("div", null, "credibility-section"),
        el("h3", "Source Credibility Assessment", "credibility-title"), container));
    }
    if (deepLinkHref) {
      add(content, section("The whole story", "conclusion", link("See the whole story", deepLinkHref, "evidence-source")));
    }

    const close = el("button", "×", "close-btn", { id: "verifact-close-btn", type: "button", "aria-label": "Close" });
    const modal = el("div", null, "verifact-modal", { id: "verifact-result-modal" });
    add(modal, add(el("div", null, "header"), el("span", "✨ Analysis Report", "title"), close), content);
    close.addEventListener("click", () => modal.remove());
    mount(modal);
  }

  // The legacy error window: the report window with an "Error" tag.
  function showError(message) {
    showReport({ tag: "Error", overall_summary: `Could not complete the analysis. Details: ${message}`, analyzed_claims: [] }, null);
  }

  function deepLinkFor(result, webBaseUrl) {
    const claims = Array.isArray(result.claims) ? result.claims : [];
    const found = claims.find((c) => c && typeof c.deep_link === "string" && DEEP_LINK.test(c.deep_link));
    return found && webBaseUrl ? webBaseUrl + found.deep_link : null;
  }

  chrome.runtime.onMessage.addListener((message, sender) => {
    if (sender.id !== chrome.runtime.id || !message || typeof message !== "object") return;
    try {
      if (message.type === "vf-loading") {
        showLoading(message.iconUrl);
      } else if (message.type === "vf-error") {
        showError(message.message || "The check could not be completed.");
      } else if (message.type === "vf-result") {
        const result = message.result || {};
        if (result.report && typeof result.report === "object") {
          showReport(result.report, deepLinkFor(result, message.webBaseUrl));
        } else if (typeof result.notice === "string") {
          showReport({ tag: "Unverified", overall_summary: result.notice, analyzed_claims: [] }, null);
        } else {
          showError("The Check service sent an unreadable answer.");
        }
      }
    } catch (_err) {
      showError("The report could not be displayed.");
    }
  });
})();
