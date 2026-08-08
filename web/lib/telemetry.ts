/**
 * ShopForge browser telemetry tracker.
 *
 * Spec: docs/01-store-spec.md §6.1 and §6.3.
 *
 * Two ids:
 *   session_id  "s_" + 8 hex, one per browser session, kept in sessionStorage.
 *   trace_id    "t_" + 8 hex, one per USER INTERACTION (click / submit / navigation)
 *               and everything that interaction causes. The interaction window stays
 *               open while requests keep arriving and closes after 5s of silence.
 *               It is NOT one per HTTP request.
 *
 * Everything here is defensive: the tracker must never throw, never block rendering,
 * and must silently drop data rather than break the app it is watching.
 */

export type TelemetrySource = "web" | "api";

export type TelemetryKind =
  | "click"
  | "nav"
  | "fetch"
  | "console"
  | "error"
  | "request"
  | "sql"
  | "business"
  | "vitals";

export type TelemetryLevel = "debug" | "info" | "warn" | "error";

export interface TelemetryEvent {
  ts: string;
  trace_id: string | null;
  session_id: string | null;
  user_id: number | null;
  source: TelemetrySource;
  kind: TelemetryKind;
  name: string;
  level: TelemetryLevel;
  duration_ms: number | null;
  attrs: Record<string, unknown>;
}

/* ------------------------------------------------------------------ config */

const COLLECTOR_URL =
  (typeof process !== "undefined" &&
    process.env &&
    process.env.NEXT_PUBLIC_COLLECTOR_URL) ||
  "http://localhost:8001";

const INGEST_URL = COLLECTOR_URL.replace(/\/+$/, "") + "/ingest";

/** Interaction window: closes after this many ms with no new requests. */
const INTERACTION_WINDOW_MS = 5000;
/** Buffer flush cadence. */
const FLUSH_INTERVAL_MS = 2000;
/** Hard cap on buffered events; oldest are dropped past this. */
const MAX_BUFFER = 500;
/** Max characters kept for any single free-text attr. */
const TEXT_LIMIT = 160;

const SESSION_KEY = "sf_telemetry_session_id";
const SESSION_META_KEY = "sf_telemetry_session_started";

/**
 * The harness control plane (spec §5). `api` already excludes these routes from
 * its own telemetry; the browser must exclude them too, for two reasons.
 *
 * 1. They are not user actions. A `fetch` event for a flag poll is noise in a
 *    timeline whose whole purpose is "what did this click cause".
 * 2. The flag poller runs every 5s and the interaction window is 5s. If a poll
 *    called `traceIdForRequest()` it would keep the current window alive
 *    forever, so a trace would never close and every later request would be
 *    stamped with a stale interaction's id. That silently destroys the "one
 *    trace_id per user interaction" contract, and it makes a click that fired
 *    NO request look like a click that fired two.
 */
const CONTROL_PLANE_RE = /\/api\/debug(\/|$|\?)/;

/**
 * Next's dev server polls for hot-module updates. Those are an artefact of
 * running in dev mode, not something the user or the app did, and they land in
 * whatever interaction window happens to be open. Drop them so the timeline
 * only contains requests the application actually made. RSC navigation fetches
 * under `/_next/` are deliberately NOT matched here — those are real
 * navigations and belong in the trace.
 */
const DEV_NOISE_RE = /(hot-update|webpack-hmr|__nextjs_original-stack-frame)/;

/* ------------------------------------------------------------------- utils */

function hex8(): string {
  try {
    const buf = new Uint8Array(4);
    if (typeof crypto !== "undefined" && crypto.getRandomValues) {
      crypto.getRandomValues(buf);
    } else {
      for (let i = 0; i < buf.length; i++) buf[i] = Math.floor(Math.random() * 256);
    }
    return Array.from(buf)
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  } catch {
    return Math.floor(Math.random() * 0xffffffff)
      .toString(16)
      .padStart(8, "0");
  }
}

function newSessionId(): string {
  return "s_" + hex8();
}

function newTraceId(): string {
  return "t_" + hex8();
}

function nowIso(): string {
  return new Date().toISOString();
}

