"use strict";
/* ================================================================ 使用量 */
function tipFor(container) {
  let tip = container.querySelector(".tip");
  if (!tip) { tip = document.createElement("div"); tip.className = "tip hidden"; container.appendChild(tip); }
  return {
    show(html, ev) {
      tip.innerHTML = html; tip.classList.remove("hidden");
      const r = container.getBoundingClientRect(); let x = ev.clientX - r.left + 12, y = ev.clientY - r.top + 12;
      if (x + tip.offsetWidth > r.width) x = Math.max(0, r.width - tip.offsetWidth);
      tip.style.left = x + "px"; tip.style.top = y + "px";
    },
    hide() { tip.classList.add("hidden"); },
  };
}
const niceMax = (v) => { if (v <= 0) return 1; const p = Math.pow(10, Math.floor(Math.log10(v))); for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * p) return m * p; return 10 * p; };
function hbar(container, rows, {fmt, tipFmt}) {
  // 1系列の横棒。色は1色（系列が1つなので凡例は不要）、値は棒の先に
  container.innerHTML = "";
  if (!rows.length) { container.innerHTML = '<p class="muted">データがありません</p>'; return; }
  // 実際の幅で描く（viewBox を固定すると文字がカードの幅に合わせて伸び縮みする）
  const W = Math.max(280, container.clientWidth || 560), labelW = Math.min(140, W * 0.32), rowH = 34, bar = 18, top = 6, H = top + rows.length * rowH + 22;
  const fit = (t) => { const max = Math.floor(labelW / 12); return t.length > max ? t.slice(0, max - 1) + "…" : t; };
  const max = niceMax(Math.max(...rows.map(r => r.value)));
  const x = (v) => labelW + (W - labelW - 70) * (v / max);
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img">`;
  for (let i = 0; i <= 4; i++) { const v = max * i / 4, gx = x(v); svg += `<line class="gridline" x1="${gx}" x2="${gx}" y1="${top}" y2="${H - 20}"/><text x="${gx}" y="${H - 6}" text-anchor="middle">${fmt(v)}</text>`; }
  rows.forEach((r, i) => {
    const y = top + i * rowH + (rowH - bar) / 2, w = Math.max(0, x(r.value) - labelW), rad = Math.min(4, w);
    svg += `<text x="${labelW - 8}" y="${y + bar / 2 + 4}" text-anchor="end"><title>${esc(r.label)}</title>${esc(fit(r.label))}</text>`;
    svg += `<path data-i="${i}" fill="var(--s1)" d="M${labelW},${y} h${w - rad} a${rad},${rad} 0 0 1 ${rad},${rad} v${bar - 2 * rad} a${rad},${rad} 0 0 1 -${rad},${rad} h-${w - rad} z"/>`;
    svg += `<rect data-i="${i}" x="${labelW}" y="${top + i * rowH}" width="${W - labelW}" height="${rowH}" fill="transparent"/>`;
    svg += `<text class="val" x="${labelW + w + 6}" y="${y + bar / 2 + 4}">${fmt(r.value)}</text>`;
  });
  container.insertAdjacentHTML("beforeend", svg.replace("<svg ", `<svg width="${W}" height="${H}" `) + "</svg>");
  const tip = tipFor(container);
  container.querySelectorAll("[data-i]").forEach(el => {
    el.addEventListener("mousemove", (ev) => tip.show(tipFmt(rows[+el.dataset.i]), ev));
    el.addEventListener("mouseleave", () => tip.hide());
  });
}
function stackedColumns(container, legendEl, cols, keys, colorOf, labelOf) {
  container.innerHTML = ""; legendEl.innerHTML = "";
  if (!cols.length) { container.innerHTML = '<p class="muted">データがありません</p>'; return; }
  legendEl.innerHTML = keys.map(k => `<span style="--c:${colorOf(k)}">${esc(labelOf(k))}</span>`).join("");
  const W = Math.max(container.clientWidth || 560, cols.length * 56 + 70), H = 260, left = 52, bottom = 46, top = 10, colW = Math.min(24, (W - left) / cols.length * 0.6);
  const max = niceMax(Math.max(...cols.map(c => keys.reduce((a, k) => a + (c.parts[k] || 0), 0))));
  const y = (v) => H - bottom - (H - bottom - top) * (v / max);
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img">`;
  for (let i = 0; i <= 4; i++) { const v = max * i / 4; svg += `<line class="gridline" x1="${left}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text x="${left - 6}" y="${y(v) + 4}" text-anchor="end">${compact(v)}</text>`; }
  const step = (W - left) / cols.length;
  cols.forEach((c, i) => {
    const cx = left + step * i + step / 2, x0 = cx - colW / 2;
    let acc = 0;
    const present = keys.filter(k => c.parts[k] > 0);
    present.forEach((k, j) => {
      const v = c.parts[k], y1 = y(acc + v), y0 = y(acc), h = Math.max(0, y0 - y1 - (j > 0 ? 2 : 0)); // 2px の隙間
      const isTop = j === present.length - 1, rad = isTop ? Math.min(4, h, colW / 2) : 0;
      svg += `<path data-c="${i}" fill="${colorOf(k)}" d="M${x0},${y0 - (j > 0 ? 2 : 0)} v-${h - rad} a${rad},${rad} 0 0 1 ${rad},-${rad} h${colW - 2 * rad} a${rad},${rad} 0 0 1 ${rad},${rad} v${h - rad} z"/>`;
      acc += v;
    });
    svg += `<rect data-c="${i}" x="${left + step * i}" y="${top}" width="${step}" height="${H - bottom - top}" fill="transparent"/>`;
    svg += `<text x="${cx}" y="${H - bottom + 16}" text-anchor="middle">${esc(c.label)}</text><text x="${cx}" y="${H - bottom + 30}" text-anchor="middle">${esc(c.sub || "")}</text>`;
  });
  container.insertAdjacentHTML("beforeend", svg.replace("<svg ", `<svg width="${W}" height="${H}" style="width:${W}px" `) + "</svg>");
  container.style.overflowX = "auto";
  const tip = tipFor(container);
  container.querySelectorAll("[data-c]").forEach(el => {
    el.addEventListener("mousemove", (ev) => {
      const c = cols[+el.dataset.c];
      const total = keys.reduce((a, k) => a + (c.parts[k] || 0), 0);
      tip.show(`<b>${esc(c.title)}</b><br>` + keys.filter(k => c.parts[k]).map(k => `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${colorOf(k)};margin-right:6px"></span>${esc(labelOf(k))}: ${nf.format(c.parts[k])}`).join("<br>") + `<br>合計: ${nf.format(total)}`, ev);
    });
    el.addEventListener("mouseleave", () => tip.hide());
  });
}
async function loadUsage() {
  document.title = "Dartmouth: 使用量"; $("title").textContent = "使用量";
  let u; try { u = await api("/api/usage"); } catch (e) { toast(e.message, "error"); return; }
  const t = u.totals, failRate = t.calls ? (t.timeout + t.failed) / t.calls : 0;
  const kpi = (label, value) => `<div class="kpi"><div class="label">${label}</div><div class="value">${value}</div></div>`;
  const w = u.window || {};
  $("kpis").innerHTML = kpi("直近5時間の Claude", compact(w.claude_tokens || 0) + `<span class="muted" style="font-size:.8rem;font-weight:500"> トークン・${w.claude_calls || 0}回${w.limit_hits ? "・枠切れ " + w.limit_hits : ""}</span>`)
    + kpi("会議", nf.format(t.meetings)) + kpi("呼び出し", compact(t.calls)) + kpi("トークン", compact(t.tokens)) + kpi("所要時間の合計", fmtSec(t.elapsed_s))
    + kpi("失敗・タイムアウト", (failRate * 100).toFixed(1) + "%") + (t.cost_usd ? kpi("費用（取れた分）", "$" + t.cost_usd.toFixed(2)) : "");
  const dup = new Set(u.participants.map(p => p.display_name).filter((n, i, a) => a.indexOf(n) !== i));
  const label = (p) => p.display_name + (dup.has(p.display_name) && p.model ? "・" + p.model : "");
  const full = (p) => p.display_name + (p.model ? "（" + p.model + "）" : "");
  hbar($("chTokens"), u.participants.map(p => ({label: label(p), value: p.tokens, p})), {fmt: compact,
    tipFmt: (r) => `<b>${esc(full(r.p))}</b><br>トークン: ${nf.format(r.p.tokens)}<br>呼び出し: ${nf.format(r.p.calls)}（${r.p.meetings}会議）`});
  hbar($("chLatency"), [...u.participants].sort((a, b) => b.avg_s - a.avg_s).map(p => ({label: label(p), value: p.avg_s, p})), {fmt: (v) => (Math.round(v * 10) / 10) + "s",
    tipFmt: (r) => `<b>${esc(full(r.p))}</b><br>平均: ${r.p.avg_s}秒 / 回<br>失敗率: ${(r.p.fail_rate * 100).toFixed(1)}%`});
  // 会議ごとの積み上げ。色は参加者（表示名）に固定で割り当てる（順位では塗らない）
  const names = [...new Set(u.meetings.flatMap(m => m.participants.map(p => p.display_name)))].sort();
  const top = names.slice(0, 7), other = names.length > 7;
  const keys = other ? [...top, "その他"] : top;
  const colorOf = (k) => k === "その他" ? "var(--muted)" : `var(--s${top.indexOf(k) + 1})`;
  const cols = u.meetings.map(m => {
    const parts = {};
    m.participants.forEach(p => { const k = top.includes(p.display_name) ? p.display_name : "その他"; parts[k] = (parts[k] || 0) + p.tokens; });
    return {label: (m.started || "").slice(5, 10), sub: m.name.length > 10 ? m.name.slice(0, 9) + "…" : m.name, title: `${m.name}（${m.id}）`, parts};
  });
  stackedColumns($("chMeetings"), $("lgMeetings"), cols, keys, colorOf, (k) => k);
  $("usageTable").innerHTML = usageTableHTML(u.participants, false).replace(/^<table class="list">|<\/table>$/g, "");
  const seats = u.seats || [];
  const scoreCell = (avg, n) => avg == null ? "—" : `<span class="pill ${avg >= 4 ? "ok" : avg < 3 ? "bad" : ""}">${avg}</span>${n != null ? ` <span class="muted">(${n})</span>` : ""}`;
  $("seatTable").innerHTML = seats.length
    ? `<tr><th>ペルソナ</th><th>経路</th><th>モデル</th><th>座った</th><th>採点</th><th>Neon</th><th>Opus</th><th>Argon</th><th>採点数</th><th>トークン/回</th></tr>` + seats.map(h => `<tr><td>${esc(h.name)}</td><td>${esc(h.backend)}</td><td>${esc(h.model || "")}</td>
      <td class="num">${h.seated}</td><td class="num">${scoreCell(h.avg_score)}</td><td class="num">${scoreCell(h.neon_avg_score, h.neon_avg_score == null ? null : h.neon_rated)}</td><td class="num">${scoreCell(h.opus_avg_score, h.opus_avg_score == null ? null : h.opus_rated)}</td><td class="num">${scoreCell(h.argon_avg_score, h.argon_avg_score == null ? null : h.argon_rated)}</td><td class="num">${h.rated}</td><td class="num">${h.tokens_per_call == null ? "—" : nf.format(h.tokens_per_call)}</td></tr>`).join("")
    : '<tr><td class="muted">まだありません（議長が席を決める会議を回すと貯まります）</td></tr>';
}

let resizeTimer = null;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (route === "usage") loadUsage(); }, 300); });
