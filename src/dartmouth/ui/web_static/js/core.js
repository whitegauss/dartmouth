"use strict";
/* Dartmouth Web UI. サーバーから受け取る文字列は esc() するか、サーバーで無害化した Markdown の HTML だけを innerHTML に入れる */

const $ = (id) => document.getElementById(id);
const api = async (path, opts = {}) => {
  const r = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts});
  let data = null; try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((data && data.detail && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || r.statusText);
  return data;
};
function toast(text, level = "info") {
  if (!text) return;
  const d = document.createElement("div"); d.className = level; d.textContent = text;
  $("toast").appendChild(d); setTimeout(() => d.remove(), text.includes("\n") ? 10000 : 5000);
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const nf = new Intl.NumberFormat("ja-JP");
/* クリップボードへ。http（Tailscale のアドレスなど）では navigator.clipboard が使えないので、選択してコピーする方法に落とす */
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return; } catch (e) { /* 下へ */ }
  const ta = Object.assign(document.createElement("textarea"), {value: text});
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.appendChild(ta); ta.select();
  const ok = document.execCommand("copy"); ta.remove();
  if (!ok) throw new Error("コピーできませんでした。「.md を保存」を使ってください");
}
/* 担当の表示: 本人か外部待ちだけ。AI がやるもの（参加者の id・空）は担当を付けない（storage/minutes.py の owner_label と同じ） */
function ownerLabel(o) {
  o = String(o || "").trim();
  return ["user", "ユーザー", "本人", "me"].includes(o) ? "本人" : ["external", "外部", "外部待ち"].includes(o) ? "外部待ち" : "";
}
function compact(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  if (n >= 1e4) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "K";
  return nf.format(Math.round(n));
}
const fmtSec = (s) => s >= 3600 ? (s / 3600).toFixed(1) + "時間" : s >= 60 ? Math.round(s / 60) + "分" : Math.round(s) + "秒";
const KIND_LABEL = {decision: "決定事項", question: "未決事項", todo: "ToDo"};

let route = "", routeArg = null, lastState = null;
let pendingStart = null; // 「決定・未決・ToDo」から会議を始めるときの下書き

/* ================================================================ ルーティング */
/* 画面ごとの JS は、その画面を開いたときに読み込む（最初はホームと会議の分だけ） */
const loaded = {};
function need(name) {
  if (!loaded[name]) {
    loaded[name] = new Promise((ok, ng) => {
      const s = document.createElement("script");
      s.src = (window.__assets && window.__assets[name + ".js"]) || `/static/js/${name}.js`;
      s.onload = ok; s.onerror = () => { delete loaded[name]; ng(new Error(`${name}.js を読み込めません`)); };
      document.head.appendChild(s);
    });
  }
  return loaded[name];
}
function parseRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  const [r, ...rest] = h.split("/");
  route = r || ""; routeArg = rest.length ? decodeURIComponent(rest.join("/")) : null;
}
const VIEWS = ["v-home", "meeting", "mtabs", "v-archive", "v-meeting-view", "v-items", "v-ledger", "v-usage", "v-settings"];
function show(...ids) { VIEWS.forEach(v => $(v).classList.toggle("hidden", !ids.includes(v))); }
async function onRoute() {
  parseRoute();
  document.querySelectorAll("nav a").forEach(a => a.classList.toggle("on", a.dataset.route === route));
  $("inputbar").classList.add("hidden"); $("actionbar").classList.add("hidden"); $("closeBtn").classList.add("hidden");
  if (route !== "") { if (es) { es.close(); es = null; } mode = null; }
  if (route === "") return refresh(true);
  $("statusline").textContent = "";
  if (route === "archive" && routeArg) { show("v-meeting-view"); await need("archive"); return loadArchiveView(routeArg); }
  if (route === "archive") { show("v-archive"); await need("archive"); return loadArchive(); }
  if (route === "items") { show("v-items"); await need("items"); return loadItems(); }
  if (route === "ledger") { show("v-ledger"); await need("ledger"); return loadLedger(routeArg); }
  if (route === "usage") { show("v-usage"); await need("usage"); return loadUsage(); }
  if (route === "settings") { show("v-settings"); await need("settings"); return loadSettings(); }
  location.hash = "#/";
}
window.addEventListener("hashchange", onRoute);