function clamp(value: unknown, limit = TEXT_LIMIT): string {
  let s: string;
  if (typeof value === "string") s = value;
  else if (value === null || value === undefined) return "";
  else {
    try {
      s = String(value);
    } catch {
      return "";
    }
  }
  s = s.replace(/\s+/g, " ").trim();
  return s.length > limit ? s.slice(0, limit) + "…" : s;
}

function safeNumber(n: unknown): number | null {
  return typeof n === "number" && isFinite(n) ? Math.round(n * 1000) / 1000 : null;
}

/* --------------------------------------------------------------- tracker */

interface InteractionWindow {
  id: string;
  reason: string;
  lastActivity: number;
}

class Tracker {
  private started = false;
  private buffer: TelemetryEvent[] = [];
  private sessionId: string | null = null;
  private userId: number | null = null;
  private interaction: InteractionWindow | null = null;
  private flushTimer: ReturnType<typeof setInterval> | null = null;
  private currentRoute = "";
  private dropped = 0;
  private flushing = false;

  /** Captured before we patch anything, so telemetry delivery is never self-observing. */
  private nativeFetch: typeof fetch | null = null;
  private nativeConsoleError: ((...args: unknown[]) => void) | null = null;
  private nativeConsoleWarn: ((...args: unknown[]) => void) | null = null;

  /** Elements that have registered a click listener, for "did a listener run". */
  private clickListenerCounts = new WeakMap<EventTarget, number>();

  /* ---------------------------------------------------------------- ids */

  getSessionId(): string {
    if (this.sessionId) return this.sessionId;
    let id: string | null = null;
    try {
      id = window.sessionStorage.getItem(SESSION_KEY);
    } catch {
      id = null;
    }
    if (!id || !/^s_[0-9a-f]{8}$/.test(id)) {
      id = newSessionId();
      try {
        window.sessionStorage.setItem(SESSION_KEY, id);
      } catch {
        /* private mode: keep it in memory only */
      }
    }
    this.sessionId = id;
    return id;
  }

  /** Opens a fresh interaction window. Every request it causes reuses this id. */
  openInteraction(reason: string): string {
    const id = newTraceId();
    this.interaction = { id, reason, lastActivity: Date.now() };
    return id;
  }

  /**
   * The trace id in force right now, or null if the window has expired.
   * Does not mint.
   */
  peekTraceId(): string | null {
    const w = this.interaction;
    if (!w) return null;
    if (Date.now() - w.lastActivity > INTERACTION_WINDOW_MS) {
      this.interaction = null;
      return null;
    }
    return w.id;
  }

  /**
   * The trace id an outgoing request should carry. Extends the live window, or
   * opens a fresh one when a request happens with no interaction in flight
   * (page-load fetches, timers) so the request is still joinable end to end.
   */
  traceIdForRequest(): string {
    const live = this.peekTraceId();
    if (live) {
      (this.interaction as InteractionWindow).lastActivity = Date.now();
      return live;
    }
    return this.openInteraction("request");
  }

  setUserId(id: number | null): void {
    this.userId = typeof id === "number" ? id : null;
  }

  getUserId(): number | null {
    return this.userId;
  }

  /* -------------------------------------------------------------- record */

  record(
    kind: TelemetryKind,
    name: string,
    attrs: Record<string, unknown> = {},
    opts: {
      level?: TelemetryLevel;
      duration_ms?: number | null;
      trace_id?: string | null;
    } = {}
  ): void {
    try {
      const ev: TelemetryEvent = {
        ts: nowIso(),
        trace_id:
          opts.trace_id !== undefined ? opts.trace_id : this.peekTraceId(),
        session_id: this.getSessionId(),
        user_id: this.userId,
        source: "web",
        kind,
        name,
        level: opts.level || "info",
        duration_ms:
          opts.duration_ms === undefined ? null : safeNumber(opts.duration_ms),
        attrs: attrs || {},
      };
      if (this.buffer.length >= MAX_BUFFER) {
        this.buffer.shift();
        this.dropped++;
      }
      this.buffer.push(ev);
    } catch {
      /* telemetry must never throw */
    }
  }

  /* --------------------------------------------------------------- flush */

