/**
 * PSR.ai Hidden Context Layer - ISOLATED world content script.
 *
 * Two jobs:
 *   1. Build an accessibility snapshot of whatever the user clicks - role,
 *      accessible name, states, selector path, ancestor chain - computed from
 *      the DOM so no chrome.debugger / CDP attachment is needed (that would
 *      raise the "is debugging this browser" infobar we are trying to avoid).
 *   2. Relay page-world errors that inject.js posts over window.postMessage.
 */

const CHANNEL = "psr-ai";
const MAX_TEXT = 200;
const MAX_HTML = 1500;
const MAX_ANCESTORS = 5;

// Retry schedule for messages that race the service worker's startup. An MV3
// worker is spun up on demand, and a sendMessage issued microseconds before it
// is ready fails outright - which is exactly when a page-load console.error
// happens. Without this, the first error of every page load is lost.
const RETRY_DELAYS_MS = [250, 1000, 3000];

function send(payload, attempt = 0) {
  try {
    chrome.runtime.sendMessage({ channel: CHANNEL, payload }, () => {
      const failed = chrome.runtime.lastError;
      if (failed && attempt < RETRY_DELAYS_MS.length) {
        setTimeout(() => send(payload, attempt + 1), RETRY_DELAYS_MS[attempt]);
      }
      // Reading lastError also suppresses the "Unchecked runtime.lastError"
      // console noise the browser would otherwise print on the page.
    });
  } catch {
    // Extension context invalidated (reload/update) - retrying cannot help.
  }
}

// ---------------------------------------------------------------------------
// Accessibility computation
// ---------------------------------------------------------------------------

// Implicit ARIA roles for the elements users actually click. Chrome exposes no
// getComputedRole() to content scripts, so the mapping lives here.
const IMPLICIT_ROLES = {
  a: "link", area: "link", button: "button", summary: "button",
  input: "textbox", textarea: "textbox", select: "combobox", option: "option",
  h1: "heading", h2: "heading", h3: "heading", h4: "heading", h5: "heading", h6: "heading",
  img: "img", nav: "navigation", main: "main", header: "banner", footer: "contentinfo",
  aside: "complementary", form: "form", table: "table", tr: "row", td: "cell", th: "columnheader",
  ul: "list", ol: "list", li: "listitem", dialog: "dialog", details: "group",
  progress: "progressbar", output: "status", fieldset: "group", legend: "legend",
};

const INPUT_TYPE_ROLES = {
  button: "button", submit: "button", reset: "button", image: "button",
  checkbox: "checkbox", radio: "radio", range: "slider", search: "searchbox",
  email: "textbox", tel: "textbox", text: "textbox", url: "textbox", number: "spinbutton",
};

function computeRole(el) {
  const explicit = el.getAttribute?.("role");
  if (explicit) return explicit.trim().split(/\s+/)[0];
  const tag = el.tagName?.toLowerCase();
  if (tag === "input") {
    return INPUT_TYPE_ROLES[(el.getAttribute("type") || "text").toLowerCase()] || "textbox";
  }
  if (tag === "a" && !el.hasAttribute("href")) return "generic";
  return IMPLICIT_ROLES[tag] || "generic";
}

/**
 * Accessible-name computation, in spec precedence order and stopping at the
 * first hit: aria-labelledby > aria-label > native label > alt/title/value >
 * text content.
 */
function computeAccessibleName(el) {
  const labelledBy = el.getAttribute?.("aria-labelledby");
  if (labelledBy) {
    const text = labelledBy
      .split(/\s+/)
      .map((id) => el.ownerDocument?.getElementById(id)?.textContent?.trim() || "")
      .filter(Boolean)
      .join(" ");
    if (text) return text;
  }

  const ariaLabel = el.getAttribute?.("aria-label");
  if (ariaLabel?.trim()) return ariaLabel.trim();

  if (el.labels?.length) {
    const text = [...el.labels].map((label) => label.textContent?.trim() || "").filter(Boolean).join(" ");
    if (text) return text;
  }

  for (const attribute of ["alt", "title", "placeholder", "value"]) {
    const value = el.getAttribute?.(attribute);
    if (value?.trim()) return value.trim();
  }

  return (el.textContent || "").replace(/\s+/g, " ").trim();
}

function computeStates(el) {
  const states = {};
  if (el.matches?.(":disabled") || el.getAttribute?.("aria-disabled") === "true") states.disabled = true;
  if (el.getAttribute?.("aria-expanded")) states.expanded = el.getAttribute("aria-expanded") === "true";
  if (el.getAttribute?.("aria-checked")) states.checked = el.getAttribute("aria-checked");
  else if (typeof el.checked === "boolean") states.checked = el.checked;
  if (el.getAttribute?.("aria-selected")) states.selected = el.getAttribute("aria-selected") === "true";
  if (el.getAttribute?.("aria-invalid") === "true") states.invalid = true;
  if (el.required === true) states.required = true;
  if (el.getAttribute?.("aria-busy") === "true") states.busy = true;

  const rect = el.getBoundingClientRect?.();
  if (rect) {
    states.visible = rect.width > 0 && rect.height > 0;
    states.rect = {
      x: Math.round(rect.x), y: Math.round(rect.y),
      w: Math.round(rect.width), h: Math.round(rect.height),
    };
  }
  return states;
}

