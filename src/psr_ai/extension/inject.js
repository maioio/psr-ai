/**
 * PSR.ai Hidden Context Layer - MAIN world content script.
 *
 * Runs inside the page's own JS context (manifest `"world": "MAIN"`, Chrome
 * 111+), which is the only way to see things the extension world cannot:
 *
 *   - console.error / console.warn arguments as the page produced them
 *   - fetch() and XMLHttpRequest failures *with the response body*, which
 *     chrome.webRequest cannot expose in MV3 without attaching a debugger
 *
 * Declaring it in the manifest instead of injecting a <script> tag matters:
 * a page with a strict CSP would block the injected tag, and this is exactly
 * the kind of app whose errors are worth capturing.
 *
 * Hard rules: never throw into the page, never change observable behaviour,
 * never touch successful responses.
 */

(() => {
  if (window.__psrAiContextLayer) return;   // Guard against double injection.
  window.__psrAiContextLayer = true;

  const MAX_BODY = 2048;
  const MAX_ARG = 500;

  function post(payload) {
    try {
      window.postMessage({ __psrai: 1, payload }, "*");
    } catch {
      /* structured-clone failure - drop the event rather than break the page */
    }
  }

  function stringifyArg(arg) {
    try {
      if (arg instanceof Error) return `${arg.name}: ${arg.message}\n${arg.stack || ""}`;
      if (typeof arg === "string") return arg;
      if (typeof arg === "object" && arg !== null) return JSON.stringify(arg);
      return String(arg);
    } catch {
      return "[unserialisable]";
    }
  }

  // -------------------------------------------------------------------------
  // console.error / console.warn
  // -------------------------------------------------------------------------

  for (const level of ["error", "warn"]) {
    const original = console[level];
    if (typeof original !== "function") continue;
    console[level] = function (...args) {
      try {
        post({
          kind: "console_error",
          subtype: `console.${level}`,
          level,
          timestamp: new Date().toISOString(),
          message: args.map(stringifyArg).join(" ").slice(0, MAX_ARG * 2),
          stack: new Error().stack || "",
        });
      } catch {
        /* never let instrumentation break logging */
      }
      return original.apply(this, args);
    };
  }

  // -------------------------------------------------------------------------
  // fetch() - capture 4xx/5xx bodies and network rejections
  // -------------------------------------------------------------------------

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = async function (...args) {
      const started = performance.now();
      const request = args[0];
      const init = args[1] || {};
      const url = typeof request === "string" ? request : request?.url || "";
      const method = (init.method || request?.method || "GET").toUpperCase();

      try {
        const response = await originalFetch.apply(this, args);
        if (!response.ok) {
          let body = "";
          try {
            // Clone first: reading the original would consume the caller's body.
            body = (await response.clone().text()).slice(0, MAX_BODY);
          } catch {
            body = "[body unavailable]";
          }
          post({
            kind: "network_failure",
            source: "fetch",
            dedupe_key: `${method} ${url} ${response.status}`,
            timestamp: new Date().toISOString(),
            url,
            method,
            status: response.status,
            status_text: response.statusText,
            duration_ms: Math.round(performance.now() - started),
            response_body: body,
          });
        }
        return response;
      } catch (error) {
        post({
          kind: "network_failure",
          source: "fetch",
          dedupe_key: `${method} ${url} rejected`,
          timestamp: new Date().toISOString(),
          url,
          method,
          status: 0,
          error: String(error?.message || error),
          duration_ms: Math.round(performance.now() - started),
        });
        throw error;   // Preserve the page's own error handling exactly.
      }
    };
  }

  // -------------------------------------------------------------------------
  // XMLHttpRequest
  // -------------------------------------------------------------------------

  const OriginalXHR = window.XMLHttpRequest;
  if (typeof OriginalXHR === "function") {
    const open = OriginalXHR.prototype.open;
    const send = OriginalXHR.prototype.send;

    OriginalXHR.prototype.open = function (method, url, ...rest) {
      this.__psrai = { method: String(method || "GET").toUpperCase(), url: String(url || "") };
      return open.call(this, method, url, ...rest);
    };

    OriginalXHR.prototype.send = function (...args) {
      const meta = this.__psrai;
      if (meta) {
        meta.started = performance.now();
        this.addEventListener("loadend", () => {
          try {
            if (this.status === 0 || this.status >= 400) {
              let body = "";
              try {
                if (this.responseType === "" || this.responseType === "text") {
                  body = String(this.responseText || "").slice(0, MAX_BODY);
                }
              } catch {
                body = "[body unavailable]";
              }
              post({
                kind: "network_failure",
                source: "xhr",
                dedupe_key: `${meta.method} ${meta.url} ${this.status}`,
                timestamp: new Date().toISOString(),
                url: meta.url,
                method: meta.method,
                status: this.status,
                status_text: this.statusText,
                duration_ms: Math.round(performance.now() - meta.started),
                response_body: body,
              });
            }
          } catch {
            /* ignore */
          }
        });
      }
      return send.apply(this, args);
    };
  }
})();
