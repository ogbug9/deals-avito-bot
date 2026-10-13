"use strict";
// Экран сравнения: колонки — лоты, строки — критерии. Лучшее подсвечено, худшее приглушено.
(function () {
const h = G.h;
const C = G.compare = {ref: null, data: null, models: []};
try { C.ref = localStorage.getItem("gpuflip-cmp-ref") || null; } catch (e) {}

function table(data, onlyDiff, opt) {
  const rows = data.rows.filter(r => !onlyDiff || r.differs || r.key === "photo");
  const head = h("tr", null, h("th", {text: opt.first}), opt.cols.map((col, i) => h("th", {class: opt.isRef(i) ? "ref" : ""}, col)));
  const body = rows.map(r => h("tr", null, h("td", {text: r.label, title: r.direction > 0 ? "Больше — лучше" : r.direction < 0 ? "Меньше — лучше" : "Не сравнивается"}),
    r.cells.map((c, i) => {
      const td = h("td", {class: [c.mark, opt.isRef(i) ? "ref" : ""].join(" ").trim(), title: c.mark === "best" ? "Лучшее значение в строке" : c.mark === "worst" ? "Худшее значение в строке" : null});
      if (r.key === "photo") td.append(G.pic(c.raw, "cmpimg")); else td.append(c.text);
      if (c.diff) td.append(h("span", {class: "diff", text: "к эталону: " + c.diff}));
      return td;
    })));
  return h("table", {class: "cmp"}, h("thead", null, head), h("tbody", null, body));
}

C.load = async function () {
  $("c-csv").href = "/api/compare.csv?ids=" + G.cmp.join(",") + (C.ref ? "&ref=" + C.ref : "") + ($("c-diff").checked ? "&diff=1" : "");
  if (!G.cmp.length) {
    C.data = null;
    $("c-table").replaceChildren(h("div", {class: "empty", text: "В сравнении пусто. Отметьте лоты галочкой «Сравнить» в списке или клавишей c."}));
    return;
  }
  let d;
  try { d = await G.api("/api/compare?ids=" + G.cmp.join(",") + (C.ref ? "&ref=" + C.ref : "")); }
  catch (e) { $("c-table").replaceChildren(h("div", {class: "note", text: e.message})); return; }
  C.data = d;
  if (d.lots.length < G.cmp.length) { G.cmp = d.lots.map(l => l.id); G.cmpChanged(); }      // пропавшие лоты убираем
  const cols = d.lots.map(lot => {
    const u = G.safeUrl(lot.url);
    return [h("div", {style: "font-weight:600;overflow-wrap:anywhere", text: lot.title + (lot.status !== "active" ? " (ушло)" : "")}),
      h("div", {class: "chips", style: "margin-top:4px"},
        h("button", {class: "btn small" + (d.ref === lot.id ? " on" : ""), type: "button", text: d.ref === lot.id ? "Эталон ✓" : "Эталон", title: "Сравнивать остальные лоты с этим",
          onclick: () => { C.ref = d.ref === lot.id ? null : lot.id; try { localStorage.setItem("gpuflip-cmp-ref", C.ref || ""); } catch (e) {} C.load(); }}),
        u ? h("a", {class: "btn small", href: u, target: "_blank", rel: "noopener noreferrer", text: "Авито"}) : null,
        h("button", {class: "btn small", type: "button", text: "Убрать", onclick: () => { G.cmpToggle(lot.id); }}))];
  });
  $("c-table").replaceChildren(table(d, $("c-diff").checked, {first: "Критерий", cols, isRef: i => d.lots[i].id === d.ref}));
  if ($("c-diff").checked && !d.rows.some(r => r.differs)) $("c-table").append(h("div", {class: "small", text: "Отличий нет: лоты совпадают по всем критериям."}));
};
C.loadModels = async function () {
  $("cm-panel").hidden = !C.models.length;
  if (!C.models.length) return;
  try {
    const d = await G.api("/api/models?keys=" + encodeURIComponent(C.models.join("|")));
    $("cm-note").textContent = d.error || (d.approx ? "Индекс производительности ориентировочный (" + d.baseline + ") — проверьте words\\analogs.json." : d.baseline);
    $("cm-table").replaceChildren(table(d, false, {first: "Показатель", isRef: () => false,
      cols: d.models.map(m => [h("div", {style: "font-weight:600", text: m.key}), h("div", {class: "small", text: m.group ? "группа: " + m.group : "нет в таблице аналогов"})])}));
  } catch (e) { $("cm-table").replaceChildren(h("div", {class: "note", text: e.message})); }
};
C.loadSets = async function (list) {
  list = list || await G.api("/api/compare_sets");
  C.sets = list;
  $("c-sets").replaceChildren(h("option", {value: "", text: list.length ? "Сохранённые наборы…" : "Сохранённых наборов нет"}),
    ...list.map(s => h("option", {value: s.id, text: s.name + " (" + s.ids.length + ")"})),
    ...(list.length ? [h("option", {value: "del", text: "— удалить набор…"})] : []));
};
$("c-sets").onchange = async () => {
  const v = $("c-sets").value; $("c-sets").value = "";
  if (v === "del") {
    const name = prompt("Название набора, который удалить:\n" + C.sets.map(s => "• " + s.name).join("\n"));
    const s = C.sets.find(x => x.name === name);
    if (s) C.loadSets(await G.api("/api/compare_sets", {delete: s.id}));
    return;
  }
  const s = C.sets.find(x => String(x.id) === v);
  if (s) { G.cmp = s.ids.slice(0, 6); C.ref = s.ref || null; G.cmpChanged(); }
};
$("c-save").onclick = async () => {
  if (G.cmp.length < 2) { alert("Для набора нужно хотя бы два лота."); return; }
  const name = prompt("Название набора сравнения:", "Набор от " + new Date().toLocaleDateString("ru-RU"));
  if (name) try { C.loadSets(await G.api("/api/compare_sets", {name, ids: G.cmp, ref: C.ref})); } catch (e) { alert(e.message); }
};
$("c-diff").onchange = C.load;
$("c-clear").onclick = () => { G.cmp = []; C.ref = null; G.cmpChanged(); };
$("cm-clear").onclick = () => { C.models = []; C.loadModels(); if (G.hooks.modelsChanged) G.hooks.modelsChanged(); };

// Экспорт в картинку: таблица рисуется на холсте (без фото — чужие картинки холст сохранять не даёт).
$("c-png").onclick = () => {
  const d = C.data;
  if (!d || !d.lots.length) { alert("Сравнивать нечего."); return; }
  const rows = d.rows.filter(r => r.key !== "photo" && (!$("c-diff").checked || r.differs));
  const css = getComputedStyle(document.documentElement), v = n => css.getPropertyValue(n).trim();
  const W0 = 230, W = 210, RH = 30, HH = 56, pad = 10, width = W0 + W * d.lots.length, height = HH + RH * rows.length + 12;
  const cv = document.createElement("canvas"), k = 2;
  cv.width = width * k; cv.height = height * k;
  const x = cv.getContext("2d"); x.scale(k, k);
  x.fillStyle = v("--card"); x.fillRect(0, 0, width, height);
  const fit = (t, w) => { t = String(t); while (t.length > 3 && x.measureText(t).width > w) t = t.slice(0, -2); return t; };
  x.textBaseline = "middle";
  d.lots.forEach((lot, i) => {
    x.font = "600 13px Segoe UI, Arial"; x.fillStyle = v("--text");
    const words = lot.title, cx = W0 + W * i + pad;
    x.fillText(fit(words, W - 2 * pad), cx, 18);
    x.font = "12px Segoe UI, Arial"; x.fillStyle = v("--muted");
    x.fillText(lot.id === d.ref ? "эталон" : "лот " + (i + 1), cx, 38);
  });
  rows.forEach((r, j) => {
    const y = HH + RH * j;
    x.strokeStyle = v("--line"); x.beginPath(); x.moveTo(0, y); x.lineTo(width, y); x.stroke();
    x.font = "13px Segoe UI, Arial"; x.fillStyle = v("--muted"); x.fillText(fit(r.label, W0 - 2 * pad), pad, y + RH / 2);
    r.cells.forEach((c, i) => {
      const cx = W0 + W * i;
      if (c.mark) { x.fillStyle = v(c.mark === "best" ? "--best-bg" : "--worst-bg"); x.fillRect(cx, y + 1, W, RH - 1); }
      x.font = (c.mark === "best" ? "600 " : "") + "13px Segoe UI, Arial"; x.fillStyle = c.mark === "worst" ? v("--muted") : v("--text");
      x.fillText(fit(c.text + (c.diff ? "  (" + c.diff + ")" : ""), W - 2 * pad), cx + pad, y + RH / 2);
    });
  });
  cv.toBlob(b => {
    const a = h("a", {href: URL.createObjectURL(b), download: "gpuflip-сравнение-" + new Date().toISOString().slice(0, 10) + ".png"});
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }, "image/png");
};

G.hooks.compareChanged = () => { if (G.state.tab === "compare") C.load(); };
G.tabs.compare = function () { C.load(); C.loadModels(); C.loadSets().catch(() => {}); };
})();
