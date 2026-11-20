"use strict";
/* ================================================================ 過去の会議 */
async function loadArchive() {
  $("title").textContent = "過去の会議"; document.title = "Dartmouth: 過去の会議";
  try {
    const {meetings} = await api("/api/meetings");
    $("archiveList").classList.remove("muted");
    $("archiveList").innerHTML = meetings.length ? "" : '<span class="muted">まだありません</span>';
    const active = meetings.filter(m => !m.archived), old = meetings.filter(m => m.archived);
    active.forEach(m => $("archiveList").appendChild(withArchiveToggle(meetingItem(m, true), m)));
    if (old.length) {
      const det = document.createElement("details"); det.innerHTML = `<summary class="muted" style="cursor:pointer;margin:12px 0 4px">アーカイブした会議（${old.length}）— 記録と集計は残っています</summary>`;
      old.forEach(m => det.appendChild(withArchiveToggle(meetingItem(m, false), m)));
      $("archiveList").appendChild(det);
    }
  } catch (e) { $("archiveList").textContent = "読み込めません: " + e.message; }
}
function withArchiveToggle(el, m) {
  const b = document.createElement("button"); b.className = "small"; b.textContent = m.archived ? "アーカイブから戻す" : "アーカイブ";
  b.onclick = async () => {
    const reason = m.archived ? "" : (prompt("アーカイブする理由（任意）", "") ?? null);
    if (reason === null) return;
    try { await api(`/api/archive/${encodeURIComponent(m.id)}/archived`, {method: "POST", body: JSON.stringify({archived: !m.archived, reason})}); loadArchive(); }
    catch (e) { toast(e.message, "error"); }
  };
  const row = el.querySelector(".row") || el;
  row.appendChild(b);
  if (!m.running && !row.querySelector(".danger")) row.appendChild(deleteBtn(m, loadArchive));
  return el;
}
async function doSearch() {
  const q = $("q").value.trim();
  if (q.length < 2) { $("hits").innerHTML = '<p class="muted">2文字以上で検索してください</p>'; return; }
  const {hits} = await api("/api/search?q=" + encodeURIComponent(q));
  const hl = (s) => esc(s).replace(new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/[&<>"']/g, c => esc(c)), "gi"), (x) => `<mark>${x}</mark>`);
  $("hits").innerHTML = hits.length ? `<p class="muted">${hits.length}件${hits.length >= 200 ? "（上限）" : ""}</p>` : '<p class="muted">見つかりませんでした</p>';
  for (const h of hits) {
    const el = document.createElement("div"); el.className = "meeting-item";
    el.innerHTML = `<div class="grow"><a href="#/archive/${encodeURIComponent(h.meeting)}?seq=${h.seq}"><b>${esc(h.meeting_name)}</b></a>
      <span class="muted">${h.topic ? "議題" + h.topic + " · " : ""}${esc(h.speaker_name)} · ${esc((h.ts || "").slice(0, 16).replace("T", " "))}</span><br>${hl(h.snippet)}</div>`;
    $("hits").appendChild(el);
  }
}
$("qBtn").onclick = doSearch;
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) doSearch(); });

let avData = null;
async function loadArchiveView(arg) {
  const [id, query] = arg.split("?");
  const seqWanted = query && new URLSearchParams(query).get("seq");
  $("avTitle").textContent = "読み込み中…"; $("avMeta").textContent = ""; $("avActions").innerHTML = "";
  let d;
  try { d = await api("/api/archive/" + encodeURIComponent(id)); } catch (e) { $("avTitle").textContent = "読み込めません: " + e.message; return; }
  avData = d;
  const s = d.summary;
  document.title = "Dartmouth: " + s.name;
  $("avTitle").textContent = s.name;
  const parts = Object.entries(d.participants).map(([n, p]) => `${p.display_name}${p.model ? "（" + p.model + "）" : ""}`).join("、");
  $("avMeta").textContent = `${s.id} · ${s.state} · ${(s.started || "").slice(0, 16).replace("T", " ")} 〜 ${(s.updated || "").slice(11, 16)} · 議題 ${s.topics.length} · 参加者: ${parts}`;
  const act = $("avActions");
  const dl = (kind, label) => { const a = document.createElement("a"); a.className = "btn small"; a.href = `/api/archive/${encodeURIComponent(id)}/download/${kind}`; a.textContent = label; act.appendChild(a); };
  dl("minutes", "議事録 .md"); if (d.draft_html) dl("draft", "下書き .md"); dl("transcript", "記録 .jsonl");
  if (s.resumable && !d.live) { const b = document.createElement("button"); b.className = "small"; b.textContent = "再開"; b.onclick = () => resume(id); act.appendChild(b); }
  const f = document.createElement("button"); f.className = "small"; f.textContent = "未決事項から始める"; f.onclick = () => pickFrom(id); act.appendChild(f);
  if (s.extendable && !d.live) { const ex = document.createElement("div"); ex.style.flexBasis = "100%"; act.appendChild(ex); extendForm(ex, id); }
  $("av-minutes").innerHTML = d.minutes_html;
  $("av-draft").innerHTML = d.draft_html || '<p class="muted">まだ書き出されていません（会議の終了時に書き出されます）</p>';
  // 発言ログ
  // 色は表示名の並び（使用量の画面と同じ規則）で人に固定する
  const byName = Object.entries(d.participants).map(([n, p]) => [n, p.display_name]).sort((a, b) => a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0);
  const pcolors = {}; byName.forEach(([n], i) => pcolors[n] = `var(--s${(i % 8) + 1})`);
  $("avLog").innerHTML = "";
  d.messages.forEach(m => renderMsg($("avLog"), m, pcolors, "av-"));
  $("avFilter").value = ""; $("avCount").textContent = `${d.messages.filter(m => m.kind === "message").length} 件の発言`;
  $("av-usage").innerHTML = usageTableHTML(d.usage.participants, true) + `<p class="muted">合計 ${nf.format(d.usage.totals.calls)} 回 · ${fmtSec(d.usage.totals.elapsed_s)} · ${compact(d.usage.totals.tokens)} トークン${d.usage.totals.cost_usd ? " · $" + d.usage.totals.cost_usd.toFixed(2) : ""}</p>`;
  setAvTab(seqWanted ? "log" : "minutes");
  if (seqWanted) {
    const el = document.getElementById("av-seq-" + seqWanted);
    if (el) { el.closest("details").open = true; el.classList.add("hit"); el.scrollIntoView({block: "center"}); }
  }
}
function setAvTab(t) {
  ["minutes", "log", "draft", "usage"].forEach(x => $("av-" + x).classList.toggle("hidden", x !== t));
  document.querySelectorAll("#avTabs button").forEach(b => b.classList.toggle("on", b.dataset.av === t));
}
document.querySelectorAll("#avTabs button").forEach(b => b.onclick = () => setAvTab(b.dataset.av));
$("avFilter").addEventListener("input", () => {
  const q = $("avFilter").value.trim().toLowerCase();
  let n = 0;
  $("avLog").querySelectorAll(".msg, .sys").forEach(el => {
    const hit = !q || (el.dataset.text || "").toLowerCase().includes(q);
    el.classList.toggle("hidden", !hit); if (hit && el.classList.contains("msg")) n++;
  });
  if (q) $("avLog").querySelectorAll("details.topic").forEach(dt => dt.open = !!dt.querySelector(".msg:not(.hidden)"));
  $("avCount").textContent = q ? `${n} 件が一致` : `${avData.messages.filter(m => m.kind === "message").length} 件の発言`;
});
