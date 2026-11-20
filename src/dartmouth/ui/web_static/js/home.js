"use strict";
/* ================================================================ 会議: ホーム */
let mode = null, es = null, seen = new Set(), colors = {};
function topicRow(title = "", premise = "", from = null) {
  const row = document.createElement("div"); row.className = "topic-row";
  row.innerHTML = `<input class="t-title" placeholder="議題"><button type="button" class="danger" title="削除">✕</button>
    <textarea class="t-premise" placeholder="前提条件（任意）" rows="1"></textarea>`;
  row.querySelector(".t-title").value = title; row.querySelector(".t-premise").value = premise;
  if (from) row.dataset.from = from;
  row.querySelector("button").onclick = () => { row.remove(); syncTopicsWrap(); };
  return row;
}
function syncTopicsWrap() { $("topicsWrap").classList.toggle("hidden", !$("topics").children.length); updatePickCount(); }
let fromMeetings = new Set(), homeConfigs = [];
function applyAgentsInfo() {
  const ag = homeConfigs.find(x => x.name === "agents.yaml") || homeConfigs.find(x => x.has_agents);
  const cm = $("chairModel").value === "opus" ? "Opus (Claude)" : "Argon (Gemini 4)";
  const ce = $("chairEffort").value;
  const parts = [`議長: ${cm} / effort=${ce}`];
  if (ag && ag.backends && ag.backends.length) parts.push(`配役候補: ${ag.backends.join(", ")}`);
  $("agentsInfo").textContent = parts.join(" ／ ");
}
/* 議題の選択: 全案件の議題（議長の提案・議題ファイル）を1つの一覧に並べ、押して選ぶ。テーマや案件を先に選ばない。
   会議は1つの案件で開くので、別の案件の議題を押すとその案件に切り替わる（前の選択は外す）。選んだ状態はファイルが書き換わっても保つ */