/* ================================================================ 発言の表示（進行中と過去の会議で共通） */
function topicBox(container, topic, title, prefix) {
  const key = prefix + (topic == null ? "pre" : "t" + topic);
  let box = document.getElementById(key);
  if (!box) {
    box = document.createElement("details"); box.className = "topic"; box.id = key; box.open = true;
    box.innerHTML = `<summary>${topic == null ? "会議の開始・自己紹介" : "議題" + topic + (title ? "：" + esc(title) : "")}</summary>`;
    container.querySelectorAll("details.topic").forEach(d => { if (d !== box) d.open = false; }); // 前の議題は畳む（重くしない）
    container.appendChild(box);
  } else if (title && !box.dataset.titled) {
    box.querySelector("summary").textContent = "議題" + topic + "：" + title;
  }
  if (title) box.dataset.titled = "1";
  return box;
}
const ROLE_LABEL = {chair: "議長", engineer: "実装・運用", researcher: "調査", third_opinion: "第三の意見", member: "参加者"};
const hhmm = (ts) => (ts || "").slice(11, 16);
function lastSummaryCard(container, topic) {
  const cards = container.querySelectorAll(`.summary-card[data-topic="${topic}"]`);
  return cards.length ? cards[cards.length - 1] : null;
}
function setCardStatus(card, text, cls) {
  if (!card) return;
  const pill = card.querySelector(".card-status"); pill.textContent = text; pill.className = "pill card-status " + (cls || "");
}
function renderMsg(container, m, colors, prefix) {
  const box = topicBox(container, m.topic, m.event === "topic_start" ? m.title : null, prefix);
  let el;
  if (m.kind === "divider") {
    el = document.createElement("div"); el.className = "divider"; el.innerHTML = `<span>${m.html}</span>`;
  } else if (m.kind === "summary" || m.kind === "final") {
    el = document.createElement("div"); el.className = "summary-card" + (m.kind === "final" ? " final" : "");
    if (m.topic != null) el.dataset.topic = m.topic;
    const title = m.kind === "final" ? "会議の総括" : `議長のまとめ · 議題${m.topic}`;
    el.innerHTML = `<div class="sc-head"><span class="avatar" style="background:${esc(colors[m.speaker] || "var(--s1)")}">${esc((m.name || "?").slice(0, 1))}</span>
      <b>${esc(title)}</b><span class="muted">${esc(m.name)} · ${esc(hhmm(m.ts))}</span>
      <span class="pill card-status">${m.kind === "final" ? "" : "承認待ち"}</span></div><div class="sc-body">${m.html}</div>`;
    if (m.kind === "final") el.querySelector(".card-status").remove();
  } else if (m.kind === "message") {
    const isUser = m.speaker === "user";
    el = document.createElement("div"); el.className = "msg" + (isUser ? " user" : "");
    const c = !isUser ? colors[m.speaker] : null;
    const meta = [m.role && ROLE_LABEL[m.role] ? ROLE_LABEL[m.role] : "", m.round_label, m.elapsed_s ? m.elapsed_s + "秒" : "", hhmm(m.ts)].filter(Boolean).join(" · ");
    el.innerHTML = (isUser ? "" : `<span class="avatar" style="background:${esc(c || "var(--muted)")}">${esc((m.name || "?").slice(0, 1))}</span>`)
      + `<div class="bubble-wrap"><div class="who">${esc(m.name)}${m.to ? " → " + esc(m.to) : ""} <span class="meta">${esc(meta)}</span></div><div class="bubble">${m.html}</div></div>`;
    if (m.event === "rejected") setCardStatus(lastSummaryCard(container, m.topic), "却下", "bad");
  } else {
    if (m.event === "approved") { setCardStatus(lastSummaryCard(container, m.topic), m.auto ? "自動承認" : m.edited ? "承認済み（修正あり）" : "承認済み", "ok"); }
    el = document.createElement("div"); el.className = "sys" + (m.status === "failed" || m.status === "timeout" ? " bad" : "");
    el.innerHTML = (m.kind === "error" ? "⚠ " + esc(m.name) + "（" + esc(m.status) + "）: " : "") + m.html.replace(/^<p>|<\/p>\s*$/g, "");
  }
  el.dataset.text = (m.name || "") + " " + el.textContent;
  if (m.seq != null) el.id = prefix + "seq-" + m.seq;
  box.appendChild(el);
  return el;
}


/* 使用量の表（過去の会議の画面でも使う） */
function usageTableHTML(rows, perMeeting) {
  const head = `<tr><th>参加者</th><th>モデル</th>${perMeeting ? "" : "<th>会議</th>"}<th>呼び出し</th><th>失敗</th><th>平均</th><th>合計時間</th><th>トークン</th><th>文字数</th><th>費用</th></tr>`;
  return `<table class="list">${head}${rows.map(r => `<tr><td>${esc(r.display_name)}</td><td>${esc(r.model || r.adapter || "")}</td>${perMeeting ? "" : `<td class="num">${r.meetings}</td>`}
    <td class="num">${nf.format(r.calls)}</td><td class="num">${r.timeout + r.failed}</td><td class="num">${r.avg_s}秒</td><td class="num">${fmtSec(r.elapsed_s)}</td>
    <td class="num">${nf.format(r.tokens)}</td><td class="num">${nf.format(r.chars)}</td><td class="num">${r.cost_usd ? "$" + r.cost_usd.toFixed(2) : "—"}</td></tr>`).join("")}</table>`;
}

/* 設定の画面に書きかけがあるか（再読み込みを止める判定に使う） */
let setFile = null, setDirty = false;
