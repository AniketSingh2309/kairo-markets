// News: merged headlines for holdings and / or watchlist, grouped by day.
import { $, esc, api, ago, splitSym, store, articleHref, knownArticle, feed, logoHtml, money, pctTxt, tone, navigate, onRoute, isMf } from "./core.js";
import { getWatchlist } from "./markets.js";
import { ensurePortfolio } from "./portfolio.js";

const N = { scope: store.get("kairo-news-scope", "holdings"), period: "7d", data: null, loading: false };

function dayLabel(iso) {
  const d = new Date(iso), today = new Date(), y = new Date(); y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" });
}

async function load() {
  const held = await ensurePortfolio();
  const watch = getWatchlist().filter((s) => !s.startsWith("^") && !s.endsWith("=X"));
  const symbols = [...new Set(N.scope === "holdings" ? held : N.scope === "watchlist" ? watch : [...held, ...watch])].slice(0, 40);
  N.symbols = symbols;
  if (!symbols.length) { N.data = { items: [], errors: {} }; render(); return; }
  N.loading = true; render();
  try { N.data = await api(`/news?symbols=${encodeURIComponent(symbols.join(","))}&period=${N.period}`); }
  catch (err) { N.data = { items: [], errors: {}, error: err.message }; }
  N.loading = false; render();
}

function render() {
  [...$("#newsScope").children].forEach((b) => b.classList.toggle("on", b.dataset.s === N.scope));
  [...$("#newsPeriod").children].forEach((b) => b.classList.toggle("on", b.dataset.p === N.period));
  const box = $("#newsBody");
  if (N.loading) { box.innerHTML = `<div class="card">${"<div class='skeleton' style='height:44px;margin:10px 0'></div>".repeat(6)}</div>`; return; }
  const d = N.data; if (!d) return;
  if (d.error) { box.innerHTML = `<div class="card" style="color:var(--down)">${esc(d.error)}</div>`; return; }
  if (!N.symbols?.length) {
    box.innerHTML = `<div class="card empty-state"><h3>Nothing to follow yet</h3><p>${N.scope === "holdings" ? "Add trades to your portfolio" : "Add stocks to your watchlist"} and their headlines will collect here.</p>
      <div class="actions"><a class="btn primary" href="#/${N.scope === "holdings" ? "portfolio" : "markets"}">${N.scope === "holdings" ? "Go to portfolio" : "Go to markets"}</a></div></div>`;
    return;
  }
  if (!d.items.length) { box.innerHTML = `<div class="card empty" style="height:140px">No headlines in the last ${N.period.replace("d", " days")} for ${N.symbols.length} symbols.</div>`; return; }
  let html = "", last = "";
  for (const it of d.items) {
    const lbl = dayLabel(it.published_at);
    if (lbl !== last) { html += `<div class="news-day">${esc(lbl)}</div>`; last = lbl; }
    html += `<a class="news-item${it.thumb ? " has-img" : ""}" href="${esc(articleHref(it))}">
      <div><div class="news-title">${esc(it.title)}</div>
      <div class="news-meta">${it.symbols.map((s) => `<span class="pill">${esc(splitSym(s)[0])}</span>`).join("")}<span>${esc(it.publisher)}</span><span>·</span><span>${esc(ago(it.published_at))}</span>${/\.pdf$/i.test(it.url || "") ? `<span class="pill">PDF</span>` : ""}</div></div>
      ${it.thumb ? `<img class="news-thumb" src="${esc(it.thumb)}" alt="" loading="lazy" decoding="async" onerror="this.remove()">` : ""}</a>`;
  }
  const errs = Object.keys(d.errors || {});
  const covered = new Set(d.items.flatMap((i) => i.symbols));
  const silent = N.symbols.filter((s) => !covered.has(s) && !errs.includes(s)).map((s) => splitSym(s)[0]);
  box.innerHTML = `<div class="card">${html}</div>${errs.length ? `<div class="fine">No news feed for ${errs.map(esc).join(", ")}.</div>` : ""}
    ${silent.length ? `<div class="fine">No headlines from this source in the period for ${silent.map(esc).join(", ")} — Yahoo has little news for Indian stocks; BSE filings are included where the company lists on BSE.</div>` : ""}
    <div class="fine">Headlines from Yahoo Finance and official BSE filings, refreshed every 10 minutes. Covering ${N.symbols.length} symbols.</div>`;
}

export function initNews() {
  $("#newsScope").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; N.scope = b.dataset.s; store.set("kairo-news-scope", N.scope); load(); };
  $("#newsPeriod").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; N.period = b.dataset.p; load(); };
}
export function showNews(sub = "", params = new URLSearchParams()) {
  const reading = sub === "read" && params.get("u");
  $("#view-news .page-h").hidden = !!reading;
  $("#newsBody").hidden = !!reading;
  $("#newsReader").hidden = !reading;
  if (reading) { showReader(params.get("u"), (params.get("s") || "").split(",").filter(Boolean), params.get("t") || ""); return; }
  feed.release("article");
  load();
}

// ------------------------------------------------------------------ in-app article page
const R = { url: "", syms: [], title: "", preview: null, more: null, off: [] };
onRoute((r) => { if (!(r.view === "news" && r.sub === "read")) { feed.release("article"); R.off.forEach((f) => f()); R.off = []; } });