  flush(useBeacon = false): void {
    if (this.buffer.length === 0) return;
    if (this.flushing && !useBeacon) return;

    const batch = this.buffer;
    this.buffer = [];
    const dropped = this.dropped;
    this.dropped = 0;

    let body: string;
    try {
      body = JSON.stringify({ events: batch, dropped });
    } catch {
      return;
    }

    if (useBeacon) {
      try {
        if (navigator && typeof navigator.sendBeacon === "function") {
          const blob = new Blob([body], { type: "application/json" });
          if (navigator.sendBeacon(INGEST_URL, blob)) return;
        }
      } catch {
        /* fall through to fetch */
      }
    }

    const f = this.nativeFetch;
    if (!f) return;
    this.flushing = true;
    try {
      f(INGEST_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
        keepalive: true,
        mode: "cors",
        credentials: "omit",
      })
        .catch(() => {
          /* silently drop */
        })
        .finally(() => {
          this.flushing = false;
        });
    } catch {
      this.flushing = false;
    }
  }

  /* --------------------------------------------------------------- start */

  start(): void {
    if (this.started) return;
    if (typeof window === "undefined" || typeof document === "undefined") return;
    this.started = true;

    try {
      this.nativeFetch = window.fetch.bind(window);
    } catch {
      this.nativeFetch = null;
    }
    this.nativeConsoleError = console.error.bind(console);
    this.nativeConsoleWarn = console.warn.bind(console);

    this.getSessionId();
    this.currentRoute = location.pathname + location.search;

    this.installListenerTracking();
    this.installClickCapture();
    this.installSubmitCapture();
    this.installNavTracking();
    this.installFetchWrap();
    this.installConsolePatch();
    this.installErrorHandlers();

    this.openInteraction("page_load");
    this.emitSessionMeta();
    this.emitVitals();

    this.record("nav", "page_load", {
      from: null,
      to: this.currentRoute,
      referrer: clamp(document.referrer, 300),
    });

    this.flushTimer = setInterval(() => this.flush(false), FLUSH_INTERVAL_MS);

    window.addEventListener("beforeunload", () => this.flush(true));
    window.addEventListener("pagehide", () => this.flush(true));
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") this.flush(true);
    });
  }

  /* ---------------------------------------------------------- session meta */

  private emitSessionMeta(): void {
    let already = false;
    try {
      already = window.sessionStorage.getItem(SESSION_META_KEY) === "1";
    } catch {
      already = false;
    }
    if (already) return;
    try {
      window.sessionStorage.setItem(SESSION_META_KEY, "1");
    } catch {
      /* ignore */
    }

    this.record("vitals", "session_start", {
      viewport_w: window.innerWidth,
      viewport_h: window.innerHeight,
      screen_w: window.screen ? window.screen.width : null,
      screen_h: window.screen ? window.screen.height : null,
      device_pixel_ratio: window.devicePixelRatio || 1,
      user_agent: clamp(navigator.userAgent, 300),
      locale: navigator.language || null,
      languages: navigator.languages ? navigator.languages.slice(0, 5) : null,
      timezone: (() => {
        try {
          return Intl.DateTimeFormat().resolvedOptions().timeZone;
        } catch {
          return null;
        }
      })(),
      url: clamp(location.href, 300),
      collector: INGEST_URL,
    });
  }

  /* --------------------------------------------------------------- vitals */

  private emitVitals(): void {
    const emit = () => {
      try {
        const nav = (performance.getEntriesByType("navigation") ||
          [])[0] as PerformanceNavigationTiming | undefined;
        const paints = performance.getEntriesByType("paint") || [];
        const fp = paints.find((p) => p.name === "first-paint");
        const fcp = paints.find((p) => p.name === "first-contentful-paint");

        const attrs: Record<string, unknown> = {
          route: this.currentRoute,
          first_paint_ms: fp ? safeNumber(fp.startTime) : null,
          first_contentful_paint_ms: fcp ? safeNumber(fcp.startTime) : null,
          viewport_w: window.innerWidth,
          viewport_h: window.innerHeight,
        };
        if (nav) {
          attrs.dns_ms = safeNumber(nav.domainLookupEnd - nav.domainLookupStart);
          attrs.tcp_ms = safeNumber(nav.connectEnd - nav.connectStart);
          attrs.ttfb_ms = safeNumber(nav.responseStart - nav.requestStart);
          attrs.response_ms = safeNumber(nav.responseEnd - nav.responseStart);
          attrs.dom_interactive_ms = safeNumber(nav.domInteractive);
          attrs.dom_content_loaded_ms = safeNumber(
            nav.domContentLoadedEventEnd
          );
          attrs.load_event_ms = safeNumber(nav.loadEventEnd);
          attrs.transfer_size = safeNumber(nav.transferSize);
          attrs.nav_type = nav.type;
        }
        const duration = nav ? nav.loadEventEnd || nav.domComplete : null;
        this.record("vitals", "page_load_timing", attrs, {
          duration_ms: duration ? safeNumber(duration) : null,
        });
      } catch {
        /* ignore */
      }
    };

    if (document.readyState === "complete") {
      setTimeout(emit, 0);
    } else {
      window.addEventListener("load", () => setTimeout(emit, 0), { once: true });
    }
  }

  /* ------------------------------------------------- listener bookkeeping */

  /**
   * Patch add/removeEventListener so we can answer "was there any click handler
   * on the path at all". React 17+ delegates to the root container, so a hit on a
   * bare overlay shows zero listeners on the element itself — exactly the signal
   * BUG-002 needs.
   */
  private installListenerTracking(): void {
    const counts = this.clickListenerCounts;
    const proto = EventTarget.prototype;
    const origAdd = proto.addEventListener;
    const origRemove = proto.removeEventListener;

    proto.addEventListener = function (
      this: EventTarget,
      type: string,
      listener: EventListenerOrEventListenerObject | null,
      options?: boolean | AddEventListenerOptions
    ) {
      try {
        if (type === "click" && listener) {
          counts.set(this, (counts.get(this) || 0) + 1);
        }
      } catch {
        /* ignore */
      }
      return origAdd.call(this, type, listener as EventListener, options);
    };

    proto.removeEventListener = function (
      this: EventTarget,
      type: string,
      listener: EventListenerOrEventListenerObject | null,
      options?: boolean | EventListenerOptions
    ) {
      try {
        if (type === "click" && listener) {
          const c = counts.get(this) || 0;
          if (c > 0) counts.set(this, c - 1);
        }
      } catch {
        /* ignore */
      }
      return origRemove.call(this, type, listener as EventListener, options);
    };
  }

  /* ---------------------------------------------------------- describe DOM */

  private selectorPath(el: Element | null): string {
    const parts: string[] = [];
    let node: Element | null = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 12) {
      const tag = node.tagName.toLowerCase();
      if (tag === "html" || tag === "body") {
        parts.unshift(tag);
        break;
      }
      let part = tag;
      const testid = node.getAttribute && node.getAttribute("data-testid");
      if (testid) {
        part += `[data-testid="${testid}"]`;
        parts.unshift(part);
        break;
      }
      if (node.id) {
        part += `#${node.id}`;
        parts.unshift(part);
        break;
      }
      const cls = (node.getAttribute("class") || "")
        .split(/\s+/)
        .filter(Boolean)
        .slice(0, 2);
      if (cls.length) part += "." + cls.join(".");
      const parent: Element | null = node.parentElement;
      if (parent) {
        const sibs = Array.prototype.filter.call(
          parent.children,
          (c: Element) => c.tagName === node!.tagName
        ) as Element[];
        if (sibs.length > 1) {
          part += `:nth-of-type(${sibs.indexOf(node) + 1})`;
        }
      }
      parts.unshift(part);
      node = parent;
      depth++;
    }
    return parts.join(" > ");
  }

  private describeElement(
    el: Element | null,
    withStyle = true
  ): Record<string, unknown> | null {
    if (!el || el.nodeType !== 1) return null;
    const out: Record<string, unknown> = {
      tag: el.tagName.toLowerCase(),
      testid: el.getAttribute("data-testid") || null,
      id: el.id || null,
      classes: clamp(el.getAttribute("class") || "", 200) || null,
      role: el.getAttribute("role") || null,
      type: el.getAttribute("type") || null,
      href: el.getAttribute("href") || null,
      disabled: (el as HTMLButtonElement).disabled === true,
      text: clamp((el as HTMLElement).innerText || el.textContent || "", 120),
      selector: this.selectorPath(el),
    };
    try {
      const r = el.getBoundingClientRect();
      out.rect = {
        x: Math.round(r.x),
        y: Math.round(r.y),
        w: Math.round(r.width),
        h: Math.round(r.height),
      };
    } catch {
      out.rect = null;
    }
    if (withStyle) {
      try {
        const cs = getComputedStyle(el);
        out.z_index = cs.zIndex;
        out.position = cs.position;
        out.pointer_events = cs.pointerEvents;
        out.opacity = cs.opacity;
        out.display = cs.display;
        out.visibility = cs.visibility;
        out.background = clamp(cs.backgroundColor, 40);
      } catch {
        /* ignore */
      }
    }
    return out;
  }

  private isInteractive(el: Element | null): boolean {
    if (!el || el.nodeType !== 1) return false;
    const tag = el.tagName.toLowerCase();
    if (["button", "a", "input", "select", "textarea", "label"].includes(tag)) {
      return true;
    }
    const role = el.getAttribute("role");
    if (role && ["button", "link", "menuitem", "tab", "checkbox"].includes(role)) {
      return true;
    }
    if (el.hasAttribute("data-testid")) return true;
    if (el.hasAttribute("onclick")) return true;
    if ((this.clickListenerCounts.get(el) || 0) > 0) return true;
    return false;
  }

  private interactiveAncestor(el: Element | null): Element | null {
    let node: Element | null = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 12) {
      if (this.isInteractive(node)) return node;
      node = node.parentElement;
      depth++;
    }
    return null;
  }

  /* ---------------------------------------------------------------- click */

  /**
   * Capture-phase, document-level. Fires before any handler, so it records the
   * click even when the intended handler never runs — and it records the element
   * ACTUALLY hit, which is the whole diagnosis for an invisible overlay.
   */
  private installClickCapture(): void {
    document.addEventListener(
      "click",
      (ev: MouseEvent) => {
        try {
          this.handleClick(ev);
        } catch {
          /* never break the page */
        }
      },
      true
    );

    // Bubble phase on document: if this never runs, propagation was stopped.
    document.addEventListener("click", (ev: MouseEvent) => {
      try {
        (ev as unknown as Record<string, unknown>).__sfReachedDocument = true;
      } catch {
        /* ignore */
      }
    });
  }

  private handleClick(ev: MouseEvent): void {
    const traceId = this.openInteraction("click");
    const hit = (ev.target as Element) || null;

    // Listener presence along the composed path.
    let listenersOnPath = 0;
    let listenerOnHit = 0;
    const path: EventTarget[] =
      typeof (ev as Event & { composedPath?: () => EventTarget[] })
        .composedPath === "function"
        ? (ev as Event & { composedPath: () => EventTarget[] }).composedPath()
        : [];
    for (const t of path) {
      const c = this.clickListenerCounts.get(t) || 0;
      listenersOnPath += c;
    }
    if (hit) listenerOnHit = this.clickListenerCounts.get(hit) || 0;

    // What is stacked under the pointer, top first. This is how an invisible
    // overlay eating a click becomes a one-line diagnosis.
    const stack: Array<Record<string, unknown>> = [];
    let obscuredInteractive: Record<string, unknown> | null = null;
    try {
      const els = document.elementsFromPoint(ev.clientX, ev.clientY) || [];
      for (let i = 0; i < Math.min(els.length, 6); i++) {
        const d = this.describeElement(els[i]);
        if (d) stack.push(d);
      }
      const intendedTop = this.interactiveAncestor(hit);
      for (let i = 1; i < Math.min(els.length, 8); i++) {
        const cand = els[i];
        if (!this.isInteractive(cand)) continue;
        // An interactive thing sitting under the pointer that the click never
        // reached, because something above it swallowed the hit.
        if (intendedTop && (cand === intendedTop || cand.contains(intendedTop))) {
          break;
        }
        if (hit && cand.contains(hit)) break;
        obscuredInteractive = this.describeElement(cand);
        break;
      }
    } catch {
      /* ignore */
    }

    const hitInfo = this.describeElement(hit);
    const intended = this.interactiveAncestor(hit);
    const intendedInfo =
      intended && intended !== hit ? this.describeElement(intended) : null;

    const attrs: Record<string, unknown> = {
      selector: hitInfo ? hitInfo.selector : null,
      testid: hitInfo ? hitInfo.testid : null,
      text: hitInfo ? hitInfo.text : null,
      tag: hitInfo ? hitInfo.tag : null,
      // "the element ACTUALLY hit" — the point of this whole event
      hit_element: hitInfo,
      intended_target: intendedInfo,
      hit_is_intended_target: !intendedInfo,
      element_stack_at_point: stack,
      obscured_interactive_element: obscuredInteractive,
      click_blocked_by_overlay: !!obscuredInteractive,
      listeners_on_path: listenersOnPath,
      listeners_on_hit_element: listenerOnHit,
      client_x: ev.clientX,
      client_y: ev.clientY,
      page_x: ev.pageX,
      page_y: ev.pageY,
      button: ev.button,
      trusted: ev.isTrusted,
      viewport_w: window.innerWidth,
      viewport_h: window.innerHeight,
      route: this.currentRoute,
    };

    const name =
      (hitInfo && (hitInfo.testid as string)) ||
      (hitInfo && (hitInfo.selector as string)) ||
      "unknown";

    // Emit after the event has fully propagated so we know whether handlers ran
    // and whether the default was prevented.
    setTimeout(() => {
      try {
        const reached =
          (ev as unknown as Record<string, unknown>).__sfReachedDocument === true;
        attrs.propagation_reached_document = reached;
        attrs.propagation_stopped = !reached;
        attrs.default_prevented = ev.defaultPrevented === true;
        attrs.listener_ran = listenersOnPath > 0 && reached;
      } catch {
        /* ignore */
      }
      this.record("click", name, attrs, {
        trace_id: traceId,
        level: attrs.click_blocked_by_overlay ? "warn" : "info",
      });
    }, 0);
  }

  /* --------------------------------------------------------------- submit */

  private installSubmitCapture(): void {
    document.addEventListener(
      "submit",
      (ev: Event) => {
        try {
          const traceId = this.openInteraction("submit");
          const form = ev.target as HTMLFormElement | null;
          const info = this.describeElement(form);
          const fields: string[] = [];
          try {
            if (form && form.elements) {
              for (let i = 0; i < form.elements.length && i < 25; i++) {
                const el = form.elements[i] as HTMLInputElement;
                if (el && el.name) fields.push(el.name);
              }
            }
          } catch {
            /* ignore */
          }
          this.record(
            "click",
            (info && (info.testid as string)) || "form-submit",
            {
              submit: true,
              form: info,
              fields,
              method: form ? form.method : null,
              action: form ? clamp(form.action, 200) : null,
              route: this.currentRoute,
            },
            { trace_id: traceId }
          );
        } catch {
          /* ignore */
        }
      },
      true
    );
  }

  /* ------------------------------------------------------------------ nav */

  private installNavTracking(): void {
    const emitNav = (kind: string) => {
      try {
        const to = location.pathname + location.search;
        const from = this.currentRoute;
        if (to === from) return;
        this.currentRoute = to;
        const traceId = this.peekTraceId() || this.openInteraction("nav");
        this.record(
          "nav",
          "route_change",
          { from, to, via: kind, title: clamp(document.title, 120) },
          { trace_id: traceId }
        );
      } catch {
        /* ignore */
      }
    };

    try {
      const h = window.history;
      const origPush = h.pushState;
      const origReplace = h.replaceState;

      h.pushState = function (
        this: History,
        ...args: Parameters<History["pushState"]>
      ) {
        const r = origPush.apply(this, args);
        setTimeout(() => emitNav("pushState"), 0);
        return r;
      };
      h.replaceState = function (
        this: History,
        ...args: Parameters<History["replaceState"]>
      ) {
        const r = origReplace.apply(this, args);
        setTimeout(() => emitNav("replaceState"), 0);
        return r;
      };
    } catch {
      /* ignore */
    }

    window.addEventListener("popstate", () => emitNav("popstate"));
    window.addEventListener("hashchange", () => emitNav("hashchange"));

    // App Router can swap routes without a history call we see; poll cheaply.
    setInterval(() => emitNav("poll"), 500);
  }

  /* ---------------------------------------------------------------- fetch */

  private installFetchWrap(): void {
    const native = this.nativeFetch;
    if (!native) return;
    const self = this;

    window.fetch = function (
      input: RequestInfo | URL,
      init?: RequestInit
    ): Promise<Response> {
      let url = "";
      let method = "GET";
      try {
        if (typeof input === "string") url = input;
        else if (input instanceof URL) url = input.href;
        else if (input && typeof (input as Request).url === "string")
          url = (input as Request).url;
        method = (
          (init && init.method) ||
          (input && (input as Request).method) ||
          "GET"
        ).toUpperCase();
      } catch {
        /* ignore */
      }

      // Never observe or stamp our own telemetry delivery.
      if (url && url.indexOf(INGEST_URL) === 0) {
        return native(input as RequestInfo, init);
      }

      // Never observe, stamp, or extend the interaction window for the harness
      // control plane, or for the dev server's own housekeeping.
      if (url && (CONTROL_PLANE_RE.test(url) || DEV_NOISE_RE.test(url))) {
        return native(input as RequestInfo, init);
      }

      const isApi = /(^|\/)api\//.test(url) || url.indexOf("/api") === 0;
      let traceId: string | null = null;
      let nextInit = init;

      if (isApi) {
        try {
          traceId = self.traceIdForRequest();
          const headers = new Headers(
            (init && init.headers) ||
              (input instanceof Request ? input.headers : undefined) ||
              {}
          );
          headers.set("X-Trace-Id", traceId);
          headers.set("X-Session-Id", self.getSessionId());
          nextInit = { ...(init || {}), headers };
        } catch {
          nextInit = init;
        }
      }

      const started =
        typeof performance !== "undefined" ? performance.now() : Date.now();
      const reqBytes = estimateBodySize(nextInit && nextInit.body);

      const finish = (
        res: Response | null,
        err: unknown,
        respBytes: number | null
      ) => {
        const dur =
          (typeof performance !== "undefined" ? performance.now() : Date.now()) -
          started;
        const attrs: Record<string, unknown> = {
          method,
          url: clamp(url, 300),
          path: pathOf(url),
          status: res ? res.status : null,
          ok: res ? res.ok : false,
          status_text: res ? clamp(res.statusText, 60) : null,
          request_bytes: reqBytes,
          response_bytes: respBytes,
          trace_id_sent: traceId,
          session_id_sent: isApi ? self.getSessionId() : null,
          content_type: res ? res.headers.get("content-type") : null,
          route: self.currentRoute,
        };
        if (err) attrs.error = clamp((err as Error)?.message || err, 300);

        const level: TelemetryLevel = err
          ? "error"
          : res && res.status >= 500
          ? "error"
          : res && res.status >= 400
          ? "warn"
          : "info";

        self.record("fetch", `${method} ${pathOf(url)}`, attrs, {
          duration_ms: dur,
          level,
          trace_id: traceId || self.peekTraceId(),
        });
      };

      let p: Promise<Response>;
      try {
        p = native(input as RequestInfo, nextInit);
      } catch (e) {
        finish(null, e, null);
        return Promise.reject(e);
      }

      return p.then(
        (res) => {
          let respBytes: number | null = null;
          try {
            const cl = res.headers.get("content-length");
            if (cl) respBytes = parseInt(cl, 10);
          } catch {
            /* ignore */
          }
          if (respBytes === null) {
            try {
              const clone = res.clone();
              clone
                .arrayBuffer()
                .then((b) => finish(res, null, b.byteLength))
                .catch(() => finish(res, null, null));
              return res;
            } catch {
              finish(res, null, null);
              return res;
            }
          }
          finish(res, null, respBytes);
          return res;
        },
        (err) => {
          finish(null, err, null);
          throw err;
        }
      );
    } as typeof fetch;
  }

  /* -------------------------------------------------------------- console */

  private installConsolePatch(): void {
    const self = this;
    const origError = this.nativeConsoleError;
    const origWarn = this.nativeConsoleWarn;

    console.error = function (...args: unknown[]) {
      try {
        self.record(
          "console",
          "console.error",
          {
            message: clamp(args.map(fmtArg).join(" "), 600),
            arg_count: args.length,
            route: self.currentRoute,
            stack: firstStack(args),
          },
          { level: "error" }
        );
      } catch {
        /* ignore */
      }
      if (origError) origError(...args);
    };

    console.warn = function (...args: unknown[]) {
      try {
        self.record(
          "console",
          "console.warn",
          {
            message: clamp(args.map(fmtArg).join(" "), 600),
            arg_count: args.length,
            route: self.currentRoute,
          },
          { level: "warn" }
        );
      } catch {
        /* ignore */
      }
      if (origWarn) origWarn(...args);
    };
  }

  /* ---------------------------------------------------------------- error */

  private installErrorHandlers(): void {
    window.addEventListener("error", (ev: ErrorEvent) => {
      try {
        this.record(
          "error",
          clamp(ev.message || "window.onerror", 120) || "window.onerror",
          {
            message: clamp(ev.message, 400),
            filename: clamp(ev.filename, 300),
            lineno: ev.lineno ?? null,
            colno: ev.colno ?? null,
            stack: clamp(ev.error && ev.error.stack, 2000) || null,
            error_type: ev.error && ev.error.name ? ev.error.name : null,
            route: this.currentRoute,
          },
          { level: "error" }
        );
      } catch {
        /* ignore */
      }
    });

    window.addEventListener(
      "unhandledrejection",
      (ev: PromiseRejectionEvent) => {
        try {
          const reason = ev.reason;
          this.record(
            "error",
            "unhandledrejection",
            {
              message: clamp(
                (reason && (reason.message || reason)) || "unhandled rejection",
                400
              ),
              stack: clamp(reason && reason.stack, 2000) || null,
              error_type: reason && reason.name ? reason.name : typeof reason,
              route: this.currentRoute,
            },
            { level: "error" }
          );
        } catch {
          /* ignore */
        }
      }
    );
  }
}

