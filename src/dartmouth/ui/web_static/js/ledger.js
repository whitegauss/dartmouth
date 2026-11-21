"use strict";
/* ================================================================ 台帳 */
const LG_VIEWS = [
  ["all", "すべて", {}], ["decision", "決定", {kind: "decision"}], ["premise", "前提", {kind: "premise"}],
  ["unknown", "不明点", {kind: "unknown"}], ["discussion", "協議事項", {kind: "discussion"}], ["todo", "ToDo", {kind: "todo"}],
  ["question", "未決（未分類）", {kind: "question"}], ["ready", "すぐ実行できる", {kind: "decision,todo", executable: "auto"}],
  ["blocked", "止まっている", {}],
  ["ball_ai", "AI の番", {ball: "ai"}], ["ball_self", "本人の番", {ball: "self"}], ["ball_external", "外部待ち", {ball: "external"}],
];
const LG_BALL_PILL = {ai: "ok", self: "warn", external: "", blocked: "bad", held: ""};
const lgItemLink = (uid) => "#/ledger/item/" + encodeURIComponent(uid);
const lg = {view: "all", cell: null, data: null, open: null, jobTimer: null};
function lgParams() {
  const v = LG_VIEWS.find(x => x[0] === lg.view)[2];
  const p = new URLSearchParams();
  if ($("lgTheme").value) p.set("theme", $("lgTheme").value);
  if ($("lgProject").value) p.set("project", $("lgProject").value);
  if (lg.cell) { p.set("project", lg.cell.project); p.set("milestone", lg.cell.milestone); }
  p.set("status", $("lgStatus").value);
  if ($("lgText").value.trim()) p.set("text", $("lgText").value.trim());
  for (const [k, val] of Object.entries(v)) p.set(k, val);
  return p;
}
async function loadLedger(arg) {
  document.title = "Dartmouth: 台帳"; $("title").textContent = "台帳";
  if (!$("lgViews").children.length) {
    for (const [key, label] of LG_VIEWS) {
      const b = document.createElement("button"); b.className = "small"; b.textContent = label; b.dataset.v = key;
      b.onclick = () => { lg.view = key; lgRenderViews(); lgLoadItems(); };
      $("lgViews").appendChild(b);
    }
    lgRenderViews();
  }
  try {
    const st = await api("/api/ledger/classify");
    const keep = $("lgCfg").value;
    $("lgCfg").innerHTML = st.configs.map(c => `<option value="${esc(c.path)}">${esc(c.name)}</option>`).join("");
    if (keep) $("lgCfg").value = keep;
    lgShowJob(st);
  } catch (e) { /* 設定がなくても台帳は見られる */ }
  await lgLoadItems(true);
  if (arg && arg.startsWith("item/")) await lgOpenItem(arg.slice(5));
}
/* #/ledger/item/<ID> で開いたとき: その項目の案件に絞り、詳細を開いて見える位置まで送る */
async function lgOpenItem(uid) {
  let it;
  try { it = await api("/api/ledger/item/" + encodeURIComponent(uid)); } catch (e) { toast(`項目 ${uid} が見つかりません`, "error"); return; }
  $("lgTheme").value = ""; lgFillProjects();
  $("lgProject").value = it.project; $("lgStatus").value = "all"; $("lgText").value = "";
  lg.view = "all"; lg.cell = null; lg.open = uid; lgRenderViews();
  await lgLoadItems();
  const tr = [...$("lgTable").querySelectorAll("tbody tr")].find(r => r.dataset.uid === uid);
  if (tr) tr.scrollIntoView({block: "center"});
}
async function lgPackText() {
  const p = new URLSearchParams();
  if ($("lgProject").value) p.set("project", $("lgProject").value);
  if ($("lgPackOpen").checked) p.set("open", "1");
  const r = await fetch("/api/ledger/pack?" + p.toString());
  if (!r.ok) throw new Error(`書き出せません（${r.status}）`);
  return r.text();
}
const lgCopy = copyText;
$("lgPackCopy").onclick = async () => {
  try { const t = await lgPackText(); await lgCopy(t); toast(`コピーしました（${nf.format(t.length)} 字）`, "ok"); }
  catch (e) { toast(e.message, "error"); }
};
$("lgPackSave").onclick = async () => {
  try {
    const t = await lgPackText();
    const a = Object.assign(document.createElement("a"), {href: URL.createObjectURL(new Blob([t], {type: "text/markdown"})),
      download: `${$("lgProject").value || "all"}-context-pack.md`});
    a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } catch (e) { toast(e.message, "error"); }
};
function lgRenderViews() { $("lgViews").querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.v === lg.view)); }
async function lgLoadItems(refreshFilters) {
  let d;
  try { d = await api("/api/ledger?" + lgParams().toString()); } catch (e) { toast(e.message, "error"); return; }
  lg.data = d;
  if (refreshFilters) {
    const th = $("lgTheme").value;
    $("lgTheme").innerHTML = '<option value="">すべて</option>' + d.themes.map(t => `<option>${esc(t)}</option>`).join("");
    $("lgTheme").value = d.themes.includes(th) ? th : "";
    lgFillProjects();
  }
  const c = d.counts, L = d.labels;
  const kpi = (label, value, view) => `<div class="kpi" style="cursor:pointer" data-v="${view}"><div class="label">${label}</div><div class="value">${value}</div></div>`;
  $("lgKpis").innerHTML = ["discussion", "unknown", "question", "decision", "todo", "premise"].filter(k => c.open[k]).map(k => kpi(L.kind[k], c.open[k], k)).join("")
    + (c.unclassified ? kpi("AI 未分類", c.unclassified, "all") : "");
  $("lgKpis").querySelectorAll(".kpi").forEach(el => el.onclick = () => { lg.view = el.dataset.v; lgRenderViews(); lgLoadItems(); });
  $("lgMeta").textContent = `全 ${c.items} 件・案件 ${c.projects} · 取り込み ${(d.meta.synced_at || "—").slice(5, 16).replace("T", " ")} · 分類 ${(d.meta.classified_at || "—").slice(5, 16).replace("T", " ")}`;
  $("lgMeta").title = d.db;
  const pid = $("lgProject").value;
  $("lgPackScope").textContent = pid ? `案件「${pid}」を書き出します` : "全案件を書き出します（案件を選ぶとその案件だけ）";
  let items = d.items;
  if (lg.view === "blocked") items = items.filter(i => i.blocked_by.length);
  lgRenderTable(items);
  await lgRenderMatrix();
  await lgRenderTimeline();
}
async function lgRenderTimeline() {
  const th = lgTheme();
  let rows;
  try { rows = (await api("/api/ledger/timeline" + (th ? "?theme=" + encodeURIComponent(th) : ""))).rows; } catch (e) { return; }
  const pid = $("lgProject").value;
  if (pid) rows = rows.filter(r => r.project === pid);
  if (!rows.length) {
    $("lgTimeline").innerHTML = '<p class="muted">期限のあるマイルストーンはまだありません</p>';
    return;
  }
  // 範囲: 今月の前の月から、最後の期限の2か月後まで。2年以内なら月、それより長ければ年の目盛り
  const today = new Date();
  const ym = (s) => { const [y, m] = s.split("-").map(Number); return y * 12 + (m - 1); };
  const nowM = today.getFullYear() * 12 + today.getMonth();
  const m0 = Math.min(nowM - 1, ...rows.map(r => ym(r.due_key)));
  const m1 = Math.max(nowM + 3, ...rows.map(r => ym(r.due_key))) + 2;
  const span = m1 - m0;
  const xOf = (y, mo, d = 1) => ((y * 12 + mo - m0) + (d - 1) / 31) / span * 100;  // mo は 0 始まり
  const xDue = (k) => { const [y, m, d] = k.split("-").map(Number); return xOf(y, m - 1, d); };
  const ticks = [];
  for (let m = m0; m <= m1; m++) {
    const y = Math.floor(m / 12), mo = m % 12;
    // 月の目盛りは、最初と1月だけ年を付ける。最初の目盛りの直後は、重ならないよう文字を出さない
    if (span <= 24 ? true : mo === 0) ticks.push({x: xOf(y, mo), label: span <= 24 ? (m === m0 || mo === 0 ? `${y}年${mo + 1}月` : (m === m0 + 1 ? "" : `${mo + 1}月`)) : `${y}年`, major: mo === 0});
  }
  const left = 170;
  const trackW = Math.max(200, ($("lgTimeline").clientWidth || 800) - left);
  const grid = ticks.map(t => `<div class="tl-grid" style="left:${t.x}%;${t.major ? "background:var(--line-2)" : ""}"></div>`).join("")
    + `<div class="tl-today" style="left:${xOf(today.getFullYear(), today.getMonth(), today.getDate())}%" title="今日"></div>`;
  const icon = {done: "●", doing: "◐", todo: "○", empty: "○", na: "－"};
  const rank = {over: 0, doing: 1, todo: 2, empty: 3, na: 4, done: 5};
  let h = `<div class="tl" style="--tl-left:${left}px"><div class="tl-axis">${ticks.filter(t => t.x < 97).map((t, i) => `<span style="left:${t.x}%;${i === 0 ? "transform:none" : ""}">${t.label}</span>`).join("")}</div>`;
  const themes = [...new Set(rows.map(r => r.theme))];
  for (const t of themes) {
    if (themes.length > 1) h += `<div class="tl-theme">${esc(t)}</div>`;
    for (const p of [...new Set(rows.filter(r => r.theme === t).map(r => r.project))]) {
      const rs = rows.filter(r => r.theme === t && r.project === p);
      // 同じ期限のマイルストーンは1つの印にまとめる
      const groups = [];
      for (const r of rs) {
        const g = groups.find(x => x.due_key === r.due_key);
        if (g) g.items.push(r); else groups.push({due_key: r.due_key, due: r.due, items: [r]});
      }
      // 重なる印は段をずらす（文字数から幅を見積もる）
      const laneEnd = [];
      const chips = groups.map(g => {
        const label = g.items.map(r => r.key).join("・");
        const xpx = xDue(g.due_key) / 100 * trackW, w = label.length * 8 + 26;
        let lane = laneEnd.findIndex(e => e < xpx - w / 2 - 4);
        if (lane < 0) { lane = laneEnd.length; laneEnd.push(0); }
        laneEnd[lane] = xpx + w / 2;
        const worst = g.items.map(r => r.overdue ? "over" : r.state).sort((a, b) => rank[a] - rank[b])[0];
        const tip = g.items.map(r => `${r.key} ${r.title}（${{done: "済", doing: "進行中", todo: "未着手", empty: "項目なし", na: "対象外"}[r.state] || r.state}）${r.overdue ? " 期限切れ" : ""}${r.current ? " ← 現在地" : ""}`).join("\n");
        return {g, label, lane, worst, tip, cur: g.items.some(r => r.current)};
      });
      const lanes = Math.max(1, laneEnd.length), rowH = 12 + lanes * 26;
      const dots = chips.map(c => `<div class="tl-dot ${c.worst}${c.cur ? " cur" : ""}" style="left:${xDue(c.g.due_key)}%;top:${8 + c.lane * 26 + 11}px" title="${esc("期限 " + c.g.due + "\n" + c.tip)}">${icon[c.worst === "over" ? c.g.items[0].state : c.worst] || "○"} ${esc(c.label)}</div>`).join("");
      h += `<div class="tl-row" style="min-height:${rowH}px"><div class="tl-name" title="${esc(rs[0].project_name)}">${esc(rs[0].project_name)}</div><div class="tl-track" style="height:${rowH}px">${grid}${dots}</div></div>`;
    }
  }
  $("lgTimeline").innerHTML = h + "</div>";
}
function lgFillProjects() {
  const th = $("lgTheme").value, pv = $("lgProject").value;
  const ps = lg.data.projects.filter(p => !th || p.theme === th);
  $("lgProject").innerHTML = '<option value="">すべて</option>' + ps.map(p => `<option value="${esc(p.id)}">${esc(p.id)}${p.theme !== p.id ? "（" + esc(p.theme) + "）" : ""}</option>`).join("");
  $("lgProject").value = ps.some(p => p.id === pv) ? pv : "";
}
function lgTheme() {
  if ($("lgTheme").value) return $("lgTheme").value;
  const p = lg.data && lg.data.projects.find(x => x.id === $("lgProject").value);
  return p ? p.theme : null;
}
async function lgRenderMatrix() {
  const th = lgTheme();
  if (!th) { $("lgMatrix").innerHTML = '<p class="muted">テーマを選ぶと表が出ます</p>'; $("lgMsEdit").innerHTML = ""; return; }
  let mx;
  try { mx = await api("/api/ledger/matrix/" + encodeURIComponent(th)); } catch (e) { toast(e.message, "error"); return; }
  const lab = {done: "済", doing: "進行中", todo: "未着手", empty: "—", na: "対象外"};
  if (!mx.milestones.length) {
    $("lgMatrix").innerHTML = `<p class="muted">マイルストーンはまだありません</p>`;
  } else {
    let h = '<table class="list mx"><thead><tr><th>案件</th>' + mx.milestones.map(m => `<th title="${esc(m.criteria)}">${esc(m.key)}<br><span class="muted" style="font-weight:400">${esc(m.title)}</span>${m.due ? `<span class="due">期限 ${esc(m.due)}</span>` : ""}</th>`).join("") + "<th>段階なし</th></tr></thead><tbody>";
    for (const r of mx.rows) {
      h += `<tr><td><b>${esc(r.project.id)}</b></td>`;
      r.cells.forEach(c => {
        const sel = lg.cell && lg.cell.project === r.project.id && lg.cell.milestone === c.milestone;
        const open = Object.entries(c.open).map(([k, n]) => `${lg.data.labels.kind[k]}${n}`).join(" ");
        h += `<td class="cell ${c.state}${r.current === c.milestone ? " cur" : ""}${sel ? " sel" : ""}" data-p="${esc(r.project.id)}" data-m="${c.milestone}" title="${esc(open || "")}${c.note ? "\n" + esc(c.note) : ""}">
          ${lab[c.state] || c.state}${c.total ? ` ${c.done}/${c.total}` : ""}${c.blocked ? `<br><span class="blk">止まり ${c.blocked}</span>` : ""}
          ${c.due && (c.due !== (mx.milestones.find(m => m.id === c.milestone) || {}).due || c.overdue) ? `<span class="due${c.overdue ? " over" : ""}">${c.overdue ? "遅れ " : ""}${esc(c.due)}</span>` : ""}
          <button class="small mxm" style="padding:0 5px;margin-left:2px" title="この案件の期限・状態を決める">⋯</button></td>`;
      });
      h += `<td class="muted">${r.unassigned_open || ""}</td></tr>`;
    }
    $("lgMatrix").innerHTML = h + "</tbody></table>";
    $("lgMatrix").querySelectorAll("td.cell").forEach(td => {
      td.onclick = (e) => {
        const p = td.dataset.p, m = Number(td.dataset.m);
        if (e.target.classList.contains("mxm")) { e.stopPropagation(); return lgCellState(p, m, mx); }
        lg.cell = lg.cell && lg.cell.project === p && lg.cell.milestone === m ? null : {project: p, milestone: m};
        lgLoadItems();
      };
    });
  }
  // マイルストーンの編集（提案中のものは承認できる）
  const box = $("lgMsEdit"); box.innerHTML = "";
  for (const m of [...mx.milestones, ...mx.proposed]) {
    const row = document.createElement("div"); row.className = "row"; row.style.cssText = "gap:6px;margin-bottom:6px;flex-wrap:wrap";
    row.innerHTML = `<input value="${esc(m.key)}" style="width:6em" data-f="key"><input value="${esc(m.title)}" style="flex:1;min-width:10em" data-f="title">
      <input value="${esc(m.criteria)}" placeholder="達成条件" style="flex:2;min-width:12em" data-f="criteria"><input value="${esc(m.due || "")}" placeholder="期限（2026-12 など）" style="width:9em" data-f="due"><input type="number" value="${m.ord}" style="width:5em" data-f="ord" title="並び順">
      ${m.status === "proposed" ? `<span class="pill warn" title="${esc(m.source)}">提案</span><button class="small primary" data-a="approve">承認</button>` : ""}
      <button class="small" data-a="save">保存</button><button class="small danger" data-a="rm">削除</button>`;
    row.querySelectorAll("button").forEach(b => b.onclick = async () => {
      try {
        if (b.dataset.a === "rm") { if (!confirm(`${m.key} ${m.title} を削除しますか？（項目の割り当ては外れます）`)) return; await api(`/api/ledger/milestone/${m.id}`, {method: "DELETE"}); }
        else {
          const fields = {}; row.querySelectorAll("input").forEach(i => fields[i.dataset.f] = i.dataset.f === "ord" ? Number(i.value) : i.value);
          if (b.dataset.a === "approve") fields.status = "active";
          await api(`/api/ledger/milestone/${m.id}`, {method: "POST", body: JSON.stringify({fields})});
        }
        lgLoadItems();
      } catch (e) { toast(e.message, "error"); }
    });
    box.appendChild(row);
  }
  const add = document.createElement("div"); add.className = "row"; add.style.cssText = "gap:6px;flex-wrap:wrap";
  add.innerHTML = `<input placeholder="キー（例: M1）" style="width:8em"><input placeholder="名前（例: 要件確定）" style="flex:1;min-width:10em"><input placeholder="達成条件" style="flex:2;min-width:12em"><input placeholder="期限（2026-12 など）" style="width:9em"><button class="small primary">足す</button>`;
  add.querySelector("button").onclick = async () => {
    const [k, t, c, due] = [...add.querySelectorAll("input")].map(i => i.value.trim());
    if (!k || !t) return toast("キーと名前を入れてください", "warning");
    try { await api("/api/ledger/milestone", {method: "POST", body: JSON.stringify({theme: th, key: k, title: t, criteria: c, due})}); lgLoadItems(); }
    catch (e) { toast(e.message, "error"); }
  };
  box.appendChild(add);
}
async function lgCellState(project, milestone, mx) {
  const m = mx.milestones.find(x => x.id === milestone) || {};
  const row = mx.rows.find(r => r.project.id === project);
  const cell = row ? row.cells.find(c => c.milestone === milestone) : null;
  const due = prompt(`「${project}」の ${m.key} ${m.title} の期限（この案件だけ。例: 2026-12、2027、2030年3月）。空欄でテーマ共通の期限${m.due ? "（" + m.due + "）" : ""}に戻す`, cell && cell.due !== m.due ? cell.due : "");
  if (due === null) return;
  try {
    await api("/api/ledger/due", {method: "POST", body: JSON.stringify({project, milestone, due: due.trim()})});
    const v = prompt("状態も手で決めますか？ done（済み）/ doing / todo / na（対象外）。空欄なら自動判定のまま", cell && cell.note !== undefined && cell.state && ["done", "na"].includes(cell.state) ? cell.state : "");
    if (v !== null && v.trim()) await api("/api/ledger/cell", {method: "POST", body: JSON.stringify({project, milestone, state: v.trim()})});
    lgLoadItems();
  } catch (e) { toast(e.message, "error"); }
}
function lgRenderTable(items) {
  const L = lg.data.labels, tb = $("lgTable").querySelector("tbody"); tb.innerHTML = "";
  for (const it of items) {
    const tr = document.createElement("tr"); if (["done", "superseded", "dropped"].includes(it.status)) tr.className = "done";
    const sub = [];
    if (it.rationale) sub.push(`${it.kind === "premise" ? "前提の要点" : "根拠"}: ${esc(it.rationale)}`);
    if (it.needs) sub.push(`${it.kind === "unknown" ? "分かればよいこと" : it.kind === "discussion" ? "決める相手・内容" : "完了条件"}: ${esc(it.needs)}`);
    if (it.blocked_by.length) sub.push(`<span style="color:var(--bad)">止まっている ← ${it.blocked_by.map(esc).join("、")}</span>`);
    tr.dataset.uid = it.uid;
    const ball = it.ball_now && it.ball_now !== "none" ? `<br><span class="pill ${LG_BALL_PILL[it.ball_now] || ""}" title="次に誰が動く番か${it.ball ? "（手で設定）" : "（自動）"}" style="margin-top:4px">${esc(L.ball[it.ball_now])}</span>` : "";
    tr.innerHTML = `<td><span class="pill">${esc(L.kind[it.kind] || it.kind)}</span>${ball}</td>
      <td class="text">${esc(it.text)}${sub.length ? `<br><span class="muted" style="font-size:.8rem">${sub.join("<br>")}</span>` : ""}
        <br><span class="muted" style="font-size:.76rem"><a href="${lgItemLink(it.uid)}" class="lg-id">${esc(it.uid)}</a> · ${esc(it.project)}${it.topic_title ? " · " + esc(it.topic_title) : ""}</span></td>
      <td>${it.executable ? `<span class="pill ${it.executable === "auto" ? "ok" : it.executable === "blocked" ? "bad" : "warn"}">${esc(L.executable[it.executable])}</span>` : ""}</td>
      <td>${esc(L.domain[it.domain] || "")}</td><td>${esc(it.milestone_key || "")}</td>
      <td><select class="st">${Object.entries(L.status).map(([k, v]) => `<option value="${k}" ${k === it.status ? "selected" : ""}>${v}</option>`).join("")}</select></td>`;
    tr.querySelector(".st").onclick = (e) => e.stopPropagation();
    tr.querySelector(".lg-id").onclick = (e) => e.stopPropagation();
    tr.querySelector(".st").onchange = async (e) => {
      // 状態はそのまま切り替える（メモは聞かない。前のメモは残す。未完了に戻したら消す）
      const note = e.target.value === "open" ? "" : (it.status_note || "");
      try { await api(`/api/ledger/item/${encodeURIComponent(it.uid)}/status`, {method: "POST", body: JSON.stringify({status: e.target.value, note})}); lgLoadItems(); }
      catch (err) { toast(err.message, "error"); }
    };
    tr.style.cursor = "pointer";
    tr.onclick = () => lgToggleDetail(tr, it.uid);
    tb.appendChild(tr);
    if (lg.open === it.uid) lgToggleDetail(tr, it.uid, true);
  }
  $("lgCount").textContent = `${items.length} 件` + (lg.cell ? "（マイルストーンのセルで絞り込み中。もう一度押すと解除）" : "");
}
async function lgToggleDetail(tr, uid, force) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("lg-detail")) { next.remove(); if (!force) { lg.open = null; return; } }
  lg.open = uid;
  let it;
  try { it = await api("/api/ledger/item/" + encodeURIComponent(uid)); } catch (e) { toast(e.message, "error"); return; }
  const L = lg.data.labels;
  const d = document.createElement("tr"); d.className = "lg-detail";
  const sel = (f, opts, cur) => `<select data-f="${f}"><option value="">—</option>${Object.entries(opts).map(([k, v]) => `<option value="${k}" ${String(k) === String(cur ?? "") ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
  const msOpts = Object.fromEntries(it.milestones.map(m => [m.id, `${m.key} ${m.title}`]));
  const rel = (list, empty) => list.length ? list.map(x => `<li>${["done", "superseded"].includes(x.status) ? "✓" : "・"} <code>${esc(x.uid || x.dst)}</code> ${esc(x.text || "")}${x.source === "manual" || x.source === "ai" ? ` <button class="small" data-rm-src="${esc(x.src || "")}" data-rm="${esc(x.uid || x.dst)}" title="依存を外す">×</button>` : ""}</li>`).join("") : `<li class="muted">${empty}</li>`;
  d.innerHTML = `<td colspan="6">
    <div class="lg-dl">
      <div><label>種類</label>${sel("kind", L.kind, it.kind)}</div><div><label>実行</label>${sel("executable", L.executable, it.executable)}</div>
      <div><label>領域</label>${sel("domain", L.domain, it.domain)}</div><div><label>マイルストーン</label>${sel("milestone", msOpts, it.milestone)}</div>
      <div><label title="空なら担当・種類・依存から自動で決める">ボール（— は自動）</label>${sel("ball", {self: L.ball.self, ai: L.ball.ai, external: L.ball.external}, it.ball)}</div>
    </div>
    <label>根拠</label><textarea rows="2" data-f="rationale">${esc(it.rationale)}</textarea>
    <label>分かればよいこと／決める相手／完了条件</label><textarea rows="2" data-f="needs">${esc(it.needs)}</textarea>
    <div class="row" style="gap:6px;margin:6px 0"><button class="small primary" data-a="save">保存</button><a class="btn small" href="#/archive/${encodeURIComponent(it.meeting)}">会議を開く</a><button class="small" data-a="link" title="#/ledger/item/<ID> のリンク。AI に「この件」と渡すときに">リンクをコピー</button></div>
    <div class="two" style="gap:12px">
      <div><b>必要なもの（これがないとできない）</b><ul class="req">${rel(it.requires.map(x => ({...x, dst: x.uid})), "なし")}</ul>
        <div class="row" style="gap:6px"><input placeholder="必要な項目の uid か m:マイルストーンid" style="flex:1"><button class="small" data-a="dep">足す</button></div></div>
      <div><b>これがあるとできること</b><ul>${rel(it.enables, "なし")}</ul>
        ${it.unlocks.length ? `<p class="muted" style="font-size:.8rem;margin:0">これが済むと、依存がすべて揃うもの: ${it.unlocks.map(u => esc(u.uid)).join("、")}</p>` : ""}
        ${it.blockers.length ? `<p style="color:var(--bad);font-size:.8rem;margin:4px 0 0">できない理由: ${it.blockers.map(b => esc(b.dst) + " " + esc(b.text || "")).join("、")}</p>` : ""}</div>
    </div></td>`;
  d.querySelector('[data-a="save"]').onclick = async () => {
    const fields = {};
    d.querySelectorAll("[data-f]").forEach(el => { const v = el.value; fields[el.dataset.f] = el.dataset.f === "milestone" ? (v ? Number(v) : null) : v; });
    try { await api("/api/ledger/item/" + encodeURIComponent(uid), {method: "POST", body: JSON.stringify({fields})}); toast("保存しました", "ok"); lgLoadItems(); }
    catch (e) { toast(e.message, "error"); }
  };
  d.querySelector('[data-a="link"]').onclick = async () => {
    try { await lgCopy(`[${uid}] ${location.origin}${location.pathname}${lgItemLink(uid)}`); toast("リンクをコピーしました", "ok"); }
    catch (e) { toast(e.message, "error"); }
  };
  d.querySelector('[data-a="dep"]').onclick = async () => {
    const dst = d.querySelector('[data-a="dep"]').previousElementSibling.value.trim();
    if (!dst) return;
    try { await api("/api/ledger/dep", {method: "POST", body: JSON.stringify({src: uid, dst})}); lgLoadItems(); }
    catch (e) { toast(e.message, "error"); }
  };
  d.querySelectorAll(".req [data-rm]").forEach(b => b.onclick = async () => {
    try { await api("/api/ledger/dep", {method: "POST", body: JSON.stringify({src: uid, dst: b.dataset.rm, remove: true})}); lgLoadItems(); }
    catch (e) { toast(e.message, "error"); }
  });
  d.querySelectorAll("ul:not(.req) [data-rm]").forEach(b => b.remove());
  tr.after(d);
}
function lgShowJob(st) {
  const r = st.result;
  $("lgJob").textContent = st.running ? "分類中… " + (st.log.slice(-1)[0] || "")
    : st.error ? "失敗: " + st.error
    : r ? `分類 ${r.classified} 件・失敗 ${r.failed} 件・残り ${r.remaining} 件・マイルストーンの提案 ${r.proposed_milestones} 件` : "";
  $("lgClassify").disabled = !!st.running;
}
$("lgTheme").onchange = () => { lg.cell = null; lgFillProjects(); lgLoadItems(); };
$("lgProject").onchange = () => { lg.cell = null; lgLoadItems(); };
$("lgStatus").onchange = () => lgLoadItems();
let lgTextTimer = null;
$("lgText").addEventListener("input", () => { clearTimeout(lgTextTimer); lgTextTimer = setTimeout(lgLoadItems, 300); });
$("lgSync").onclick = async () => {
  try { const r = await api("/api/ledger/sync", {method: "POST"}); toast(`${r.meetings} 会議を取り込み、新しい項目 ${r.new_items} 件`, "ok"); lgLoadItems(true); }
  catch (e) { toast(e.message, "error"); }
};
$("lgThemeBtn").onclick = async () => {
  const pid = $("lgProject").value;
  if (!pid) return toast("先に案件を選んでください", "warning");
  const cur = lg.data.projects.find(p => p.id === pid);
  const t = prompt(`案件「${pid}」のテーマ（同じテーマの案件は共通マイルストーンで横に並べて比べます）`, cur ? cur.theme : pid);
  if (t === null) return;
  try { await api(`/api/ledger/project/${encodeURIComponent(pid)}/theme`, {method: "POST", body: JSON.stringify({theme: t})}); lgLoadItems(true); }
  catch (e) { toast(e.message, "error"); }
};
$("lgClassify").onclick = async () => {
  if (!$("lgCfg").value) return toast("議長を借りる設定ファイルがありません", "warning");
  try {
    await api("/api/ledger/classify", {method: "POST", body: JSON.stringify({config_path: $("lgCfg").value, project: $("lgProject").value || null})});
    clearInterval(lg.jobTimer);
    lg.jobTimer = setInterval(async () => {
      try {
        const st = await api("/api/ledger/classify"); lgShowJob(st);
        if (!st.running) { clearInterval(lg.jobTimer); if (route === "ledger") lgLoadItems(true); }
      } catch (e) { clearInterval(lg.jobTimer); }
    }, 2000);
  } catch (e) { toast(e.message, "error"); }
};