async function showReader(url, syms, title) {
  Object.assign(R, { url, syms, title, preview: null, more: null });
  window.scrollTo(0, 0);
  renderReader();
  const live = syms.filter((s) => !isMf(s) && !s.startsWith("^"));
  feed.require("article", live);
  R.off.forEach((f) => f());
  R.off = [feed.on("quote", renderStocks), feed.on("tick", renderStocks)];
  try { R.preview = await api(`/news/preview?u=${encodeURIComponent(url)}`); } catch (err) { R.preview = { error: err.message }; }
  if (R.url !== url) return;
  renderReader();
  if (syms.length) {
    try { R.more = await api(`/news?symbols=${encodeURIComponent(syms.slice(0, 3).join(","))}&period=7d`); } catch { R.more = { items: [] }; }
    if (R.url === url) renderMore();
  }
}

function renderReader() {
  const box = $("#newsReader"), it = knownArticle(R.url) || {}, p = R.preview || {};
  const filing = p.kind === "filing" || /bseindia\.com/.test(R.url);
  const title = it.title || p.title || R.title || (R.preview && filing ? "Company filing" : "");
  const publisher = it.publisher || p.site || (filing ? "BSE filing" : "");
  const when = it.published_at || p.published;
  const image = !filing && (it.image || p.image);  // the feed's own picture first
  const summary = filing ? it.summary : p.description || it.summary;
  const loading = !R.preview;
  box.innerHTML = `<a class="btn sm ghost reader-back" href="#/news">← All news</a>
    <article class="card reader">
      <div class="reader-meta">${esc(publisher)}${when ? ` · ${esc(ago(when))}` : ""}</div>
      ${title ? `<h1 class="reader-title">${esc(title)}</h1>` : `<div class="skeleton" style="height:28px;width:80%;margin:6px 0 14px"></div>`}
      ${image ? `<img class="reader-cover" src="${esc(image)}" alt="" onerror="this.remove()">` : ""}
      ${summary ? `<p class="reader-desc">${esc(summary)}</p>` : loading ? `<div class="skeleton" style="height:14px;margin:8px 0"></div><div class="skeleton" style="height:14px;width:70%"></div>` : ""}
      ${filing ? `<div class="reader-pdf-wrap"><iframe class="reader-pdf" src="/news/filing?u=${encodeURIComponent(R.url)}" title="Filing PDF"></iframe></div>
        <div class="reader-actions"><a class="btn" href="/news/filing?u=${encodeURIComponent(R.url)}" target="_blank" rel="noopener">Open PDF full screen</a>
        <span class="fine">Official filing from BSE, shown here as filed.</span></div>`
      : R.preview?.error ? `<div class="reader-note">The publisher's page couldn't be loaded right now.</div>
        <div class="reader-actions"><a class="btn primary" href="${esc(R.url)}" target="_blank" rel="noopener noreferrer">Read on ${esc(publisher || "the publisher's site")} ↗</a></div>`
      : loading ? "" : `<div class="reader-note">This is the publisher's summary. The full article belongs to ${esc(publisher || "the publisher")} and is read on their site.</div>
        <div class="reader-actions"><a class="btn primary" href="${esc(R.url)}" target="_blank" rel="noopener noreferrer">Read the full story on ${esc(publisher || "the publisher's site")} ↗</a></div>`}
    </article>
    <div class="card reader-stocks" id="readerStocks" ${R.syms.length ? "" : "hidden"}></div>
    <div class="card" id="readerMore" hidden></div>`;
  renderStocks();
  renderMore();
}

function renderStocks() {
  const box = $("#readerStocks");
  if (!box || !R.syms.length || $("#newsReader").hidden) return;
  box.innerHTML = `<div class="card-h"><span class="card-t">In this story</span></div>` + R.syms.map((s) => {
    const q = feed.last(s) || {};
    return `<a class="reader-stock" href="#/markets?symbol=${encodeURIComponent(s)}">${logoHtml(s, q.name, 34)}
      <div class="rs-id"><b>${esc(splitSym(s)[0])}</b><span class="muted">${esc(q.name || splitSym(s)[1] || "")}</span></div>
      <div class="rs-px num">${q.price != null ? money(q.price, q.currency) : "—"}<span class="${tone(q.change_pct)}">${q.change_pct != null ? pctTxt(q.change_pct) : ""}</span></div></a>`;
  }).join("");
}

function renderMore() {
  const box = $("#readerMore");
  const items = (R.more?.items || []).filter((i) => i.url !== R.url).slice(0, 6);
  if (!box) return;
  box.hidden = !items.length;
  if (!items.length) return;
  box.innerHTML = `<div class="card-h"><span class="card-t">More on ${esc(R.syms.slice(0, 3).map((s) => splitSym(s)[0]).join(", "))}</span></div>` +
    items.map((i) => `<a class="news-item${i.thumb ? " has-img" : ""}" href="${esc(articleHref(i))}"><div><div class="news-title">${esc(i.title)}</div>
      <div class="news-meta"><span>${esc(i.publisher)}</span><span>·</span><span>${esc(ago(i.published_at))}</span></div></div>
      ${i.thumb ? `<img class="news-thumb" src="${esc(i.thumb)}" alt="" loading="lazy" onerror="this.remove()">` : ""}</a>`).join("");
}
