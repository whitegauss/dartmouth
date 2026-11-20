"use strict";
/* ================================================================ 会議: 進行中 */
let replaying = true;
function addLive(m) {
  if (m.seq != null) { if (seen.has(m.seq)) return; seen.add(m.seq); }
  const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 120;
  const el = renderMsg($("log"), m, colors, "lv-");
  if (m.kind === "summary" || m.kind === "final") {
    // まとめが届いたら、相互批判の下にあるカードの冒頭まで自動で移動する（過去分の読み込み中は除く）
    if (!replaying) scrollToCard(el);
  } else if (nearBottom) {
    window.scrollTo(0, document.body.scrollHeight);
  }
}
function scrollToCard(card) {
  if (!card) return;
  const d = card.closest("details"); if (d) d.open = true;
  card.scrollIntoView({behavior: "smooth", block: "start"});
}
function connect() {
  if (es) es.close();
  es = new EventSource("/api/events");
  replaying = true;
  es.addEventListener("msg", (e) => addLive(JSON.parse(e.data)));
  es.addEventListener("ready", async () => {
    replaying = false;
    // 開いたとき（または読み込み中に「まとめを見る」が押されていたら）、最新のまとめカードを見せる
    if (lastState && lastState.waiting === "approval") {
      await showApproval();
      scrollToCard(lastSummaryCard($("log"), lastState.topic_no));
    }
    pendingJump = false;
  });
  es.addEventListener("minutes", (e) => { $("minutes").innerHTML = JSON.parse(e.data).html; $("minutes").classList.remove("muted"); });
  es.addEventListener("approval", () => setTimeout(showApproval, 50));
  es.addEventListener("notice", (e) => { const d = JSON.parse(e.data); toast(d.text, d.level); });
  es.addEventListener("paused", (e) => { const d = JSON.parse(e.data); toast(`${d.participant} が応答しないため一時停止しました`, "warning"); });
  es.addEventListener("end", (e) => {
    const d = JSON.parse(e.data); $("ended").classList.remove("hidden");
    $("endedText").textContent = d.reason === "done" ? "議事録と要件定義書の下書きを書き出しました。" : "会議は中断されました（" + d.reason + "）。再開できます。";
    if (d.reason === "done" && !st.readonly && st.meeting) setTimeout(() => extendForm($("endedExtend"), st.meeting.id), 300);
  });
}
let pendingJump = false;
async function showApproval() {
  // 承認のボタンは、スレッドの中の最新のまとめカードに付ける
  if (document.getElementById("approvalActions") || replaying) return;
  const {proposal, topic} = await api("/api/proposal");
  if (!proposal || document.getElementById("approvalActions")) return;
  let card = lastSummaryCard($("log"), topic);
  if (!card) {
    // まとめの記録がない（議長を飛ばした等）ときも、承認待ちのまとめをスレッドの最後にカードで出す
    card = renderMsg($("log"), {kind: "summary", topic, name: "議長", speaker: "", html: proposal.html, ts: ""}, colors, "lv-");
  }
  const bar = document.createElement("div"); bar.id = "approvalActions"; bar.className = "btns";
  bar.innerHTML = `<button class="primary" data-act="approve">承認</button><button data-act="edit">修正して承認</button><button class="danger" data-act="reject">却下</button>`;
  bar.querySelector('[data-act="approve"]').onclick = () => send("/approve");
  bar.querySelector('[data-act="edit"]').onclick = openEditor;
  bar.querySelector('[data-act="reject"]').onclick = () => { const r = prompt("却下する理由（参加者に伝わります）"); if (r && r.trim()) send("/reject " + r.trim()); };
  card.appendChild(bar);
  card.classList.add("awaiting");
}
function clearApproval() {
  const bar = document.getElementById("approvalActions"); if (bar) { bar.closest(".summary-card")?.classList.remove("awaiting"); bar.remove(); }
  $("editor").classList.add("hidden");
}
function renderPaused(st) {
  let card = document.getElementById("pausedInline");
  if (st.waiting !== "pause" || !st.paused) { if (card) card.remove(); return; }
  if (card) return;
  card = document.createElement("div"); card.id = "pausedInline"; card.className = "summary-card paused";
  card.innerHTML = `<div class="sc-head"><b>一時停止</b><span class="muted">${esc(st.participants[st.paused.participant] || st.paused.participant)} が応答しません</span></div>
    <div class="sc-body"><p>${esc(st.paused.reason)}</p></div>
    <div class="btns"><button data-c="/retry">再試行</button><button data-c="/continue">飛ばして続行</button><button class="danger" data-c="/abort">中断</button></div>`;
  card.querySelectorAll("[data-c]").forEach(b => b.onclick = () => send(b.dataset.c));
  $("log").appendChild(card);
  card.scrollIntoView({block: "nearest"});
}
function statusLines(st) {
  const lines = [];
  lines.push(st.topic_no ? `議題 ${st.topic_no}/${st.topics_total}「${st.topic_title}」 ${st.phase_label}` : st.phase_label);
  for (const [n, f] of Object.entries(st.inflight)) lines.push(`${st.participants[n] || n} 応答中… ${Math.round(f.elapsed_s)}秒`);
  if (st.absent.length) lines.push("欠席: " + st.absent.map(n => st.participants[n] || n).join(", "));
  if (st.waiting === "approval") lines.push("承認待ち");
  if (st.waiting === "pause") lines.push("一時停止中");
  if (st.waiting === "topic") lines.push("/topic で議題を追加してください");
  if (st.waiting === "next") lines.push("「次へ」で次のラウンドへ");
  lines.push(`予算 残り ${st.calls_left}回`);
  return lines;
}
// 構造（秒数以外）が変わったときだけ HTML を作り直し、それ以外は秒数の文字だけ書き換える（毎秒のちらつきを防ぐ）
function patchHTML(el, html, secs) {
  const key = html.replace(/<span class="secs"[^>]*>\d+<\/span>/g, "");
  if (el.dataset.key !== key) { el.innerHTML = html; el.dataset.key = key; return; }
  el.querySelectorAll(".secs").forEach(x => { const v = String(secs[x.dataset.n] ?? ""); if (x.textContent !== v) x.textContent = v; });
}
const PHASES = [["plan", "席決め"], ["independent", "独立回答"], ["critique", "相互批判"], ["summary", "まとめ"], ["approval", "承認"]];
function renderSide(st) {
  // 進行
  patchHTML($("topicLine"), st.topic_no
    ? `<small>議題 ${st.topic_no} / ${st.topics_total}</small>${esc(st.topic_title)}`
    : `<small>${esc(st.phase_label)}</small>${st.phase === "done" ? "会議は終わりました" : "議題を待っています"}`, {});
  const order = PHASES.map(p => p[0]);
  const curIdx = order.indexOf(st.phase === "topic" ? "plan" : st.phase);
  patchHTML($("steps"), st.topic_no ? PHASES.map(([k, label], i) => {
    const cls = i < curIdx ? "done" : i === curIdx ? "now" : "";
    const extra = k === "critique" && st.critique_rounds ? ` ${Math.min(st.critique_done + (i === curIdx ? 1 : 0), st.critique_rounds)}/${st.critique_rounds}` : "";
    return `<div class="step ${cls}"><div class="bar"></div>${label}${extra}</div>`;
  }).join("") : "", {});
  // 席
  const ids = (st.members && st.members.length) ? st.members : Object.keys(st.participants);
  const secs = Object.fromEntries(Object.entries(st.inflight).map(([n, f]) => [n, Math.round(f.elapsed_s)]));
  patchHTML($("people"), ids.map(n => {
    const name = st.participants[n] || n, c = (st.colors || {})[n] || "var(--muted)";
    const speaking = st.inflight[n], absent = st.absent.includes(n);
    const role = ROLE_LABEL[(st.roles || {})[n]] || "";
    const model = (st.models || {})[n] || "";
    const state = speaking ? `<span class="pill live">考え中 <span class="secs" data-n="${esc(n)}">${secs[n]}</span>秒</span>` : absent ? `<span class="pill">欠席</span>` : "";
    return `<div class="person ${speaking ? "speaking" : ""} ${absent ? "absent" : ""}"><span class="avatar" style="background:${esc(c)}">${esc(name.slice(0, 1))}</span>
      <div><div class="nm">${esc(name)}</div><div class="md">${esc([role, model].filter(Boolean).join(" · "))}</div></div><span class="st">${state}</span></div>`;
  }).join(""), secs);
  // 予算
  const max = st.max_calls || (st.calls_left || 1), left = st.calls_left, used = max - left, pct = Math.max(0, Math.min(100, used / max * 100));
  patchHTML($("budget"), `<div class="meter ${left / max < 0.2 ? "low" : ""}"><i style="width:${pct}%"></i></div>
    <div class="kv"><span>呼び出し ${used} / ${max}</span><span>残り ${left}</span></div>
    ${st.approval_mode === "auto" ? '<div class="kv" style="margin-top:6px"><span>承認</span><span class="pill ok">自動</span></div>' : ""}`, {});
  // 考え中（チャットの末尾）
  patchHTML($("typing"), Object.entries(st.inflight).map(([n, f]) => {
    const name = st.participants[n] || n, c = (st.colors || {})[n] || "var(--muted)";
    return `<div class="typing-row"><span class="avatar" style="background:${esc(c)}">${esc(name.slice(0, 1))}</span>
      <span class="dots"><i></i><i></i><i></i></span><span>${esc(name)} が${f.round === "summary" ? "まとめています" : f.round === "plan" ? "席を決めています" : "考えています"}… <span class="muted"><span class="secs" data-n="${esc(n)}">${secs[n]}</span>秒</span></span></div>`;
  }).join(""), secs);
}
function renderStatus(st) {
  const lines = statusLines(st);
  $("status").textContent = lines.join("\n");
  renderSide(st);
  $("statusline").textContent = lines.slice(0, 2).join(" ｜ ");
  renderPaused(st);
  if (st.waiting === "approval" && $("editor").classList.contains("hidden")) showApproval();
  if (st.waiting !== "approval") clearApproval();
  // 承認待ちのあいだは、画面下に承認のボタンを固定で出す（スクロールしても押せる）
  const awaiting = st.waiting === "approval" && st.meeting.running && !st.readonly;
  $("actionbar").classList.toggle("hidden", !awaiting);
  if (awaiting) $("actionLabel").textContent = `議長のまとめ（議題${st.topic_no}「${st.topic_title}」）が承認待ちです`;
  $("inputbar").classList.toggle("hidden", !st.meeting.running || !!st.readonly);
  $("watchNote").classList.toggle("hidden", !st.readonly);
  if (st.readonly) $("watchNote").textContent = st.meeting.running ? "別のプロセス（dm run など）で進行中の会議を表示しています。ここからは操作できません" : "別のプロセスで進んでいた会議です（終わりました）。「閉じる」でホームに戻ります";
  if (!st.meeting.running) {
    $("ended").classList.remove("hidden");
    if (!st.readonly && $("endedExtend").dataset.for !== st.meeting.id) { $("endedExtend").dataset.for = st.meeting.id; extendForm($("endedExtend"), st.meeting.id); }
  }
}
async function send(line) {
  if (!line.trim()) return;
  try {
    const r = await api("/api/command", {method: "POST", body: JSON.stringify({line})});
    if (/^\/(approve|a|reject|r)(\s|$)/.test(line.trim())) { clearApproval(); $("actionbar").classList.add("hidden"); }
    if (r.action === "edit") return openEditor();
    if (r.action === "home") { mode = null; return refresh(true); }
    toast(r.message, r.level === "warning" ? "warning" : "info");
  } catch (e) { toast(e.message, "error"); }
}
$("abApprove").onclick = () => send("/approve");
$("abEdit").onclick = () => openEditor();
$("abReject").onclick = () => { const r = prompt("却下する理由（参加者に伝わります）"); if (r && r.trim()) send("/reject " + r.trim()); };
$("abJump").onclick = async () => {
  if (replaying) { pendingJump = true; toast("会議ログを読み込んでいます。終わったらまとめに移動します"); return; }
  await showApproval();
  const b = document.getElementById("approvalActions");
  const card = b ? b.closest(".summary-card") : lastSummaryCard($("log"), lastState && lastState.topic_no);
  if (card) scrollToCard(card);
};
$("send").onclick = () => { const v = $("line").value; $("line").value = ""; send(v); };
$("line").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); $("send").click(); } });
document.querySelectorAll("[data-cmd]").forEach(b => b.onclick = () => send(b.dataset.cmd));
let draftTimer = null;
async function openEditor() {
  try {
    const {text} = await api("/api/draft");
    // 修正欄はまとめカードのすぐ下に開く
    const bar = document.getElementById("approvalActions");
    if (bar) bar.closest(".summary-card").after($("editor"));
    $("draft").value = text; $("editor").classList.remove("hidden"); $("draftState").textContent = "";
    if (bar) bar.classList.add("hidden");
    $("draft").focus();
  } catch (e) { toast(e.message, "error"); }
}
$("draft").addEventListener("input", () => {
  $("draftState").textContent = "保存中…"; clearTimeout(draftTimer);
  draftTimer = setTimeout(async () => { try { await api("/api/draft", {method: "PUT", body: JSON.stringify({text: $("draft").value})}); $("draftState").textContent = "自動保存しました"; } catch (e) { $("draftState").textContent = "保存できません: " + e.message; } }, 600);
});
$("saveApprove").onclick = async () => {
  try { const r = await api("/api/approve_edit", {method: "POST", body: JSON.stringify({text: $("draft").value})}); toast(r.message); clearApproval(); $("actionbar").classList.add("hidden"); }
  catch (e) { toast(e.message, "error"); }
};
$("editClose").onclick = () => { $("editor").classList.add("hidden"); document.getElementById("approvalActions")?.classList.remove("hidden"); toast("書きかけは保存してあります。「修正して承認」で続きから開けます"); };
async function closeMeeting() {
  if (lastState && lastState.meeting && lastState.meeting.running && !lastState.readonly && !confirm("会議を閉じますか？ 進行中の会議は中断として記録され、あとで再開できます。")) return;
  await api("/api/close", {method: "POST"}); mode = null; await refresh(true);
}
$("closeBtn").onclick = closeMeeting; $("homeBtn").onclick = closeMeeting;
function setTab(t) {
  $("meeting").classList.toggle("show-log", t === "log"); $("meeting").classList.toggle("show-side", t === "side");
  document.querySelectorAll("#mtabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
}
document.querySelectorAll("#mtabs button").forEach(b => b.onclick = () => setTab(b.dataset.tab));

async function refresh(force = false, cfgNow = false) {
  let st;
  try { st = await api("/api/state"); } catch (e) { $("statusline").textContent = "接続できません: " + e.message; return; }
  lastState = st;
  // サーバーの再起動（boot）や画面のファイルの更新（asset_version）を見つけたら、表示中の画面のまま再読み込みする
  const ver = st.boot + ":" + st.asset_version;
  if (window.__ver && window.__ver !== ver && !setDirty) { location.reload(); return; }
  window.__ver = ver;
  $("liveDot").classList.toggle("hidden", !(st.mode === "meeting" && st.meeting.running));
  if (route !== "") return;
  if (force || st.mode !== mode || (st.mode === "meeting" && $("title").dataset.id !== st.meeting.id)) {
    mode = st.mode;
    $("closeBtn").classList.toggle("hidden", mode !== "meeting");
    if (mode === "home") {
      if (es) { es.close(); es = null; }
      show("v-home");
      $("title").textContent = "会議"; $("title").dataset.id = ""; document.title = "Dartmouth";
      $("inputbar").classList.add("hidden"); $("actionbar").classList.add("hidden"); $("statusline").textContent = "";
      await loadHome(st);
    } else {
      show("meeting", "mtabs");
      $("title").textContent = st.meeting.name; $("title").dataset.id = st.meeting.id; document.title = "Dartmouth: " + st.meeting.name;
      $("log").innerHTML = ""; seen = new Set(); colors = st.colors || {};
      ["editor", "ended"].forEach(id => $(id).classList.add("hidden")); $("endedExtend").innerHTML = ""; delete $("endedExtend").dataset.for;
      $("side").appendChild($("editor"));
      $("minutes").textContent = "（まだありません）"; $("minutes").classList.add("muted");
      try { const mn = await api("/api/minutes"); $("minutes").innerHTML = mn.html; $("minutes").classList.remove("muted"); } catch (e) {}
      connect();
    }
  }
  if (mode === "meeting") renderStatus(st);
  else if (mode === "home" && st.configs) { if (cfgNow) homeSig = ""; syncHomeConfigs(st); }
}


/* ================================================================ 起動 */
onRoute();
setInterval(() => refresh(false), 1000);
