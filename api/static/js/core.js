// Shared helpers: DOM, formatting, storage, API, toasts, modals, routing.

export const $ = (s, root = document) => root.querySelector(s);
export const $$ = (s, root = document) => [...root.querySelectorAll(s)];
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

/** Main market indices (index tickers, not stocks): the index strip and the terminal's search use them. */
export const INDICES = [["^NSEI", "NIFTY 50"], ["^BSESN", "SENSEX"], ["^NSEBANK", "BANK NIFTY"], ["^CNXIT", "NIFTY IT"],
  ["^NSEMDCP50", "MIDCAP 50"], ["^INDIAVIX", "INDIA VIX"], ["^GSPC", "S&P 500"], ["^IXIC", "NASDAQ"]];

/** In-app link for a news item (opens the article page, not the publisher's site). */
const articles = new Map();
export function articleHref(item) {
  if (!item?.url) return "#/news";
  articles.set(item.url, item);
  try { sessionStorage.setItem(`kairo-article:${item.url}`, JSON.stringify(item)); } catch { /* storage unavailable */ }
  const syms = (item.symbols || []).join(",");
  // The headline rides along so the page still has it when opened from a shared link or a new tab.
  const t = item.title ? `&t=${encodeURIComponent(item.title.slice(0, 160))}` : "";
  return `#/news/read?u=${encodeURIComponent(item.url)}${syms ? `&s=${encodeURIComponent(syms)}` : ""}${t}`;
}
export function knownArticle(url) {
  if (articles.has(url)) return articles.get(url);
  try { return JSON.parse(sessionStorage.getItem(`kairo-article:${url}`) || "null"); } catch { return null; }
}

/** Last good API result per key, kept across visits so pages paint instantly and then refresh. */
export const lastSeen = {
  get(key) { return store.get(`kairo-last:${key}`, null); },
  set(key, value) {
    try { const s = JSON.stringify(value); if (s.length < 1_500_000) localStorage.setItem(`kairo-last:${key}`, s); } catch { /* full or unavailable */ }
  },
};

// ---------------------------------------------------------------- formatting
export const CUR = { USD: "$", INR: "₹", EUR: "€", GBP: "£", JPY: "¥" };
export function fmt(v, digits) {
  if (v == null || !isFinite(v)) return "—";
  const d = digits ?? (Math.abs(v) < 10 ? 4 : 2);
  return v.toLocaleString(undefined, { minimumFractionDigits: Math.min(d, 2), maximumFractionDigits: d });
}
export const money = (v, cur, digits) => v == null || !isFinite(v) ? "—" : `${v < 0 ? "−" : ""}${CUR[cur] ?? ""}${fmt(Math.abs(v), digits)}${CUR[cur] ? "" : cur ? " " + cur : ""}`;
export const signedMoney = (v, cur, digits) => v == null ? "—" : `${v >= 0 ? "+" : "−"}${CUR[cur] ?? ""}${fmt(Math.abs(v), digits)}${CUR[cur] || !cur ? "" : " " + cur}`;
export const signed = (v, d = 2) => v == null ? "—" : `${v >= 0 ? "+" : "−"}${fmt(Math.abs(v), d)}`;
export const pctTxt = (v, d = 2) => v == null || !isFinite(v) ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(d)}%`;
export const tone = (v) => v == null ? "" : v >= 0 ? "up" : "down";
export function compact(v) {
  if (v == null) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(Math.round(v * 100) / 100);
}
/** Indian-style big numbers for rupee totals: 12.4L, 3.1Cr. */
export function inrShort(v) {
  if (v == null) return "—";
  const a = Math.abs(v), s = v < 0 ? "−" : "";
  if (a >= 1e7) return `${s}₹${(a / 1e7).toFixed(2)}Cr`;
  if (a >= 1e5) return `${s}₹${(a / 1e5).toFixed(2)}L`;
  return money(v, "INR");
}
export const timeTxt = (t, sec = true) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", ...(sec ? { second: "2-digit" } : {}) });
export const dateTxt = (t) => new Date(t).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
export function ago(t) {
  const s = (Date.now() - new Date(t).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)}d ago`;
  return dateTxt(t);
}
export const tzName = (() => { try { return new Intl.DateTimeFormat([], { timeZoneName: "short" }).formatToParts(new Date()).find((p) => p.type === "timeZoneName").value; } catch { return ""; } })();
export const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const SUFFIX = { NS: "NSE", BO: "BSE", L: "LSE", DE: "XETRA", PA: "PARIS", TO: "TSX", AX: "ASX", HK: "HKEX", T: "TSE" };
/** "RELIANCE.NS" -> ["RELIANCE", "NSE"] the way broker apps show it. */
export function splitSym(sym) {
  const m = /^(.+)\.([A-Z]{1,3})$/.exec(sym || "");
  return m && SUFFIX[m[2]] ? [m[1], SUFFIX[m[2]]] : [sym, ""];
}
const LOGO_TINTS = ["#5b6cf0", "#e08a2e", "#2f9e6e", "#a35bd6", "#d9534f", "#1f9bb0", "#c0703f", "#4f7cc4"];
/**
 * Company logo with the initials underneath: the initials show until the image loads, and stay if
 * there's no logo (the server answers 404). Indices and funds get initials only.
 */