/* -------------------------------------------------------------- helpers */

function pathOf(url: string): string {
  try {
    if (!url) return "";
    if (url.startsWith("/")) return url.split("?")[0];
    return new URL(url).pathname;
  } catch {
    return clamp(url, 120);
  }
}

function estimateBodySize(body: unknown): number | null {
  try {
    if (body === null || body === undefined) return 0;
    if (typeof body === "string") return new Blob([body]).size;
    if (body instanceof Blob) return body.size;
    if (body instanceof ArrayBuffer) return body.byteLength;
    if (ArrayBuffer.isView(body)) return (body as ArrayBufferView).byteLength;
    if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams)
      return new Blob([body.toString()]).size;
    if (typeof FormData !== "undefined" && body instanceof FormData) return null;
    return null;
  } catch {
    return null;
  }
}

function fmtArg(a: unknown): string {
  try {
    if (typeof a === "string") return a;
    if (a instanceof Error) return `${a.name}: ${a.message}`;
    return JSON.stringify(a);
  } catch {
    return String(a);
  }
}

function firstStack(args: unknown[]): string | null {
  for (const a of args) {
    if (a instanceof Error && a.stack) return clamp(a.stack, 2000);
  }
  return null;
}

/* ---------------------------------------------------------------- public */

const tracker = new Tracker();

