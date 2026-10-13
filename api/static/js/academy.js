// Kai Academy: a structured trading course. Modules of short lessons, each with an illustration where it
// helps, a "try it live" link into the Terminal with Kai coaching, and a quiz. A lesson counts as done when
// its quiz is answered correctly; finishing every lesson unlocks a printable certificate. Progress stays in
// this browser.
import { $, esc, store, navigate, toast } from "./core.js";
import { MODULES, LESSON_COUNT } from "./academy-content.js";
import { kaiOn, setKai } from "./teacher.js";

const A = { prog: store.get("kairo-academy", { done: {}, name: "" }) };
const save = () => store.set("kairo-academy", A.prog);
const doneCount = () => Object.keys(A.prog.done).length;
const allLessons = MODULES.flatMap((m) => m.lessons.map((l) => ({ ...l, module: m })));
const AVATAR = `<svg viewBox="0 0 40 40" width="38" height="38" aria-hidden="true"><circle cx="20" cy="22" r="14" fill="var(--brand)"/>
  <circle cx="15" cy="21" r="2" fill="#fff"/><circle cx="25" cy="21" r="2" fill="#fff"/><path d="M14.5 27c3 2.6 8 2.6 11 0" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round"/>
  <path d="M6 9.5 20 4l14 5.5-14 5.5z" fill="var(--text)"/><path d="M31 11v6" stroke="var(--text)" stroke-width="1.6"/><circle cx="31" cy="18" r="1.6" fill="var(--warn)"/></svg>`;

export function initAcademy() {
  $("#learnBody").addEventListener("click", onClick);
}

export function showAcademy(sub) {
  document.title = "Kai Academy · Kairo Markets";
  if (sub === "certificate") renderCertificate();
  else if (sub && allLessons.some((l) => l.id === sub)) renderLesson(sub);
  else renderHome();
  window.scrollTo?.(0, 0);
}

// ------------------------------------------------------------------ home
function renderHome() {
  const n = doneCount(), pct = Math.round((n / LESSON_COUNT) * 100);
  const next = allLessons.find((l) => !A.prog.done[l.id]);
  $("#learnBody").innerHTML = `<div class="page-h"><div><h1 class="page-title">Kai Academy</h1>
      <div class="page-sub">Learn to read charts and trade with discipline, in ${LESSON_COUNT} short lessons. Each one ends with a quiz and a live exercise with Kai.</div></div></div>
    <div class="card ac-hero">${AVATAR}<div class="ac-hero-t">
        <b>${n === 0 ? "Start with the basics: how the Indian market works." : n === LESSON_COUNT ? "Course complete. Well done!" : `${n} of ${LESSON_COUNT} lessons done.`}</b>
        <div class="ac-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><span style="width:${pct}%"></span></div>
        <small class="muted">${pct}% complete · progress is saved in this browser</small></div>
      ${n === LESSON_COUNT ? `<a class="btn primary" href="#/learn/certificate">Your certificate</a>` : next ? `<a class="btn primary" href="#/learn/${next.id}">${n ? "Continue" : "Start"}: ${esc(next.title)}</a>` : ""}</div>
    <div class="ac-grid">${MODULES.map((m, i) => {
      const done = m.lessons.filter((l) => A.prog.done[l.id]).length;
      return `<div class="card ac-mod"><div class="ac-mod-h"><span class="ac-num">${i + 1}</span><div><b>${esc(m.title)}</b><small class="muted">${esc(m.blurb)}</small></div>
          ${done === m.lessons.length ? `<span class="chip good">Done</span>` : `<span class="muted ac-count">${done}/${m.lessons.length}</span>`}</div>
        <ol class="ac-list">${m.lessons.map((l) => `<li class="${A.prog.done[l.id] ? "done" : ""}"><a href="#/learn/${l.id}">${A.prog.done[l.id] ? "✓ " : ""}${esc(l.title)}</a></li>`).join("")}</ol></div>`;
    }).join("")}</div>
    <p class="muted ac-fine">Education, not investment advice. Practise in paper mode before risking real money.</p>`;
}

