// Accounts in the browser: the sign-up / log-in gate, the 4-digit app PIN (set up, unlock pad, auto-lock),
// onboarding questions, and the account menu. The server enforces everything; this is the face of it.
import { $, esc, api, toast, modal, store } from "./core.js";

const A = { me: null, gate: null, resolve: null, lastInput: Date.now(), hiddenAt: 0 };
const IDLE_LOCK_MS = 5 * 60_000;      // lock after 5 minutes without touching the app
const BACKGROUND_LOCK_MS = 60_000;    // ...or after a minute in another tab / app, like Groww
const LOGO = `<img src="/static/img/kairo-mark.png" width="44" height="44" alt="" class="au-logo">`;

/** Resolves once someone is signed in and unlocked (immediately when accounts are off). */
export async function ensureSignedIn() {
  let me;
  try { me = await api("/auth/me?app_open=true"); } catch { return null; }
  A.me = me;
  if (me.auth === "off") return null;
  window.addEventListener("kairo:auth", onAuthError);
  watchActivity();
  if (me.user && !me.locked) { afterSignIn(); return me.user; }
  await new Promise((resolve) => { A.resolve = resolve; me.user ? showPin("unlock") : showWelcome(me.first_account ? "signup" : "login"); });
  return A.me.user;
}
export const currentUser = () => A.me?.user || null;

function finish() {
  closeGate();
  afterSignIn();
  const r = A.resolve; A.resolve = null;
  r?.();
}
function afterSignIn() { renderAccountButton(); }

// ------------------------------------------------------------------ gate shell
function gate(html) {
  if (!A.gate) {
    A.gate = document.createElement("div");
    A.gate.className = "au-gate";
    A.gate.setAttribute("role", "dialog"); A.gate.setAttribute("aria-modal", "true");
    document.body.appendChild(A.gate);
  }
  A.gate.innerHTML = `<div class="au-card">${html}</div>`;
  A.gate.hidden = false;
  document.body.classList.add("au-open");
  A.gate.querySelector("input:not([type=checkbox])")?.focus();
  return A.gate;
}
function closeGate() { if (A.gate) { A.gate.hidden = true; A.gate.innerHTML = ""; } document.body.classList.remove("au-open"); }
const err = (root, msg) => { const e = root.querySelector(".au-err"); if (e) { e.textContent = msg || ""; e.hidden = !msg; } };
async function busy(btn, fn) { btn.disabled = true; try { await fn(); } finally { btn.disabled = false; } }