export function logoHtml(sym, name = "", size = 32) {
  if (!sym) return "";
  const fund = isMf(sym), index = sym.startsWith("^") || sym.includes("=");
  const words = String(name || splitSym(sym)[0].replace(/^\^/, "")).replace(/[^A-Za-z0-9& ]+/g, " ").trim().split(/\s+/)
    .filter((w) => /^[A-Za-z0-9]/.test(w) && !/^(the|ltd|limited|inc|corp|corporation|plc|co)$/i.test(w));
  const ini = (words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2)).toUpperCase();
  let h = 0; for (const c of sym) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const img = fund || index ? "" : `<img src="/logo/${encodeURIComponent(sym)}" alt="" loading="lazy" decoding="async" onerror="this.remove()">`;
  return `<span class="logo" style="--s:${size}px;--c:${LOGO_TINTS[h % LOGO_TINTS.length]}" aria-hidden="true"><b>${esc(ini)}</b>${img}</span>`;
}
export const isMf = (sym) => /^MF\d{3,8}$/.test(sym || "");
export const symHtml = (sym) => {
  if (isMf(sym)) return `${esc(sym.slice(2))}<span class="ex-tag">MF</span>`;
  const [b, e] = splitSym(sym); return `${esc(b)}${e ? `<span class="ex-tag">${e}</span>` : ""}`;
};

// ---------------------------------------------------------------- API
export async function api(path, opts = {}) {
  const { timeout = 25000, ...rest } = opts;
  const init = { ...rest, headers: { ...(opts.body ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) } };
  if (opts.body && typeof opts.body !== "string") init.body = JSON.stringify(opts.body);
  // Never wait forever: a request stuck behind the browser's per-site connection limit (or a dead
  // network) fails after `timeout` ms, so the page can show a retry instead of an endless skeleton.
  const ctrl = new AbortController(), timer = setTimeout(() => ctrl.abort(), timeout);
  let r, body = null;
  try {
    r = await fetch(path, { ...init, signal: ctrl.signal });
    if (r.status === 204) return null;
    try { body = await r.json(); } catch { /* no body */ }
  } catch (err) {
    if (err.name === "AbortError") { const e = new Error("The server took too long to answer"); e.status = 0; throw e; }
    throw err;
  } finally { clearTimeout(timer); }
  if (!r.ok) {
    const d = body?.detail;
    const msg = Array.isArray(d) ? d.map((e) => `${e.loc?.slice(1).join(".") || "input"}: ${e.msg}`).join("; ")
      : typeof d === "string" ? d : d?.message || `Request failed (HTTP ${r.status})`;
    const err = new Error(msg); err.status = r.status; err.body = body; throw err;
  }
  return body;
}

