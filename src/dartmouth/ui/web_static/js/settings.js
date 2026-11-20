"use strict";
/* ================================================================ 設定 */
async function loadSettings() {
  document.title = "Dartmouth: 設定"; $("title").textContent = "設定";
  try {
    const {files} = await api("/api/settings/files");
    const groups = {};
    files.forEach(f => (groups[f.group] = groups[f.group] || []).push(f));
    $("fileList").innerHTML = files.length ? "" : '<p class="muted">編集できるファイルがありません（dm web -c で設定ファイルを渡すと出ます）</p>';
    for (const [g, fs] of Object.entries(groups)) {
      $("fileList").insertAdjacentHTML("beforeend", `<h3>${esc(g)}</h3>`);
      fs.forEach(f => { const a = document.createElement("a"); a.href = "javascript:void 0"; a.textContent = f.name; a.title = f.path; a.dataset.path = f.path; a.onclick = () => openSetting(f.path); $("fileList").appendChild(a); });
    }
  } catch (e) { toast(e.message, "error"); }
  try {
    const m = await api("/api/models");
    const block = (title, list) => `<h3>${title}</h3>` + (list.length ? list.map(x => `<div><code title="クリックでコピー" data-copy="${esc(x.id)}">${esc(x.id)}</code>${x.label !== x.id ? ` <span class="muted">${esc(x.label)}</span>` : ""}</div>`).join("") : '<p class="muted">取得できません</p>');
    $("modelList").classList.remove("muted");
    $("modelList").innerHTML = block("claude_code（model:）", m.claude_code) + block("antigravity_cli（model:）", m.antigravity_cli)
      + '<p class="muted" style="margin-top:8px">議論は軽いモデルのリレー、議長だけ強いモデル、がおすすめです。</p>';
    $("modelList").querySelectorAll("[data-copy]").forEach(c => c.onclick = () => { navigator.clipboard && navigator.clipboard.writeText(c.dataset.copy); toast(c.dataset.copy + " をコピーしました"); });
  } catch (e) { $("modelList").textContent = "取得できません"; }
}
async function openSetting(path) {
  if (setDirty && !confirm("保存していない変更があります。破棄しますか？")) return;
  try {
    const f = await api("/api/settings/file?path=" + encodeURIComponent(path));
    setFile = f; setDirty = false;
    $("setName").textContent = f.name; $("setText").value = f.text; $("setText").disabled = false; $("setSave").disabled = true; $("setState").textContent = "";
    $("setHelp").textContent = f.kind === "config"
      ? "設定ファイル（YAML）。保存前に形式を検証します。登録されていないアダプタの追加と command の変更は、安全のため画面からはできません。"
      : "プロンプト（Markdown）。$display_name などの $変数 が使えます。";
    document.querySelectorAll(".filelist a").forEach(a => a.classList.toggle("on", a.dataset.path === path));
  } catch (e) { toast(e.message, "error"); }
}
$("setText").addEventListener("input", () => { setDirty = true; $("setSave").disabled = false; $("setState").textContent = "未保存"; });
$("setSave").onclick = async () => {
  try {
    const r = await api("/api/settings/file", {method: "PUT", body: JSON.stringify({path: setFile.path, text: $("setText").value})});
    setDirty = false; $("setSave").disabled = true; $("setState").textContent = "保存しました"; toast(r.message);
  } catch (e) { toast(e.message, "error"); $("setState").textContent = "保存できません"; }
};
window.addEventListener("beforeunload", (e) => { if (setDirty) { e.preventDefault(); e.returnValue = ""; } });