// ------------------------------------------------------------------ welcome: sign up / log in
function showWelcome(tab = "login") {
  const signup = tab === "signup";
  const root = gate(`${LOGO}<h1 class="au-h">${signup ? "Create your Kairo account" : "Welcome back"}</h1>
    <p class="au-sub">${signup ? "Learn, practise and trade smarter, with Kai, your AI trading coach." : "Log in to continue to Kairo."}</p>
    <div class="seg au-tabs"><button data-tab="login" class="${signup ? "" : "on"}">Log in</button><button data-tab="signup" class="${signup ? "on" : ""}">Sign up</button></div>
    <form class="au-form" novalidate>
      ${signup ? `<label class="au-f"><span>First name</span><input name="name" autocomplete="given-name" maxlength="60" required></label>` : ""}
      <label class="au-f"><span>Email</span><input name="email" type="email" autocomplete="email" inputmode="email" maxlength="254" required></label>
      <label class="au-f"><span>Password</span><span class="au-pw"><input name="password" type="password" autocomplete="${signup ? "new-password" : "current-password"}" maxlength="128" required>
        <button type="button" class="au-eye" data-eye aria-label="Show password">Show</button></span>${signup ? `<small class="muted">At least 8 characters, with letters and a number.</small>` : ""}</label>
      ${signup ? `<label class="au-check"><input type="checkbox" name="age"> I am 18 years or older</label>
        <label class="au-check"><input type="checkbox" name="terms"> I agree to the <a href="#" data-doc="terms">Terms</a> and <a href="#" data-doc="privacy">Privacy Policy</a></label>
        <label class="au-check muted"><input type="checkbox" name="whatsapp"> Send me Kai's alerts and market updates on WhatsApp (optional)</label>` : ""}
      <div class="au-err" hidden></div>
      <button class="btn primary au-go" type="submit">${signup ? "Create account" : "Log in"}</button>
      ${signup ? "" : `<button type="button" class="au-link" data-forgot>Forgot password?</button>`}
    </form>
    <p class="au-fine">Kairo is for learning and analysis. It is not a broker and never asks for your bank, Aadhaar or broker passwords.</p>`);
  root.querySelector(".au-tabs").onclick = (e) => { const b = e.target.closest("[data-tab]"); if (b) showWelcome(b.dataset.tab); };
  root.querySelector("[data-eye]").onclick = (e) => { const i = root.querySelector("[name=password]"); i.type = i.type === "password" ? "text" : "password"; e.target.textContent = i.type === "password" ? "Show" : "Hide"; };
  root.querySelector("[data-forgot]")?.addEventListener("click", () => showForgot(root.querySelector("[name=email]").value));
  root.querySelectorAll("[data-doc]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); showDoc(a.dataset.doc); }));
  const f = root.querySelector("form");
  f.onsubmit = (e) => {
    e.preventDefault(); err(root, "");
    busy(f.querySelector(".au-go"), async () => {
      try {
        if (signup) {
          const r = await api("/auth/signup", { method: "POST", body: { name: f.name.value, email: f.email.value, password: f.password.value,
            age_confirmed: f.age.checked, terms_accepted: f.terms.checked, whatsapp_opt_in: f.whatsapp.checked } });
          A.me = r;
          if (r.adopted_existing_data) toast("Your existing data is now in this account", "Portfolio, paper trades, alerts and journal carried over.", "ok");
          showPin("setup");
        } else {
          A.me = await api("/auth/login", { method: "POST", body: { email: f.email.value, password: f.password.value } });
          A.me.user.onboarded ? finish() : showOnboarding();
        }
      } catch (ex) { err(root, ex.message); }
    });
  };
}

function showForgot(email = "") {
  const root = gate(`${LOGO}<h1 class="au-h">Reset your password</h1><p class="au-sub">We'll email you a 6-digit code.</p>
    <form class="au-form" id="auForgot" novalidate><label class="au-f"><span>Email</span><input name="email" type="email" value="${esc(email)}" autocomplete="email" required></label>
      <div class="au-err" hidden></div><button class="btn primary au-go" type="submit">Send code</button>
      <button type="button" class="au-link" data-back>Back to log in</button></form>`);
  root.querySelector("[data-back]").onclick = () => showWelcome("login");
  const f = root.querySelector("form");
  f.onsubmit = (e) => {
    e.preventDefault(); err(root, "");
    busy(f.querySelector(".au-go"), async () => {
      try { await api("/auth/password/forgot", { method: "POST", body: { email: f.email.value } }); showReset(f.email.value); }
      catch (ex) { err(root, ex.message); }
    });
  };
}
function showReset(email) {
  const root = gate(`${LOGO}<h1 class="au-h">Enter the code</h1><p class="au-sub">If <b>${esc(email)}</b> has a Kairo account, a 6-digit code is on its way. It expires in 15 minutes.</p>
    <form class="au-form" novalidate><label class="au-f"><span>6-digit code</span><input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required></label>
      <label class="au-f"><span>New password</span><input name="password" type="password" autocomplete="new-password" maxlength="128" required><small class="muted">At least 8 characters, with letters and a number.</small></label>
      <div class="au-err" hidden></div><button class="btn primary au-go" type="submit">Set new password</button>
      <button type="button" class="au-link" data-back>Back to log in</button></form>`);
  root.querySelector("[data-back]").onclick = () => showWelcome("login");
  const f = root.querySelector("form");
  f.onsubmit = (e) => {
    e.preventDefault(); err(root, "");
    busy(f.querySelector(".au-go"), async () => {
      try { A.me = await api("/auth/password/reset", { method: "POST", body: { email, code: f.code.value, new_password: f.password.value } }); toast("Password changed", "You're logged in. Other devices were logged out.", "ok"); A.me.user.pin_set ? finish() : showPin("setup"); }
      catch (ex) { err(root, ex.message); }
    });
  };
}

// ------------------------------------------------------------------ PIN: set up and unlock
const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "⌫"];
function pinPad(root, onComplete) {
  let value = "";
  const dots = root.querySelector(".au-dots"), pad = root.querySelector(".au-pad");
  const draw = () => dots.querySelectorAll("i").forEach((d, i) => d.classList.toggle("on", i < value.length));
  const press = (k) => {
    if (k === "⌫") value = value.slice(0, -1);
    else if (/^\d$/.test(k) && value.length < 4) value += k;
    draw();
    if (value.length === 4) { const v = value; setTimeout(() => { value = ""; draw(); onComplete(v); }, 120); }
  };
  pad.innerHTML = KEYS.map((k) => (k ? `<button type="button" data-k="${k}" aria-label="${k === "⌫" ? "Delete" : k}">${k}</button>` : `<span></span>`)).join("");
  pad.onclick = (e) => { const b = e.target.closest("[data-k]"); if (b) press(b.dataset.k); };
  const onKey = (e) => { if (!A.gate || A.gate.hidden || !root.isConnected) { document.removeEventListener("keydown", onKey); return; }
    if (/^\d$/.test(e.key)) press(e.key); else if (e.key === "Backspace") press("⌫"); };
  document.addEventListener("keydown", onKey);
  draw();
  return { shake() { dots.classList.remove("shake"); void dots.offsetWidth; dots.classList.add("shake"); } };
}
const padHtml = `<div class="au-dots" aria-hidden="true"><i></i><i></i><i></i><i></i></div><div class="au-err" hidden></div><div class="au-pad"></div>`;

function showPin(mode, opts = {}) {
  if (mode === "unlock") {
    const name = A.me?.user?.name?.split(" ")[0] || "";
    const root = gate(`${LOGO}<h1 class="au-h">Welcome back${name ? `, ${esc(name)}` : ""}</h1><p class="au-sub">Enter your 4-digit PIN</p>${padHtml}
      <button type="button" class="au-link" data-forgot-pin>Forgot PIN? Log in with your password</button>`);
    const pad = pinPad(root, async (pin) => {
      err(root, "");
      try {
        A.me = await api("/auth/unlock", { method: "POST", body: { pin } });
        if (opts.onDone) { closeGate(); opts.onDone(); } else finish();
      } catch (ex) {
        pad.shake(); err(root, ex.message);
        if (ex.status === 401 && ex.body?.detail?.code === "PIN_LOCKOUT") setTimeout(() => showWelcome("login"), 1600);
      }
    });
    root.querySelector("[data-forgot-pin]").onclick = async () => { try { await api("/auth/logout", { method: "POST" }); } catch { /* ignore */ } A.me = { ...A.me, user: null }; showWelcome("login"); };
    return;
  }
  // setup (or change): enter twice; changing an existing PIN needs the password first
  let first = null;
  const step = () => {
    const root = gate(`${LOGO}<h1 class="au-h">${first ? "Confirm your PIN" : opts.change ? "Choose a new PIN" : "Set a 4-digit PIN"}</h1>
      <p class="au-sub">${first ? "Enter the same 4 digits again." : "You'll use it to open Kairo quickly and keep your portfolio private, like a banking app."}</p>${padHtml}
      ${opts.change || first ? "" : `<button type="button" class="au-link" data-skip>Skip for now</button>`}`);
    const pad = pinPad(root, async (pin) => {
      err(root, "");
      if (!first) {
        if (/^(\d)\1{3}$/.test(pin) || ["1234", "4321", "0123", "1212", "6969"].includes(pin)) { pad.shake(); err(root, "That PIN is too easy to guess. Avoid repeated or sequential digits."); return; }
        first = pin; step(); return;
      }
      if (pin !== first) { first = null; toast("PINs didn't match", "Let's try again.", "err"); step(); return; }
      try {
        A.me = await api("/auth/pin", { method: "POST", body: { pin, password: opts.password || null } });
        toast("PIN set", "Kairo will ask for it when you open the app and after 5 minutes away.", "ok");
        if (opts.onDone) { closeGate(); opts.onDone(); } else A.me.user.onboarded ? finish() : showOnboarding();
      } catch (ex) { first = null; toast("Couldn't set the PIN", ex.message, "err"); step(); }
    });
    root.querySelector("[data-skip]")?.addEventListener("click", () => (A.me.user.onboarded ? finish() : showOnboarding()));
  };
  step();
}

// ------------------------------------------------------------------ onboarding
const Q = [
  ["experience", "How long have you been investing or trading?", [["new", "I'm new"], ["under_1y", "Under a year"], ["1_3y", "1–3 years"], ["3y_plus", "3+ years"]], false],
  ["interests", "What do you want to learn or trade?", [["stocks", "Stocks"], ["fno", "Futures & options"], ["mutual_funds", "Mutual funds"], ["long_term", "Long-term investing"]], true],
  ["style", "Your style?", [["intraday", "Intraday"], ["swing", "Swing (days–weeks)"], ["long_term", "Long-term"], ["not_sure", "Not sure yet"]], false],
  ["language", "Kai should speak…", [["en", "English"], ["hi", "हिन्दी (Hindi, coming soon)"]], false],
];
function showOnboarding(onDone) {
  const ans = { interests: [], paper_capital: 1000000 };
  const root = gate(`${LOGO}<h1 class="au-h">A few quick questions</h1><p class="au-sub">So Kai can pitch lessons and tips at the right level. You can skip these.</p>
    <div class="au-qs">${Q.map(([k, q, opts, multi]) => `<div class="au-q" data-q="${k}" data-multi="${multi ? 1 : 0}"><b>${esc(q)}</b><div class="chips">${opts.map(([v, l]) => `<button type="button" data-v="${v}">${esc(l)}</button>`).join("")}</div></div>`).join("")}
      <div class="au-q"><b>Practice money for paper trading</b><div class="chips" data-cap>${[[100000, "₹1 lakh"], [1000000, "₹10 lakh"], [2500000, "₹25 lakh"]].map(([v, l]) => `<button type="button" data-c="${v}" class="${v === 1000000 ? "on" : ""}">${l}</button>`).join("")}</div></div></div>
    <div class="au-err" hidden></div>
    <div class="au-row"><button type="button" class="au-link" data-skip>Skip</button><button type="button" class="btn primary" data-save>Start using Kairo</button></div>`);
  root.querySelector(".au-qs").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.c) { ans.paper_capital = Number(b.dataset.c); b.parentElement.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); return; }
    const q = b.closest("[data-q]"), k = q.dataset.q;
    if (q.dataset.multi === "1") { const set = new Set(ans[k]); set.has(b.dataset.v) ? set.delete(b.dataset.v) : set.add(b.dataset.v); ans[k] = [...set]; b.classList.toggle("on"); }
    else { ans[k] = b.dataset.v; q.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); }
  };
  const done = () => { if (onDone) { closeGate(); onDone(); } else finish(); };
  root.querySelector("[data-skip]").onclick = async () => { try { A.me = await api("/auth/onboarding", { method: "POST", body: { skipped: true } }); } catch { /* ignore */ } done(); };
  root.querySelector("[data-save]").onclick = (e) => busy(e.target, async () => {
    try { A.me = await api("/auth/onboarding", { method: "POST", body: ans }); if (ans.experience === "new") store.set("kairo-kai", true); done(); }
    catch (ex) { err(root, ex.message); }
  });
}

// ------------------------------------------------------------------ terms / privacy (plain-language summaries)
function showDoc(which) {
  const docs = {
    terms: ["Terms of use (summary)", ["Kairo is an educational and analysis tool. It is not a stockbroker, investment adviser or research analyst, and nothing in it is investment advice.",
      "Paper trading uses virtual money. If you connect a broker, real orders are placed only when you confirm them, and you are responsible for them.",
      "Market data may be delayed or wrong; check with your broker before trading.", "You must be 18 or older and keep your password and PIN private."]],
    privacy: ["Privacy policy (summary)", ["We collect your email, first name, your 18+ confirmation, your consents, and the answers you choose to give about your experience. That's all we need.",
      "Your portfolio, paper trades, journal, alerts and scans are stored in your own separate data file. We never sell your data.",
      "We never ask for your Aadhaar, bank details or broker password. Broker connections use the broker's own login.",
      "You can download everything we hold about you, or delete your account and all its data, from the account menu at any time.",
      "Questions or complaints: contact the grievance officer listed on our website."]],
  }[which];
  modal(docs[0], `<ul class="au-doc">${docs[1].map((p) => `<li>${esc(p)}</li>`).join("")}</ul><p class="fine">The full legal text will be published before public launch.</p>`);
}

// ------------------------------------------------------------------ mid-session: locked or signed out
function onAuthError(e) {
  const status = e.detail?.status;
  if (A.gate && !A.gate.hidden) return;  // a screen is already up
  if (status === 423) showPin("unlock", { onDone: () => window.dispatchEvent(new CustomEvent("kairo:unlocked")) });
  else if (status === 401) { A.me = { ...A.me, user: null }; A.resolve = () => location.reload(); showWelcome("login"); }
}
async function lockNow() {
  try { await api("/auth/lock", { method: "POST" }); } catch { /* ignore */ }
  showPin("unlock", { onDone: () => window.dispatchEvent(new CustomEvent("kairo:unlocked")) });
}
function watchActivity() {
  for (const ev of ["pointerdown", "keydown", "wheel", "touchstart"]) document.addEventListener(ev, () => (A.lastInput = Date.now()), { passive: true });
  setInterval(() => { if (A.me?.user?.pin_set && (!A.gate || A.gate.hidden) && Date.now() - A.lastInput > IDLE_LOCK_MS) lockNow(); }, 30_000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { A.hiddenAt = Date.now(); return; }
    if (A.me?.user?.pin_set && A.hiddenAt && Date.now() - A.hiddenAt > BACKGROUND_LOCK_MS && (!A.gate || A.gate.hidden)) lockNow();
    A.hiddenAt = 0;
  });
}

// ------------------------------------------------------------------ account menu
function renderAccountButton() {
  const u = A.me?.user; if (!u) return;
  let btn = $("#acctBtn");
  if (!btn) {
    btn = document.createElement("button");
    btn.id = "acctBtn"; btn.className = "acct-btn"; btn.setAttribute("aria-label", "Your account");
    $("#themeBtn").after(btn);
    btn.onclick = (e) => { e.stopPropagation(); toggleMenu(); };
    document.addEventListener("click", (e) => { if (!e.target.closest(".acct-menu, #acctBtn")) $(".acct-menu")?.remove(); });
  }
  btn.textContent = (u.name || u.email).trim()[0].toUpperCase();
  btn.title = `${u.name} · ${u.email}`;
}
function toggleMenu() {
  if ($(".acct-menu")) { $(".acct-menu").remove(); return; }
  const u = A.me.user, m = document.createElement("div");
  m.className = "acct-menu";
  m.innerHTML = `<div class="acct-id"><b>${esc(u.name)}</b><small class="muted">${esc(u.email)}</small></div>
    ${u.pin_set ? `<button data-a="lock">🔒 Lock Kairo now</button><button data-a="pin-change">Change PIN</button><button data-a="pin-remove">Remove PIN</button>`
      : `<button data-a="pin-set">Set a 4-digit PIN</button>`}
    <button data-a="prefs">Learning preferences</button><button data-a="profile">Name &amp; notifications</button><button data-a="password">Change password</button>
    <hr><button data-a="export">Download my data</button><button data-a="delete" class="danger">Delete my account</button>
    <hr><button data-a="logout">Log out</button>`;
  document.body.appendChild(m);
  const r = $("#acctBtn").getBoundingClientRect();
  m.style.top = `${r.bottom + 6}px`; m.style.right = `${Math.max(8, window.innerWidth - r.right)}px`;
  m.onclick = (e) => { const b = e.target.closest("[data-a]"); if (b) { m.remove(); menuAction(b.dataset.a); } };
}
const askPassword = (title, label = "Your password") => new Promise((resolve) => {
  let done = false;
  const close = modal(title, `<form id="auPw"><div class="field"><label>${esc(label)}</label><input class="input" type="password" name="pw" autocomplete="current-password" required></div>
    <div class="modal-f"><button type="button" class="btn ghost" data-close>Cancel</button><button class="btn primary" type="submit">Continue</button></div></form>`,
  (root, closeFn) => { root.querySelector("form").onsubmit = (e) => { e.preventDefault(); done = true; resolve(e.target.pw.value); closeFn(); }; });
  const obs = new MutationObserver(() => { if (!document.querySelector("#auPw")) { obs.disconnect(); if (!done) resolve(null); } });
  obs.observe(document.body, { childList: true, subtree: true });
  void close;
});
async function menuAction(a) {
  const refresh = (r) => { if (r?.user) A.me = r; renderAccountButton(); };
  if (a === "lock") lockNow();
  if (a === "pin-set") showPin("setup", { change: true, onDone: renderAccountButton });
  if (a === "pin-change") { const pw = await askPassword("Change your PIN", "Confirm with your password"); if (pw) showPin("setup", { change: true, password: pw, onDone: renderAccountButton }); }
  if (a === "pin-remove") {
    const pw = await askPassword("Remove your PIN", "Confirm with your password"); if (!pw) return;
    try { refresh(await api("/auth/pin/remove", { method: "POST", body: { password: pw } })); toast("PIN removed", "Kairo won't ask for a PIN on this account.", "ok"); } catch (ex) { toast("Couldn't remove the PIN", ex.message, "err"); }
  }
  if (a === "prefs") showOnboarding(renderAccountButton);
  if (a === "profile") {
    const u = A.me.user;
    modal("Name & notifications", `<form id="auProf"><div class="field"><label>First name</label><input class="input" name="name" value="${esc(u.name)}" maxlength="60"></div>
      <label class="au-check" style="margin-top:10px"><input type="checkbox" name="wa" ${u.whatsapp_opt_in ? "checked" : ""}> Send me Kai's alerts and market updates on WhatsApp</label>
      <div class="modal-f"><button type="button" class="btn ghost" data-close>Cancel</button><button class="btn primary" type="submit">Save</button></div></form>`,
    (root, close) => { root.querySelector("form").onsubmit = async (e) => { e.preventDefault();
      try { refresh(await api("/auth/profile", { method: "PATCH", body: { name: e.target.name.value, whatsapp_opt_in: e.target.wa.checked } })); toast("Saved", "", "ok"); close(); } catch (ex) { toast("Couldn't save", ex.message, "err"); } }; });
  }
  if (a === "password") {
    modal("Change password", `<form id="auChg"><div class="field"><label>Current password</label><input class="input" type="password" name="cur" autocomplete="current-password"></div>
      <div class="field" style="margin-top:10px"><label>New password</label><input class="input" type="password" name="new" autocomplete="new-password"><div class="fine">At least 8 characters, with letters and a number. Other devices will be logged out.</div></div>
      <div class="modal-f"><button type="button" class="btn ghost" data-close>Cancel</button><button class="btn primary" type="submit">Change password</button></div></form>`,
    (root, close) => { root.querySelector("form").onsubmit = async (e) => { e.preventDefault();
      try { await api("/auth/password", { method: "POST", body: { current: e.target.cur.value, new: e.target.new.value } }); toast("Password changed", "Other devices were logged out.", "ok"); close(); } catch (ex) { toast("Couldn't change it", ex.message, "err"); } }; });
  }
  if (a === "export") {
    try {
      const data = await api("/auth/export");
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
      const link = document.createElement("a"); link.href = url; link.download = `kairo-my-data-${new Date().toISOString().slice(0, 10)}.json`; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (ex) { toast("Couldn't export", ex.message, "err"); }
  }
  if (a === "delete") {
    modal("Delete your account", `<p>This permanently deletes your account and <b>everything</b> in it: portfolio, paper trades, journal, alerts, scans and settings. It can't be undone.</p>
      <p class="fine">Tip: download your data first from the account menu.</p>
      <form id="auDel"><div class="field"><label>Your password</label><input class="input" type="password" name="pw" autocomplete="current-password"></div>
      <div class="field" style="margin-top:10px"><label>Type DELETE to confirm</label><input class="input" name="confirm" autocomplete="off"></div>
      <div class="modal-f"><button type="button" class="btn ghost" data-close>Cancel</button><button class="btn danger" type="submit">Delete my account</button></div></form>`,
    (root, close) => { root.querySelector("form").onsubmit = async (e) => { e.preventDefault();
      try { await api("/auth/delete", { method: "POST", body: { password: e.target.pw.value, confirm: e.target.confirm.value } }); close(); toast("Account deleted", "All your data has been removed.", "ok"); setTimeout(() => location.reload(), 1200); }
      catch (ex) { toast("Couldn't delete", ex.message, "err"); } }; });
  }
  if (a === "logout") { try { await api("/auth/logout", { method: "POST" }); } catch { /* ignore */ } location.reload(); }
}