// ---------------------------------------------------------------- toasts & modals
export function toast(title, text = "", kind = "") {
  let box = $("#toasts");
  if (!box) { box = document.createElement("div"); box.id = "toasts"; box.className = "toasts"; document.body.appendChild(box); }
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<b>${esc(title)}</b>${text ? `<span class="muted">${esc(text)}</span>` : ""}`;
  box.appendChild(el);
  setTimeout(() => el.remove(), kind === "alert" ? 12000 : 5000);
}

/** Open a modal; `render(close)` returns inner HTML, `wire(root, close)` attaches handlers. */
export function modal(title, bodyHtml, wire) {
  const scrim = document.createElement("div");
  scrim.className = "scrim";
  scrim.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
    <div class="modal-h"><span class="modal-t">${esc(title)}</span><button class="icon-btn" data-close aria-label="Close">✕</button></div>${bodyHtml}</div>`;
  const close = () => { scrim.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) close(); });
  scrim.querySelectorAll("[data-close]").forEach((b) => (b.onclick = close));
  document.addEventListener("keydown", onKey);
  document.body.appendChild(scrim);
  if (wire) wire(scrim.querySelector(".modal"), close);
  scrim.querySelector("input, select, textarea, button.primary")?.focus();
  return close;
}

// ---------------------------------------------------------------- routing
const listeners = [];
export function onRoute(fn) { listeners.push(fn); }
export function parseRoute() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean);
  return { view: parts[0] || "markets", sub: parts[1] || "", params: new URLSearchParams(query) };
}
export function navigate(view, sub = "", params = {}) {
  const q = new URLSearchParams(params).toString();
  const hash = `#/${view}${sub ? "/" + sub : ""}${q ? "?" + q : ""}`;
  if (location.hash === hash) listeners.forEach((fn) => fn(parseRoute()));
  else location.hash = hash;
}
window.addEventListener("hashchange", () => listeners.forEach((fn) => fn(parseRoute())));

// ---------------------------------------------------------------- shared app state
export const app = { provider: "yahoo", llm: false, model: "", names: new Map() };

// ---------------------------------------------------------------- stock lists (official, from the server)
let catalogP = null, catalogAt = 0;
/** Universes for the screener / Explore: {default, items: [{key, label, group, count, source}]}. */
export function universeCatalog() {
  if (!catalogP || Date.now() - catalogAt > 60000) {
    catalogAt = Date.now();
    catalogP = api("/screener/universes").catch(() => { catalogP = null; return { default: "", items: [] }; });
  }
  return catalogP;
}
export const validUniverse = (cat, key, extra = []) =>
  cat.items.some((u) => u.key === key) || extra.some(([k]) => k === key) ? key : cat.default || cat.items[0]?.key || "";
/** <option>s grouped by market (India, India sectors, US, ...) plus an optional "Yours" group. */
export function universeOptions(cat, current, extra = []) {
  const groups = new Map();
  for (const u of cat.items) { if (!groups.has(u.group)) groups.set(u.group, []); groups.get(u.group).push(u); }
  const opt = (k, l) => `<option value="${esc(k)}"${k === current ? " selected" : ""}>${esc(l)}</option>`;
  const grp = (g, opts) => `<optgroup label="${esc(g)}">${opts}</optgroup>`;
  return [...groups].map(([g, us]) => grp(g, us.map((u) => opt(u.key, u.count ? `${u.label} (${u.count})` : u.label)).join(""))).join("")
    + (extra.length ? grp("Yours", extra.map(([k, l]) => opt(k, l)).join("")) : "");
}
const stockHits = new Map();
/** Every NSE stock and ETF, every US-listed stock and the main coins, by name or ticker. */
export async function searchStocks(q, limit = 8) {
  const t = q.trim().toLowerCase();
  if (t.length < 2) return [];
  const key = `${t}|${limit}`;
  if (!stockHits.has(key)) {
    stockHits.set(key, api(`/stocks/search?q=${encodeURIComponent(t)}&limit=${limit}`).then((b) => b.items).catch(() => { stockHits.delete(key); return []; }));
  }
  return stockHits.get(key);
}