const pk = {sel: new Map(), seen: new Map(), editing: null, deferred: false};
const pkKey = (c, title) => c.path + "\u0000" + title;
function curCfg() { return homeConfigs.find(x => String(x.index) === $("cfg").value); }
const cfgName = (c) => c.theme ? c.label : c.name;
function pkState(c, t) {
  const k = pkKey(c, t.title);
  if (!pk.sel.has(k)) pk.sel.set(k, {on: false});
  return pk.sel.get(k);
}
function hasPicks() {
  const c = curCfg();
  return !!c && (propTopics(c).length > 0 || (c.topics || []).some(t => pkState(c, t).on)
    || (left.items[c.project] || []).some(it => left.on.has(it.uid)));
}
function clearPicks() { for (const st of pk.sel.values()) st.on = false; prop.on.clear(); left.on.clear(); }
function useProject(c) {
  if (String(c.index) === $("cfg").value) return;
  if (hasPicks()) toast(`案件「${cfgName(c)}」に切り替えました（会議は1つの案件で開くので、ほかの案件の選択は外しました）`, "warning");
  clearPicks();
  $("cfg").value = String(c.index);
}
function renderPicker() {
  if (pk.editing) { pk.deferred = true; return; }
  const c = curCfg(), box = $("picker");
  box.innerHTML = "";
  const list = projectConfigs();
  for (const x of list) if (x.project) loadProposal(x);
  const hasTopics = (x) => (x.topics || []).length || (x.project && prop.data[x.project] && prop.data[x.project].topics.length);
  // 選択中の案件を先頭に。議題のない案件は、議長に作ってもらうボタンだけの1行にする
  const full = list.filter(hasTopics).sort((a, b) => (b === c) - (a === c));
  for (const x of full) box.appendChild(projectSection(x, x === c));
  const empty = list.filter(x => !hasTopics(x) && x.project && (prop.open[x.project] || prop.running.has(x.project)));
  for (const x of empty) box.appendChild(projectSection(x, false));
  if (!full.length) box.insertAdjacentHTML("afterbegin", '<p class="muted" style="margin:4px 0">議題がまだありません。下の「議長に議題を作ってもらう」か、細かい設定から議題を足してください</p>');
  if (c) {
    $("mname").placeholder = c.meeting_name ? `空なら「${c.meeting_name}」` : "例: homeserver-storage";
    $("purpose").placeholder = c.purpose ? `空なら: ${c.purpose}` : "";
    const about = c.description || c.purpose || "";
    $("cfgInfo").textContent = about ? about.slice(0, 140) + (about.length > 140 ? "…" : "") : (c.theme ? "" : c.path);
  }
  renderLeftovers(c);
  updatePickCount();
}
function projectSection(c, isCur) {
  const sec = document.createElement("div");
  const h = document.createElement("div"); h.className = "pkg";
  h.innerHTML = `<span>${esc(cfgName(c))}${c.theme ? ` <span class="muted" style="font-weight:400">${esc(c.theme)}</span>` : ""}</span>`;
  if (c.project) h.appendChild(propControl(c));
  sec.appendChild(h);
  const d = c.project && prop.data[c.project];
  for (const t of (d ? d.topics : [])) sec.appendChild(propCard(c, t));
  if (d && isCur) {
    const extra = [];
    if (d.skipped && d.skipped.length) extra.push(`会議では決まらないので外したもの ${d.skipped.length} 件（${d.skipped.slice(0, 3).map(x => x.uid.split(":")[1] + ": " + x.reason).join(" / ")}${d.skipped.length > 3 ? " …" : ""}）`);
    if (d.left_out && d.left_out.length) extra.push(`今回は入れなかった未解決事項 ${d.left_out.length} 件（細かい設定から個別に選べます）`);
    if (extra.length) sec.insertAdjacentHTML("beforeend", `<div class="muted" style="font-size:.8rem;margin-bottom:6px">${esc(extra.join("　"))}</div>`);
  }
  const seen = pk.seen.get(c.path);
  for (const t of c.topics || []) sec.appendChild(pkCard(c, t, seen && !seen.has(t.title)));
  pk.seen.set(c.path, new Set((c.topics || []).map(t => t.title)));
  return sec;
}
function pkCard(c, t, isNew) {
  const st = pkState(c, t);
  const el = document.createElement("div"); el.className = "pk" + (st.on ? " on" : "");
  const title = st.title ?? t.title, premise = st.premise ?? t.premise ?? "";
  const meta = [];
  if (t.group) meta.push(esc(t.group));
  if (st.title != null || st.premise != null) meta.push("この会議だけ変更あり");
  el.innerHTML = `<div class="ck">✓</div><div class="bd"><div class="pt">${esc(title)}${isNew ? ' <span class="pill live">新しく追加</span>' : ""}</div>
    ${premise ? `<div class="pp">${esc(premise)}</div>` : ""}${meta.length ? `<div class="pm">${meta.join(" · ")}</div>` : ""}</div>
    <button type="button" class="ed" title="中身を直す">✎</button>`;
  el.onclick = () => { useProject(c); st.on = !st.on; renderPicker(); };
  el.querySelector(".ed").onclick = (e) => { e.stopPropagation(); pkEdit(el, c, t); };
  return el;
}
function pkEdit(el, c, t) {
  const st = pkState(c, t);
  pk.editing = t.title;
  const f = document.createElement("div"); f.className = "pk-edit";
  f.innerHTML = `<label>議題</label><input class="e-title"><label>前提（任意）</label><textarea class="e-premise" rows="3"></textarea>
    <label>グループ（任意）</label><input class="e-group" list="pkGroups">
    <div class="row"><button type="button" class="small primary" data-a="save">設定ファイルに保存</button>
      <button type="button" class="small" data-a="once">この会議だけ変える</button>
      <button type="button" class="small danger" data-a="rm">設定ファイルから消す</button>
      <button type="button" class="small" data-a="cancel">やめる</button></div>`;
  f.querySelector(".e-title").value = st.title ?? t.title;
  f.querySelector(".e-premise").value = st.premise ?? t.premise ?? "";
  f.querySelector(".e-group").value = t.group || "";
  const dl = document.getElementById("pkGroups") || Object.assign(document.createElement("datalist"), {id: "pkGroups"});
  dl.innerHTML = [...new Set((c.topics || []).map(x => x.group).filter(Boolean))].map(g => `<option value="${esc(g)}">`).join("");
  document.body.appendChild(dl);
  const close = () => { pk.editing = null; pk.deferred = false; renderPicker(); };
  f.querySelectorAll("button").forEach(b => b.onclick = async () => {
    const nt = f.querySelector(".e-title").value.trim(), np = f.querySelector(".e-premise").value;
    if (b.dataset.a === "cancel") return close();
    if (b.dataset.a === "once") {
      useProject(c);
      st.title = nt && nt !== t.title ? nt : null; st.premise = np !== (t.premise || "") ? np : null;
      st.on = true; return close();
    }
    try {
      if (b.dataset.a === "rm") {
        if (!confirm(`「${t.title}」を設定ファイルから消しますか？（元の版は .bak に残ります）`)) return;
        await api("/api/config/topics", {method: "POST", body: JSON.stringify({config_path: c.path, action: "remove", title: t.title})});
        pk.sel.delete(pkKey(c, t.title));
      } else {
        if (!nt) return toast("議題のタイトルを入れてください", "warning");
        await api("/api/config/topics", {method: "POST", body: JSON.stringify({config_path: c.path, action: "update", title: t.title,
          new_title: nt, premise: np, group: f.querySelector(".e-group").value})});
        const old = pk.sel.get(pkKey(c, t.title)); pk.sel.delete(pkKey(c, t.title));
        pk.sel.set(pkKey(c, nt), {on: old ? old.on : false});
        pk.seen.get(c.path)?.add(nt);
      }
      toast("設定ファイルを更新しました", "ok");
      pk.editing = null; await refresh(false, true);
    } catch (e) { toast(e.message, "error"); }
  });
  el.replaceWith(f);
  f.querySelector(".e-title").focus();
}
function pickedTopics() {
  const c = curCfg();
  const out = [];
  if (c) {
    for (const t of propTopics(c)) out.push({title: t.title, premise: t.premise});
    for (const t of c.topics || []) {
      const st = pkState(c, t);
      if (st.on) out.push({title: st.title ?? t.title, premise: st.premise ?? t.premise ?? ""});
    }
    for (const it of (left.items[c.project] || [])) {
      if (left.on.has(it.uid)) out.push({title: it.text, premise: leftPremise(it)});
    }
  }
  return out;
}
function manualTopics() {
  return [...$("topics").children].map(r => ({title: r.querySelector(".t-title").value.trim(), premise: r.querySelector(".t-premise").value.trim()})).filter(t => t.title);
}
function updatePickCount() {
  const c = curCfg();
  const np = c ? propTopics(c).length : 0;
  const nf = c ? (c.topics || []).filter(t => pkState(c, t).on).length : 0;
  const nl = c ? (left.items[c.project] || []).filter(it => left.on.has(it.uid)).length : 0;
  const nm = manualTopics().length, n = np + nf + nl + nm;
  const parts = [[np, "議長の提案"], [nf, "議題ファイル"], [nl, "個別に選んだ論点"], [nm, "この会議だけの議題"]].filter(([k]) => k).map(([k, l]) => `${l} ${k}`);
  $("pickCount").textContent = parts.length ? (c && np + nf + nl ? `「${cfgName(c)}」で ` : "") + parts.join("・") : "議題を選んでください";
  $("startBtn").textContent = n ? `この ${n} 件で会議を始める →` : "会議を始める →";
  $("startBtn").disabled = !n;
}
/* 議長の提案: 案件の未解決事項を束ねた議題。案件ごとに、前の提案（未解決事項が変わっていなければ）を一覧に出す */
const prop = {data: {}, at: {}, on: new Set(), running: new Set(), open: {}, loading: new Set()};
const propKey = (c, t) => c.project + "\u0000" + t.title;
function propTopics(c) {
  const d = c && c.project && prop.data[c.project];
  return d ? d.topics.filter(t => prop.on.has(propKey(c, t))) : [];
}
async function loadProposal(c, force) {
  if (!c.project || prop.loading.has(c.project) || (!force && prop.at[c.project] && Date.now() - prop.at[c.project] < 30000)) return;
  prop.loading.add(c.project);
  try {
    const r = await api("/api/agenda/proposal?project=" + encodeURIComponent(c.project));
    prop.data[c.project] = r.proposal; prop.open[c.project] = r.open_items; prop.at[c.project] = Date.now();
    if (r.running) prop.running.add(c.project);
    renderPicker();
  } catch (e) { prop.at[c.project] = Date.now(); /* 台帳がなくても議題は選べる */ }
  finally { prop.loading.delete(c.project); }
}
function propControl(c) {
  const w = document.createElement("span"); w.className = "row"; w.style.cssText = "gap:6px;align-items:center;font-weight:400";
  const d = prop.data[c.project], n = prop.open[c.project], running = prop.running.has(c.project);
  if (!d && !n && !running) return w;
  const s = document.createElement("span"); s.className = "muted";
  s.textContent = running ? "議長が議題を作っています（1〜2分）" : n === 0 ? "未解決事項なし" : d ? `未解決事項 ${d.items} 件 → 議題 ${d.topics.length} 個` : `未解決事項 ${n} 件`;
  w.appendChild(s);
  if (!running && n !== 0) {
    const b = document.createElement("button"); b.type = "button"; b.className = "small";
    b.textContent = d ? "作り直してもらう" : "議長に議題を作ってもらう";
    b.onclick = (e) => { e.stopPropagation(); propose(c); };
    w.appendChild(b);
  }
  return w;
}
function propCard(c, t) {
  const k = propKey(c, t), on = prop.on.has(k);
  const el = document.createElement("div"); el.className = "pk" + (on ? " on" : "");
  el.innerHTML = `<div class="ck">✓</div><div class="bd"><div class="pt">${esc(t.title)} <span class="pill">議長の提案</span></div>
    <div class="pp" style="-webkit-line-clamp:3">${esc(t.goal)}</div>
    <ul class="its">${t.items.map(it => `<li>${esc(it.text.length > 70 ? it.text.slice(0, 70) + "…" : it.text)} <span class="muted">${esc(it.uid.split(":")[1])}</span></li>`).join("")}</ul></div>`;
  el.onclick = () => { useProject(c); prop.on.has(k) ? prop.on.delete(k) : prop.on.add(k); renderPicker(); };
  return el;
}
async function propose(c) {
  prop.running.add(c.project); renderPicker();
  try {
    const r = await api("/api/agenda/propose", {method: "POST", body: JSON.stringify({config_path: c.path, chair: $("chairModel").value, chair_effort: $("chairEffort").value})});
    prop.data[c.project] = r.proposal; prop.at[c.project] = Date.now();
    if (!r.proposal.topics.length) toast("議題にできる未解決事項がありませんでした", "warning");
    else {
      // 作ってもらった議題は選んだ状態にする（その案件に切り替える）
      useProject(c);
      for (const k of [...prop.on]) if (k.startsWith(c.project + "\u0000")) prop.on.delete(k);
      for (const t of r.proposal.topics) prop.on.add(propKey(c, t));
    }
  } catch (e) { toast(e.message, "error"); }
  finally { prop.running.delete(c.project); renderPicker(); }
}
/* 台帳に残っている論点（協議事項・不明点・未分類の未決）を、議題の候補として出す */
const left = {items: {}, on: new Set(), loading: new Set(), shown: 8, at: {}};
const leftPremise = (it) => `台帳の ${it.uid}（${LEFT_KIND[it.kind] || it.kind}）から。` + (it.needs ? `分かればよいこと・決める相手: ${it.needs}` : "")
  + (it.topic_title ? `（元の議題: ${it.topic_title}）` : "");