// ------------------------------------------------------------------ lesson
function candlesSvg(art) {
  const lo = Math.min(...art.map((c) => c[2])), hi = Math.max(...art.map((c) => c[1])), H = 150, W = Math.max(160, art.length * 46 + 20), pad = 10;
  const y = (v) => pad + ((hi - v) / (hi - lo || 1)) * (H - pad * 2);
  return `<svg class="ac-art" viewBox="0 0 ${W} ${H}" role="img" aria-label="Candlestick illustration">${art.map(([o, h, l, c], i) => {
    const x = 20 + i * 46, up = c >= o, top = y(Math.max(o, c)), bot = y(Math.min(o, c));
    return `<line x1="${x + 11}" x2="${x + 11}" y1="${y(h)}" y2="${y(l)}" class="${up ? "ac-up" : "ac-dn"}" stroke-width="2"/>
      <rect x="${x}" y="${top}" width="22" height="${Math.max(2, bot - top)}" rx="2" class="${up ? "ac-upf" : "ac-dnf"}"/>`;
  }).join("")}</svg>`;
}
function renderLesson(id) {
  const i = allLessons.findIndex((l) => l.id === id), l = allLessons[i], prev = allLessons[i - 1], next = allLessons[i + 1];
  const t = l.try;
  $("#learnBody").innerHTML = `<div class="ac-crumb"><a href="#/learn">Kai Academy</a> › ${esc(l.module.title)}</div>
    <h1 class="page-title ac-title">${esc(l.title)}${A.prog.done[l.id] ? ` <span class="chip good">Done</span>` : ""}</h1>
    <div class="card ac-lesson">${l.body.map((p) => `<p>${esc(p)}</p>`).join("")}${l.art ? candlesSvg(l.art) : ""}
      ${t ? `<div class="ac-try">${AVATAR}<div><b>Try it live</b><small class="muted">${t.page ? "Opens the page in Kairo." : "Opens the Terminal with Kai switched on to coach you."}</small></div>
        <button class="btn" data-try="${esc(l.id)}">${esc(t.label)}</button></div>` : ""}</div>
    <div class="card ac-quiz"><div class="card-t">Quiz</div>${l.quiz.map((q, qi) => `<fieldset class="ac-q" data-q="${qi}"><legend>${qi + 1}. ${esc(q.q)}</legend>
        ${q.a.map((a, ai) => `<label><input type="radio" name="q${qi}" value="${ai}"> ${esc(a)}</label>`).join("")}<div class="ac-why" hidden></div></fieldset>`).join("")}
      <button class="btn primary" data-check="${esc(l.id)}">Check answers</button><span class="ac-result" id="acResult"></span></div>
    <div class="ac-nav">${prev ? `<a class="btn" href="#/learn/${prev.id}">← ${esc(prev.title)}</a>` : `<span></span>`}
      ${next ? `<a class="btn" href="#/learn/${next.id}">${esc(next.title)} →</a>` : `<a class="btn primary" href="#/learn/certificate">Certificate →</a>`}</div>`;
}
function check(id) {
  const l = allLessons.find((x) => x.id === id);
  let right = 0, answered = 0;
  l.quiz.forEach((q, qi) => {
    const box = document.querySelector(`.ac-q[data-q="${qi}"]`), pick = box.querySelector("input:checked"), why = box.querySelector(".ac-why");
    if (!pick) { why.hidden = true; return; }
    answered++;
    const ok = Number(pick.value) === q.ok;
    if (ok) right++;
    box.className = `ac-q ${ok ? "ok" : "bad"}`;
    why.hidden = false;
    why.innerHTML = `<b>${ok ? "Right." : `Not quite. Answer: ${esc(q.a[q.ok])}.`}</b> ${esc(q.why)}`;
  });
  const res = $("#acResult");
  if (answered < l.quiz.length) { res.textContent = "Answer every question first."; return; }
  if (right === l.quiz.length) {
    const first = !A.prog.done[id];
    A.prog.done[id] = new Date().toISOString(); save();
    res.innerHTML = `<b class="up">All correct!</b> ${first ? "Lesson complete." : ""}`;
    if (first && doneCount() === LESSON_COUNT) toast("Course complete!", "Your Kai Academy certificate is ready.", "ok");
  } else res.innerHTML = `${right} of ${l.quiz.length} right. Read the explanations and try again.`;
}

function tryIt(id) {
  const t = allLessons.find((x) => x.id === id)?.try; if (!t) return;
  if (t.page) { const [v, sub] = t.page.split("/"); navigate(v, sub || ""); return; }
  if (!kaiOn()) setKai(true);
  const params = { symbol: t.symbol };
  if (t.interval) params.interval = t.interval;
  if (t.ind) params.ind = t.ind;
  if (t.chain) params.chain = "1";
  if (t.replay) params.replay = "1";
  navigate("terminal", "", params);
}

// ------------------------------------------------------------------ certificate
function renderCertificate() {
  const n = doneCount();
  if (n < LESSON_COUNT) {
    $("#learnBody").innerHTML = `<div class="ac-crumb"><a href="#/learn">Kai Academy</a> › Certificate</div>
      <div class="card empty-state">${AVATAR}<h3>Almost there</h3><p>Finish all ${LESSON_COUNT} lessons (${LESSON_COUNT - n} to go) to unlock your certificate.</p>
      <div class="actions"><a class="btn primary" href="#/learn">Back to the course</a></div></div>`;
    return;
  }
  const last = Object.values(A.prog.done).sort().pop(), when = new Date(last).toLocaleDateString([], { day: "numeric", month: "long", year: "numeric" });
  $("#learnBody").innerHTML = `<div class="ac-crumb no-print"><a href="#/learn">Kai Academy</a> › Certificate</div>
    <div class="field no-print ac-name"><label>Name on the certificate</label><input class="input" id="acName" value="${esc(A.prog.name)}" placeholder="Your name"></div>
    <div class="ac-cert">${AVATAR}<div class="ac-cert-k">Kai Academy · Kairo Markets</div><div class="ac-cert-t">Certificate of completion</div>
      <div class="ac-cert-n" id="acCertName">${esc(A.prog.name || "Your name")}</div>
      <p>has completed all ${LESSON_COUNT} lessons and quizzes across ${MODULES.length} modules: market basics, chart reading, key levels, patterns, indicators, risk and psychology, futures and options, and India's market data.</p>
      <div class="ac-cert-d">${when}</div></div>
    <div class="actions no-print" style="margin-top:14px"><button class="btn primary" data-print>Print or save as PDF</button></div>`;
  $("#acName").addEventListener("input", (e) => { A.prog.name = e.target.value.slice(0, 60); save(); $("#acCertName").textContent = A.prog.name || "Your name"; });
}

function onClick(e) {
  const c = e.target.closest("[data-check]"); if (c) { check(c.dataset.check); return; }
  const t = e.target.closest("[data-try]"); if (t) { tryIt(t.dataset.try); return; }
  if (e.target.closest("[data-print]")) window.print();
}