/** Initialise the tracker. Safe to call more than once; only the first wins. */
export function initTelemetry(): void {
  try {
    tracker.start();
  } catch {
    /* never break the app */
  }
}

/** Current browser session id ("s_xxxxxxxx"). */
export function getSessionId(): string {
  return tracker.getSessionId();
}

/** The trace id currently in force, or null if the interaction window closed. */
export function getTraceId(): string | null {
  return tracker.peekTraceId();
}

/**
 * Open a new interaction window explicitly. Use for interactions that are not a
 * raw click or submit (keyboard shortcuts, programmatic flows).
 */
export function startInteraction(reason = "manual"): string {
  return tracker.openInteraction(reason);
}

/** Attach the logged-in user to every subsequent event. */
export function setTelemetryUser(userId: number | null): void {
  tracker.setUserId(userId);
}

/** Record an arbitrary event from app code. */
export function track(
  kind: TelemetryKind,
  name: string,
  attrs: Record<string, unknown> = {},
  opts: {
    level?: TelemetryLevel;
    duration_ms?: number | null;
    trace_id?: string | null;
  } = {}
): void {
  tracker.record(kind, name, attrs, opts);
}

/** Force a flush of the buffer. */
export function flushTelemetry(useBeacon = false): void {
  tracker.flush(useBeacon);
}

export const telemetry = {
  init: initTelemetry,
  track,
  flush: flushTelemetry,
  getSessionId,
  getTraceId,
  startInteraction,
  setUser: setTelemetryUser,
};

export default telemetry;
