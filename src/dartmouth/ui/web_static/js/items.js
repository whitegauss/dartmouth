"use strict";
/* ================================================================ 決定・未決・ToDo */
let items = [], selected = new Set();
async function loadItems() {
  document.title = "Dartmouth: 決定・未決・ToDo"; $("title").textContent = "決定・未決・ToDo";
  try { items = (await api("/api/items")).items; } catch (e) { toast(e.message, "error"); return; }
  const owners = ["本人", "外部待ち", "AI"];
  const meetings = [...new Map(items.filter(i => !i.archived).map(i => [i.meeting, i.meeting_name])).entries()];
  const keep = (id, opts) => { const v = $(id).value; $(id).innerHTML = '<option value="">すべて</option>' + opts; $(id).value = v; };
  keep("fOwner", owners.map(o => `<option>${esc(o)}</option>`).join(""));
  keep("fMeeting", meetings.map(([id, name]) => `<option value="${esc(id)}">${esc(name)}（${esc(id)}）</option>`).join(""));
  renderItems();
}
function renderItems() {
  const k = $("fKind").value, st = $("fStatus").value, ow = $("fOwner").value, mt = $("fMeeting").value, q = $("fText").value.trim().toLowerCase();
  const showArch = $("fArchived").checked;
  const rows = items.filter(i => (showArch || !i.archived) && (!k || i.kind === k) && (!st || (st === "done") === i.done) && (!ow || (ownerLabel(i.owner) || "AI") === ow) && (!mt || i.meeting === mt) && (!q || i.text.toLowerCase().includes(q)));
  const tb = $("itemsTable").querySelector("tbody"); tb.innerHTML = "";
  for (const i of rows) {
    const key = i.meeting + ":" + i.id;
    const tr = document.createElement("tr"); if (i.done) tr.className = "done";
    const doneLabel = i.kind === "todo" ? "完了" : i.kind === "question" ? "解決" : "取り下げ";
    tr.innerHTML = `<td><input type="checkbox" class="sel" title="議題にする" ${selected.has(key) ? "checked" : ""} ${i.kind === "decision" ? "disabled" : ""}></td>
      <td><span class="pill">${esc(KIND_LABEL[i.kind])}</span><br><span class="muted">${esc(i.id)}</span></td>
      <td class="text">${esc(i.text)}${i.topic_title ? `<br><span class="muted">議題: ${esc(i.topic_title)}</span>` : ""}${i.note ? `<br><span class="muted">メモ: ${esc(i.note)}</span>` : ""}</td>
      <td>${esc(ownerLabel(i.owner) || (i.kind === "decision" ? "" : "AI"))}</td>
      <td><a href="#/archive/${encodeURIComponent(i.meeting)}">${esc(i.meeting_name)}</a><br><span class="muted">${esc(i.meeting_date)}</span></td>
      <td><label style="margin:0;display:flex;gap:4px;align-items:center;color:var(--fg)"><input type="checkbox" class="done" ${i.done ? "checked" : ""}>${doneLabel}</label></td>`;
    tr.querySelector(".sel").onchange = (e) => { e.target.checked ? selected.add(key) : selected.delete(key); updateSel(); };
    tr.querySelector(".done").onchange = async (e) => {
      let note = i.note || "";
      if (e.target.checked) { const n = prompt(`${doneLabel}にします。メモ（任意）`, note); if (n === null) { e.target.checked = false; return; } note = n; }
      try { await api("/api/items/status", {method: "POST", body: JSON.stringify({meeting: i.meeting, id: i.id, done: e.target.checked, note})}); i.done = e.target.checked; i.note = note; renderItems(); }
      catch (err) { toast(err.message, "error"); e.target.checked = !e.target.checked; }
    };
    tb.appendChild(tr);
  }
  const open = items.filter(i => !i.done && i.kind !== "decision" && (showArch || !i.archived)).length;
  $("itemCount").textContent = `${rows.length} 件を表示（未完了・未解決の ToDo と未決事項: ${open} 件）`;
  updateSel();
}
function updateSel() { $("itemsStart").disabled = selected.size === 0; $("itemsStart").textContent = selected.size ? `選んだ ${selected.size} 件を議題にして会議を始める` : "選んだ項目を議題にして会議を始める"; }
["fKind", "fStatus", "fOwner", "fMeeting", "fArchived"].forEach(id => $(id).onchange = renderItems);
$("fText").addEventListener("input", renderItems);
$("itemsStart").onclick = () => {
  const chosen = items.filter(i => selected.has(i.meeting + ":" + i.id));
  pendingStart = {
    topics: chosen.map(i => ({title: i.text, premise: `会議「${i.meeting_name}」の ${i.id}（${KIND_LABEL[i.kind]}）から。`, from: i.meeting + ":" + i.id})),
    from_meetings: [...new Set(chosen.map(i => i.meeting))],
  };
  selected = new Set();
  if (lastState && lastState.mode === "meeting" && lastState.meeting.running) { toast("進行中の会議があります。閉じてから始めてください", "warning"); return; }
  location.hash = "#/";
};
$("itemsExportCopy").onclick = async () => {
  const b = $("itemsExportCopy"); b.disabled = true;
  try {
    const r = await fetch("/api/export.md");
    if (!r.ok) throw new Error(`書き出せません（${r.status}）`);
    const t = await r.text();
    await copyText(t);
    toast(`全アイテムをコピーしました（${nf.format(t.length)} 字）`, "ok");
  } catch (e) { toast(e.message, "error"); } finally { b.disabled = false; }
};