// ---------------------------------------------------------------- shared live feed
/**
 * One SSE connection (`/watchlist/stream`) for every live price on screen: browsers allow only
 * ~6 connections per site, so the index strip, watchlist and holdings all share this one.
 * Owners declare the symbols they need; the union is (re)subscribed after a short debounce.
 */
// ---------------------------------------------------------------- background tabs
/*
 * Browsers allow ~6 open connections per site, shared by every tab. Live streams hold theirs open, so
 * a few Kairo tabs could use them all and leave new requests waiting forever. A tab that has been in
 * the background for a few seconds therefore closes its price streams and reopens them on return.
 */
const sleepers = new Set();
let asleep = false, sleepTimer = 0;
document.addEventListener("visibilitychange", () => {
  clearTimeout(sleepTimer);
  if (document.hidden) sleepTimer = setTimeout(() => { asleep = true; sleepers.forEach((fn) => fn(false)); }, 5000);
  else if (asleep) { asleep = false; sleepers.forEach((fn) => fn(true)); }
});
/** fn(false) when the tab goes to sleep in the background, fn(true) when it's shown again. */
export const onTabSleep = (fn) => sleepers.add(fn);
export const tabAsleep = () => asleep;

export const feed = (() => {
  const owners = new Map(), last = new Map();
  const handlers = { quote: new Set(), tick: new Set(), failure: new Set(), status: new Set() };
  let es = null, current = "", timer = 0;
  const emit = (type, d) => handlers[type].forEach((fn) => { try { fn(d); } catch (err) { console.error(err); } });
  function restart() {
    if (asleep) return;  // reopened by the wake-up handler below
    const syms = [...new Set([...owners.values()].flat())].sort().slice(0, 60);
    const key = syms.join(",");
    if (key === current) return;
    current = key;
    if (es) { es.close(); es = null; }
    if (!syms.length) { emit("status", { stream: "idle" }); return; }
    es = new EventSource(`/watchlist/stream?symbols=${encodeURIComponent(key)}`);
    for (const type of Object.keys(handlers)) {
      es.addEventListener(type, (e) => {
        const d = JSON.parse(e.data);
        if (type === "quote") last.set(d.symbol, { ...last.get(d.symbol), ...d });
        if (type === "tick") {
          const q = last.get(d.symbol) || { symbol: d.symbol };
          last.set(d.symbol, { ...q, price: d.price, ...(d.change != null ? { change: d.change, change_pct: d.change_percent } : {}) });
        }
        emit(type, d);
      });
    }
    es.onerror = () => emit("status", { stream: "reconnecting" });
  }
  const schedule = () => { clearTimeout(timer); timer = setTimeout(restart, 150); };
  onTabSleep((awake) => {
    if (awake) { restart(); return; }
    if (es) { es.close(); es = null; }
    current = ""; emit("status", { stream: "paused" });
  });
  return {
    require(owner, symbols) { owners.set(owner, [...symbols]); schedule(); },
    release(owner) { owners.delete(owner); schedule(); },
    on(type, fn) { handlers[type].add(fn); return () => handlers[type].delete(fn); },
    last: (sym) => last.get(sym),
  };
})();

/** Tiny inline sparkline (SVG string) for a list of closes, coloured by direction. */
export function sparkline(values, w = 72, h = 24, ref = null) {
  const s = (values || []).filter((v) => v != null);
  if (s.length < 2) return `<svg width="${w}" height="${h}"></svg>`;
  const base = ref ?? s[0];
  const lo = Math.min(...s, base), hi = Math.max(...s, base), r = hi - lo || 1;
  const X = (i) => 1 + (i / (s.length - 1)) * (w - 2), Y = (v) => 2 + (1 - (v - lo) / r) * (h - 4);
  const col = s[s.length - 1] >= base ? css("--up") : css("--down");
  return `<svg width="${w}" height="${h}" aria-hidden="true"><polyline points="${s.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ")}" fill="none" stroke="${col}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
}