/** A short, stable-ish CSS path: #id wins, then tag.class, then nth-of-type. */
function cssPath(el) {
  const parts = [];
  let node = el;
  while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 6) {
    if (node.id) {
      parts.unshift(`#${CSS.escape(node.id)}`);
      break;
    }
    let selector = node.tagName.toLowerCase();
    const className = (node.getAttribute("class") || "").trim().split(/\s+/).filter(Boolean)[0];
    if (className) selector += `.${CSS.escape(className)}`;
    const parent = node.parentElement;
    if (parent) {
      const twins = [...parent.children].filter((child) => child.tagName === node.tagName);
      if (twins.length > 1) selector += `:nth-of-type(${twins.indexOf(node) + 1})`;
    }
    parts.unshift(selector);
    node = node.parentElement;
  }
  return parts.join(" > ");
}

function ancestorChain(el) {
  const chain = [];
  let node = el.parentElement;
  while (node && chain.length < MAX_ANCESTORS) {
    chain.push({
      tag: node.tagName.toLowerCase(),
      role: computeRole(node),
      name: computeAccessibleName(node).slice(0, 80) || undefined,
      test_id: node.getAttribute("data-testid") || undefined,
    });
    node = node.parentElement;
  }
  return chain;
}

function snapshotElement(el) {
  return {
    tag: el.tagName.toLowerCase(),
    role: computeRole(el),
    name: computeAccessibleName(el).slice(0, MAX_TEXT),
    states: computeStates(el),
    selector: cssPath(el),
    test_id: el.getAttribute("data-testid") || el.getAttribute("data-test") || undefined,
    attributes: {
      id: el.id || undefined,
      class: (el.getAttribute("class") || "").slice(0, MAX_TEXT) || undefined,
      type: el.getAttribute("type") || undefined,
      href: el.getAttribute("href")?.slice(0, MAX_TEXT) || undefined,
      name: el.getAttribute("name") || undefined,
    },
    outer_html: (el.outerHTML || "").slice(0, MAX_HTML),
    ancestors: ancestorChain(el),
  };
}

// ---------------------------------------------------------------------------
// 3. Clicked element
// ---------------------------------------------------------------------------

document.addEventListener(
  "pointerdown",
  (event) => {
    const target = event.composedPath?.()[0] || event.target;
    if (!target || target.nodeType !== Node.ELEMENT_NODE) return;
    try {
      send({
        kind: "element_click",
        timestamp: new Date().toISOString(),
        page_url: location.href,
        page_title: document.title,
        viewport: { w: innerWidth, h: innerHeight },
        client: { x: Math.round(event.clientX), y: Math.round(event.clientY) },
        screen: { x: Math.round(event.screenX), y: Math.round(event.screenY) },
        element: snapshotElement(target),
      });
    } catch {
      // A hostile page can throw from any getter. Never break the page.
    }
  },
  { capture: true, passive: true },
);

// ---------------------------------------------------------------------------
// 2. Uncaught errors visible from the ISOLATED world
// ---------------------------------------------------------------------------

addEventListener(
  "error",
  (event) => {
    if (event.target && event.target !== window) {
      // Resource load failure (<img>, <script>, <link>) - webRequest also sees
      // it, but this tells us which element referenced it.
      const target = event.target;
      send({
        kind: "resource_error",
        timestamp: new Date().toISOString(),
        page_url: location.href,
        url: target.src || target.href || "",
        tag: target.tagName?.toLowerCase(),
      });
      return;
    }
    send({
      kind: "console_error",
      subtype: "uncaught_exception",
      timestamp: new Date().toISOString(),
      page_url: location.href,
      message: event.message || String(event.error || "Unknown error"),
      stack: event.error?.stack || "",
      source: `${event.filename || ""}:${event.lineno || 0}:${event.colno || 0}`,
    });
  },
  true,
);

addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  send({
    kind: "console_error",
    subtype: "unhandled_rejection",
    timestamp: new Date().toISOString(),
    page_url: location.href,
    message: reason?.message || String(reason),
    stack: reason?.stack || "",
  });
});

// ---------------------------------------------------------------------------
// Relay from the MAIN world (inject.js)
// ---------------------------------------------------------------------------

addEventListener("message", (event) => {
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.__psrai !== 1 || !data.payload) return;
  send({ ...data.payload, page_url: location.href });
});