const LEFT_KIND = {discussion: "協議事項", unknown: "不明点", question: "未決"};
async function loadLeftovers(c, force) {
  if (!c || !c.project || left.loading.has(c.project)) return;
  if (!force && left.items[c.project] && Date.now() - (left.at[c.project] || 0) < 60000) return;
  left.loading.add(c.project);
  try {
    const r = await api("/api/ledger/candidates?project=" + encodeURIComponent(c.project));
    left.items[c.project] = r.items; left.at[c.project] = Date.now();
    if (curCfg() === c || (curCfg() && curCfg().project === c.project)) renderLeftovers(c, true);
  } catch (e) { left.items[c.project] = []; }
  finally { left.loading.delete(c.project); }
}
function renderLeftovers(c, fromLoad) {
  if (!c || !c.project) { $("leftBox").classList.add("hidden"); return; }
  if (!fromLoad) loadLeftovers(c);
  const items = left.items[c.project];
  if (!items) { $("leftBox").classList.add("hidden"); return; }
  $("leftBox").classList.toggle("hidden", !items.length);
  $("leftTitle").textContent = `未解決事項から個別に選ぶ（${items.length}件）`;
  $("leftPicker").innerHTML = "";
  for (const it of items.slice(0, left.shown)) {
    const on = left.on.has(it.uid);
    const el = document.createElement("div"); el.className = "pk" + (on ? " on" : "");
    const meta = [LEFT_KIND[it.kind] || it.kind, it.uid.split(":")[0].slice(5) + " " + it.uid.split(":")[1]];
    if (it.unlocks) meta.push(`これが決まると ${it.unlocks} 件が進む`);
    if (it.milestone_key) meta.push(it.milestone_key);
    if (ownerLabel(it.owner)) meta.push("担当: " + ownerLabel(it.owner));
    el.innerHTML = `<div class="ck">✓</div><div class="bd"><div class="pt">${esc(it.text)}</div>
      ${it.needs ? `<div class="pp">${esc(it.needs)}</div>` : ""}<div class="pm">${esc(meta.join(" · "))}</div></div>`;
    el.onclick = () => { left.on.has(it.uid) ? left.on.delete(it.uid) : left.on.add(it.uid); el.classList.toggle("on"); updatePickCount(); };
    $("leftPicker").appendChild(el);
  }
  const rest = items.length - left.shown;
  $("leftMore").classList.toggle("hidden", rest <= 0);
  $("leftMore").textContent = `ほか ${rest} 件も見る`;
  $("leftMore").onclick = () => { left.shown += 20; renderLeftovers(c, true); };
  updatePickCount();
}
$("newTopicBtn").onclick = () => {
  const c = curCfg(); if (!c) return;
  const f = $("newTopicForm"); f.classList.remove("hidden");
  f.innerHTML = `<label>新しい議題（${c.theme ? "議題ファイルの「" + esc(c.label) + "」" : esc(c.name)} に書き込みます）</label><input class="e-title" placeholder="議題"><label>前提（任意）</label><textarea class="e-premise" rows="2"></textarea>
    <label>グループ（任意）</label><input class="e-group" list="pkGroups2"><datalist id="pkGroups2">${[...new Set((c.topics || []).map(x => x.group).filter(Boolean))].map(g => `<option value="${esc(g)}">`).join("")}</datalist>
    <div class="row"><button type="button" class="small primary" data-a="add">設定ファイルに足して選ぶ</button><button type="button" class="small" data-a="cancel">やめる</button></div>`;
  f.querySelector('[data-a="cancel"]').onclick = () => { f.classList.add("hidden"); f.innerHTML = ""; };
  f.querySelector('[data-a="add"]').onclick = async () => {
    const title = f.querySelector(".e-title").value.trim();
    if (!title) return toast("議題のタイトルを入れてください", "warning");
    try {
      await api("/api/config/topics", {method: "POST", body: JSON.stringify({config_path: c.path, action: "add", title,
        premise: f.querySelector(".e-premise").value, group: f.querySelector(".e-group").value})});
      pk.sel.set(pkKey(c, title), {on: true}); pk.seen.get(c.path)?.add(title);
      f.classList.add("hidden"); f.innerHTML = "";
      toast(`「${title}」を足しました`, "ok"); await refresh(false, true);
    } catch (e) { toast(e.message, "error"); }
  };
  f.querySelector(".e-title").focus();
};
function cfgOptionLabel(c) {
  const n = (c.topics || []).length;
  return `${c.theme ? c.theme + " / " : ""}${cfgName(c)}${n ? `（議題 ${n}）` : "（議題なし）"}`;
}
function projectConfigs() {
  // 議長・参加者だけの設定（agents.yaml など）は案件として出さない
  return homeConfigs.filter(c => c.theme || (c.name !== "agents.yaml" && !(c.has_agents && !(c.topics || []).length && !c.done_count && !c.meeting_name)));
}
function fillCfgSelects(preferIndex) {
  const list = projectConfigs();
  $("cfg").innerHTML = list.map(c => `<option value="${esc(c.index)}">${esc(cfgOptionLabel(c))}</option>`).join("");
  const want = list.find(c => String(c.index) === String(preferIndex)) || list.find(c => (c.topics || []).length) || list[0];
  if (want) $("cfg").value = String(want.index);
}
let homeSig = "";
function syncHomeConfigs(st) {
  // 設定ファイルの議題が変わったら、画面を再読み込みせずに議題の一覧だけ差し替える
  const sig = JSON.stringify(st.configs.map(c => [c.path, c.label, c.topics, c.done_topics, c.meeting_name, c.purpose, c.error]));
  if (sig === homeSig) return;
  homeSig = sig;
  homeConfigs = st.configs;
  fillCfgSelects($("cfg").value || null);
  renderPicker();
}
$("chairModel").onchange = applyAgentsInfo;
$("chairEffort").onchange = applyAgentsInfo;
$("cfg").onchange = () => { const c = curCfg(); if (c && c.error) toast(`${c.name} を読めません: ${c.error}`, "error"); clearPicks(); renderPicker(); };
$("topics").addEventListener("input", updatePickCount);
async function loadHome(st) {
  homeConfigs = st.configs;
  applyAgentsInfo();

  homeSig = "";
  syncHomeConfigs(st);
  $("noCfg").classList.toggle("hidden", st.configs.length > 0);
  $("startBtn").disabled = st.configs.length === 0;
  if (pendingStart) {
    $("topics").innerHTML = "";
    pendingStart.topics.forEach(t => $("topics").appendChild(topicRow(t.title, t.premise, t.from)));
    pkClearSelection();
    fromMeetings = new Set(pendingStart.from_meetings);
    $("fromBox").classList.remove("hidden"); $("fromItems").innerHTML = "";
    $("fromTitle").textContent = `「決定・未決・ToDo」で選んだ ${pendingStart.topics.length} 件を議題にしました（${fromMeetings.size} 会議から）`;
    pendingStart = null;
  }
  renderPicker(); syncTopicsWrap();
  try {
    const {meetings} = await api("/api/meetings");
    $("meetings").classList.remove("muted");
    $("meetings").innerHTML = meetings.length ? "" : '<span class="muted">まだありません</span>';
    for (const m of meetings.filter(m => !m.archived).slice(0, 8)) $("meetings").appendChild(meetingItem(m, true));
  } catch (e) { $("meetings").textContent = "読み込めません: " + e.message; }
}
function meetingItem(m, withFollow) {
  const el = document.createElement("div"); el.className = "meeting-item";
  const badge = m.archived ? "" : m.state === "終了" ? "ok" : m.running ? "live" : m.state.includes("承認待ち") ? "warn" : "bad";
  el.innerHTML = `<div class="grow"><a href="#/archive/${encodeURIComponent(m.id)}">${esc(m.name)}</a>
    <div class="sub">${m.archived ? `<span class="pill" title="${esc(m.archived)}">アーカイブ</span>` : ""}<span class="pill ${badge}">${esc(m.state)}${m.phase_label && m.state !== "終了" ? "・" + esc(m.phase_label) : ""}</span>
    <span class="chip">未決 ${m.open_count}</span><span class="chip">ToDo ${m.todo_count}</span><span class="muted">${esc((m.updated || "").slice(0, 16).replace("T", " "))}</span></div></div>`;
  const acts = document.createElement("div"); acts.className = "row"; acts.style.gap = "6px";
  if (m.running && !m.archived) { const b = document.createElement("button"); b.className = "small"; b.textContent = "見る"; b.onclick = async () => { try { await api(`/api/watch/${encodeURIComponent(m.id)}`, {method: "POST"}); location.hash = ""; refresh(true); } catch (e) { toast(e.message, "error"); } }; acts.appendChild(b); }
  if (m.resumable && !m.archived) { const b = document.createElement("button"); b.className = "small primary"; b.textContent = "再開"; b.onclick = () => resume(m.id); acts.appendChild(b); }
  if (withFollow) { const f = document.createElement("button"); f.className = "small"; f.textContent = "未決事項から始める"; f.onclick = () => pickFrom(m.id); acts.appendChild(f); }
  if (m.resumable && !m.running && !m.archived) { acts.appendChild(deleteBtn(m, () => loadHome({configs: homeConfigs}))); }
  el.appendChild(acts);
  return el;
}
function deleteBtn(m, onDone) {
  const b = document.createElement("button"); b.className = "small danger"; b.textContent = "削除";
  b.onclick = async () => {
    if (!confirm(`会議「${m.name}」（${m.id}）を削除しますか？\nこの操作は元に戻せません。`)) return;
    try { await api(`/api/archive/${encodeURIComponent(m.id)}`, {method: "DELETE"}); toast(`「${m.name}」を削除しました`, "ok"); onDone(); }
    catch (e) { toast(e.message, "error"); }
  };
  return b;
}
async function pickFrom(id) {
  if (route !== "") { location.hash = "#/"; await new Promise(r => setTimeout(r, 300)); }
  const m = await api("/api/meetings/" + encodeURIComponent(id));
  fromMeetings = new Set([id]);
  $("fromBox").classList.remove("hidden");
  $("fromTitle").textContent = `前の会議「${m.name}」から（チェックした項目が議題に入ります）`;
  const items = [...m.open_questions.filter(q => !q.done).map(q => ({...q, kind: "未決"})), ...m.todos.filter(t => !t.done).map(t => ({...t, kind: "ToDo"}))];
  $("fromItems").innerHTML = items.length ? "" : '<p class="muted">未完了の未決事項・ToDo はありません。議事録だけを前提資料として渡せます。</p>';
  for (const it of items) {
    const row = document.createElement("div"); row.className = "pick";
    const key = id + ":" + it.id;
    row.innerHTML = `<input type="checkbox"><label style="margin:0;color:var(--fg)">[${esc(it.id)}] ${esc(it.kind)}: ${esc(it.text)}${ownerLabel(it.owner) ? '<span class="muted">（担当: ' + esc(ownerLabel(it.owner)) + "）</span>" : ""}</label>`;
    row.querySelector("input").onchange = (e) => {
      const rows = [...$("topics").children];
      const exist = rows.find(r => r.dataset.from === key);
      if (e.target.checked && !exist) {
        if (!rows.some(r => r.dataset.from)) pkClearSelection();  // 前の会議から選ぶときは、設定ファイルの議題の選択を外す
        $("topics").appendChild(topicRow(it.text, `前の会議の ${it.id}（${it.kind}）から。`, key));
      } else if (!e.target.checked && exist) exist.remove();
      syncTopicsWrap();
    };
    row.querySelector("label").onclick = () => row.querySelector("input").click();
    $("fromItems").appendChild(row);
  }
  if (!$("mname").value) $("mname").value = m.name + "-followup";
  $("fromBox").scrollIntoView({behavior: "smooth"});
}
$("fromClear").onclick = () => {
  fromMeetings = new Set(); $("fromBox").classList.add("hidden");
  [...$("topics").children].filter(r => r.dataset.from).forEach(r => r.remove());
  syncTopicsWrap();
};
$("addTopic").onclick = () => { const r = topicRow(); $("topics").appendChild(r); syncTopicsWrap(); r.querySelector(".t-title").focus(); };
function pkClearSelection() { clearPicks(); renderPicker(); }
function qaRow(q = "", a = "") {
  const row = document.createElement("div"); row.className = "topic-row"; row.style.marginBottom = "8px";
  row.innerHTML = `<input class="qa-q" placeholder="質問・項目"><button type="button" class="danger" title="削除">✕</button>
    <textarea class="qa-a" placeholder="回答を入力" rows="1"></textarea>`;
  row.querySelector(".qa-q").value = q; row.querySelector(".qa-a").value = a;
  row.querySelector("button").onclick = () => row.remove();
  return row;
}
$("qaAddBtn").onclick = () => $("qaRows").appendChild(qaRow());
$("qaLoadBtn").onclick = async () => {
  try {
    const {items} = await api("/api/items");
    const uq = items.filter(i => !i.done && i.kind !== "decision" && (i.owner === "user" || i.owner === "ユーザー"));
    if (!uq.length) { toast("担当: user の未解決項目はありません", "warning"); return; }
    $("qaRows").innerHTML = "";
    uq.forEach(i => $("qaRows").appendChild(qaRow(`[${i.id}] ${i.text}`)));
  } catch (e) { toast(e.message, "error"); }
};
$("qaApplyBtn").onclick = () => {
  const pairs = [...$("qaRows").children].map(r => ({q: r.querySelector(".qa-q").value.trim(), a: r.querySelector(".qa-a").value.trim()})).filter(x => x.q && x.a);
  if (!pairs.length) { toast("質問と回答を入力してください", "warning"); return; }
  const block = "【ユーザー回答】\n" + pairs.map(x => `- ${x.q}\n  → ${x.a}`).join("\n");
  // 選んだ議題の先頭（なければこの会議だけの議題の先頭）の前提に足す。設定ファイルは書き換えない
  const c = curCfg();
  const firstPicked = c && (c.topics || []).find(t => pkState(c, t).on);
  if (firstPicked) {
    const t = firstPicked, st = pkState(c, t), cur = st.premise ?? t.premise ?? "";
    st.premise = cur.trim() ? cur.trim() + "\n\n" + block : block;
    renderPicker();
  } else {
    if (!$("topics").children.length) { $("topics").appendChild(topicRow()); syncTopicsWrap(); }
    const first = $("topics").children[0].querySelector(".t-premise");
    first.value = first.value.trim() ? first.value.trim() + "\n\n" + block : block;
  }
  $("qaRows").innerHTML = "";
  toast(`${pairs.length}件の回答を議題の前提に反映しました`, "ok");
};
$("startBtn").onclick = async () => {
  const topics = [...pickedTopics(), ...manualTopics()];
  const c = curCfg();
  const ag = homeConfigs.find(x => x.name === "agents.yaml");
  if (!topics.length) { toast("議題を選んでください", "warning"); return; }
  $("startBtn").disabled = true;
  try {
    await api("/api/start", {method: "POST", body: JSON.stringify({config: 0, config_path: c ? c.path : null, agents_config_path: ag ? ag.path : null, chair: $("chairModel").value, chair_effort: $("chairEffort").value, name: $("mname").value, purpose: $("purpose").value, topics, from_meetings: [...fromMeetings], include_minutes: $("inclMinutes").checked})});
    $("topics").innerHTML = ""; syncTopicsWrap(); fromMeetings = new Set(); $("fromBox").classList.add("hidden"); $("mname").value = ""; $("purpose").value = "";
    if (c) for (const k of [...pk.sel.keys()]) if (k.startsWith(c.path + "\u0000")) pk.sel.delete(k);
    clearPicks(); left.items = {}; if (c && c.project) { delete prop.data[c.project]; delete prop.at[c.project]; }
    mode = null; await refresh(true);
  } catch (e) { toast(e.message, "error"); } finally { $("startBtn").disabled = false; }
};
/* 終わった会議を、こちらの指示で延長する（議題を選び、議論してほしいことを書く） */
async function extendForm(box, id) {
  box.innerHTML = "";
  let s;
  try { s = await api("/api/meetings/" + encodeURIComponent(id)); } catch (e) { return; }
  if (s.phase !== "done" || !s.topics.length) return;
  const hv = s.handovers || {};
  const w = document.createElement("div"); w.style.margin = "10px 0";
  w.innerHTML = `<label>この会議を延長する</label><div class="row" style="gap:6px;flex-wrap:wrap"><select style="flex:1;min-width:12em"></select><button class="small primary">延長する</button></div>
    <textarea rows="2" placeholder="延長で議論してほしいこと（任意。申し送りに足して最優先で扱わせます）" style="margin-top:6px"></textarea>`;
  const sel = w.querySelector("select");
  let def = null;
  for (const t of s.topics) {
    const h = hv[String(t.no)];
    const o = document.createElement("option"); o.value = t.no;
    o.textContent = `議題${t.no}: ${t.title}${h ? `（申し送り: ${h.status}）` : ""}`;
    sel.appendChild(o);
    if (def === null && h && h.status.includes("見送り")) def = t.no;
  }
  if (def !== null) sel.value = String(def);
  const btn = w.querySelector("button");
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      const r = await api("/api/extend/" + encodeURIComponent(id), {method: "POST", body: JSON.stringify({topic: Number(sel.value), note: w.querySelector("textarea").value})});
      toast(`議題${sel.value}を延長します（議題${r.topic}）`, "ok");
      if (route !== "") location.hash = "#/"; mode = null; await refresh(true);
    } catch (e) { toast(e.message, "error"); btn.disabled = false; }
  };
  box.appendChild(w);
}
async function resume(id) {
  try { await api("/api/resume/" + encodeURIComponent(id), {method: "POST"}); if (route !== "") location.hash = "#/"; mode = null; await refresh(true); }
  catch (e) { toast(e.message, "error"); }
}
