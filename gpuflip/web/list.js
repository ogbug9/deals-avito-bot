"use strict";
// Список объявлений: фильтры, пресеты, чипы, четыре вида, виртуальная прокрутка, колонки, горячие клавиши.
(function () {
const h = G.h;
const TEXT = ["q", "pmin", "pmax", "region", "city", "rating", "age", "hours", "model", "mode", "conf", "ustatus", "disc", "status", "cond"];
const CHECKS = ["delivery", "profit", "fav", "urgent", "drop", "junk", "pcgain"];
const SHOWS = ["defect", "wanted", "excluded"];            // галочка «скрыть» включена → параметра нет
const PRESETS = [
  ["Только кандидаты", {profit: "1"}, "Лоты, прошедшие хотя бы один режим поиска"],
  ["Скидка от 20%", {disc: "20"}, "Цена ниже медианы позиции минимум на 20%"],
  ["Срочные", {urgent: "1"}, "В тексте есть маркер срочности или торга"],
  ["С доставкой", {delivery: "1"}, "Есть Авито Доставка"],
  ["Новые за час", {hours: "1"}, "Появились за последний час"],
  ["Цена снижена", {drop: "1"}, "Продавец уже снижал цену"],
  ["ПК дешевле деталей", {pcgain: "1"}, "Системные блоки, у которых оценённые детали стоят больше самого лота"],
];
const BLOCK = 100;
const L = G.list = {rows: [], total: 0, cur: -1, shown: 0, sort: "date", dir: "", pending: {}, cats: new Set(), seq: 0,
  labels: {}, loading: new Set()};

// ---------- параметры ----------
function val(k) { return ($("f-" + k).value || L.pending[k] || "").trim(); }
L.params = function () {
  const p = new URLSearchParams();
  for (const k of TEXT) if (val(k)) p.set(k, val(k));
  if (L.cats.size) p.set("cat", [...L.cats].join(","));
  for (const k of CHECKS) if ($("f-" + k).checked) p.set(k, "1");
  for (const k of SHOWS) if (!$("f-" + k).checked) p.set(k, "show");
  if (L.sort && L.sort !== "date") p.set("sort", L.sort);
  if (L.dir) p.set("dir", L.dir);
  return p;
};
L.restore = function (query) {
  const p = new URLSearchParams(query);
  L.pending = {};
  for (const k of TEXT) {
    const v = p.get(k) || "";
    $("f-" + k).value = v;
    if (v && $("f-" + k).value !== v) L.pending[k] = v;      // список ещё не загружен — применится после facets()
  }
  L.cats = new Set((p.get("cat") || "").split(",").filter(Boolean));
  for (const k of CHECKS) $("f-" + k).checked = p.get(k) === "1";
  for (const k of SHOWS) $("f-" + k).checked = p.get(k) !== "show";
  L.sort = p.get("sort") || "date"; L.dir = p.get("dir") || "";
  $("f-sort").value = L.sort; if ($("f-sort").value !== L.sort) $("f-sort").value = "";
  document.querySelectorAll("#f-cats input").forEach(cb => { cb.checked = L.cats.has(cb.value); });
};
function clearParam(k) {
  if (k === "cat") L.cats.clear(); else if (CHECKS.includes(k)) $("f-" + k).checked = false;
  else if (SHOWS.includes(k)) $("f-" + k).checked = true;
  else { $("f-" + k).value = ""; delete L.pending[k]; }
  document.querySelectorAll("#f-cats input").forEach(cb => { cb.checked = L.cats.has(cb.value); });
  L.reload();
}
function chipsAndPresets() {
  const p = L.params(), chips = [];
  const show = (k, text) => chips.push(h("span", {class: "chip"}, text,
    h("button", {type: "button", "aria-label": "Снять фильтр: " + text, title: "Снять фильтр", text: "×", onclick: () => clearParam(k)})));
  const names = {region: {msk: "Москва", regions: "Другие регионы"}, conf: {high: "доверие высокое", medium: "доверие среднее", low: "доверие низкое"},
    ustatus: Object.assign({none: "без статуса"}, G.STATUS), status: {inactive: "ушедшие", stale: "давно не видели", all: "все, включая ушедшие"}, cond: {new: "похоже на новое", used: "б/у"}};
  for (const [k, v] of p) {
    if (k === "sort" || k === "dir") continue;
    if (k === "cat") { show(k, "категории: " + L.cats.size); continue; }
    if (SHOWS.includes(k)) { show(k, {defect: "показаны брак и мусор", wanted: "показаны «куплю»", excluded: "показаны исключённые"}[k]); continue; }
    const label = L.labels[k] || k;
    if (CHECKS.includes(k)) show(k, label);
    else if (k === "mode") show(k, "режим: " + ($("f-mode").selectedOptions[0] ? $("f-mode").selectedOptions[0].textContent.replace(/ \(\d+\)$/, "") : v));
    else show(k, label + ": " + ((names[k] || {})[v] || v));
  }
  $("filterchips").replaceChildren(...chips);
  $("presets").replaceChildren(...PRESETS.map(([label, set, tip]) => {
    const on = Object.entries(set).every(([k, v]) => p.get(k) === v);
    return h("button", {class: "btn small" + (on ? " on" : ""), type: "button", title: tip, "aria-pressed": on ? "true" : "false", text: label,
      onclick: () => { for (const [k, v] of Object.entries(set)) { if (CHECKS.includes(k)) $("f-" + k).checked = !on; else $("f-" + k).value = on ? "" : v; } L.reload(); }});
  }));
  $("export-btn").href = "/api/export.csv?" + p;
}

// ---------- загрузка ----------
L.reload = async function (keepCur) {
  const my = ++L.seq;
  L.rows = []; L.loading.clear(); L.shown = 0;
  if (!keepCur) L.cur = -1;
  const p = L.params();
  history.replaceState(null, "", "#" + p.toString());
  chipsAndPresets();
  let data;
  try { data = await G.api("/api/listings?" + p + "&offset=0&limit=" + BLOCK); }
  catch (e) { $("count").textContent = "Ошибка: " + e.message; return; }
  if (my !== L.seq) return;                         // пришёл ответ на устаревший запрос
  L.total = data.total; L.hints = data.hints || [];
  data.items.forEach((it, i) => { L.rows[i] = it; });
  $("count").textContent = "Найдено: " + G.num(L.total);
  L.render();
};
async function ensure(from, to) {                   // подгрузить недостающие блоки для окна [from, to)
  const my = L.seq, jobs = [];
  for (let b = Math.floor(from / BLOCK); b * BLOCK < Math.min(to, L.total); b++) {
    if (L.rows[b * BLOCK] || L.loading.has(b)) continue;
    L.loading.add(b);
    jobs.push(G.api("/api/listings?" + L.params() + "&offset=" + b * BLOCK + "&limit=" + BLOCK).then(d => {
      if (my !== L.seq) return;
      d.items.forEach((it, i) => { L.rows[b * BLOCK + i] = it; });
    }).catch(() => {}).finally(() => L.loading.delete(b)));
  }
  if (jobs.length) { await Promise.all(jobs); if (my === L.seq) (virtual() ? drawVirtual() : appendCards()); }
}
const virtual = () => G.view.mode === "table" || G.view.mode === "compact";

// ---------- действия над лотом ----------
const A = G.act = {
  async save(it, patch) { try { Object.assign(it, await G.api("/api/listing", Object.assign({id: it.id}, patch))); return true; } catch (e) { alert(e.message); return false; } },
  async fav(it) { await A.save(it, {fav: !it.fav}); L.refreshRow(it); },
  async status(it, st) { await A.save(it, {user_status: st || null}); if (st === "skipped") L.reload(true); else L.refreshRow(it); },
  async hide(kind, it) {
    const value = kind === "seller" ? it.seller_id : it.item_key, title = kind === "seller" ? (it.seller_name || "продавец") : it.item_key;
    if (!value) { alert(kind === "seller" ? "У этого продавца нет кода в выдаче — скрыть его нельзя. Можно пропустить сам лот." : "Позиция не определена."); return; }
    if (!confirm("Скрыть " + (kind === "seller" ? "продавца «" : "модель «") + title + "»? Вернуть можно в «Настройках».")) return;
    try { await G.api("/api/exclude", {kind, value, title}); L.reload(true); G.panelClose && G.panelClose(); } catch (e) { alert(e.message); }
  },
  async bought(it) {
    const v = prompt("Цена покупки, ₽:", Math.round(it.price || 0));
    if (v == null) return;
    try { await G.api("/api/deals", {listing_id: it.id, buy_price: parseFloat(v)}); it.user_status = "bought"; L.refreshRow(it); alert("Сделка записана — см. вкладку «Сделки»."); }
    catch (e) { alert(e.message); }
  },
  open(it) { const u = G.safeUrl(it.url); if (u) window.open(u, "_blank", "noopener"); },
};
L.refreshRow = function (it) {
  const i = L.rows.findIndex(r => r && r.id === it.id);
  if (i < 0) return;
  L.rows[i] = it;
  if (virtual()) drawVirtual();
  else { const old = $("cards").querySelector('[data-i="' + i + '"]'); if (old) old.replaceWith(G.view.mode === "grid" ? gridEl(it, i) : cardEl(it, i)); mark(); }
  if (G.panelRefresh) G.panelRefresh(it);
};

// ---------- общие кусочки ----------
function cmpBox(it) {
  const cb = h("input", {type: "checkbox", "data-cmp": it.id, checked: G.cmp.includes(it.id), title: "Добавить в сравнение (клавиша c)", "aria-label": "Сравнить"});
  cb.onchange = () => { if (!G.cmpToggle(it.id)) cb.checked = false; };
  cb.onclick = e => e.stopPropagation();
  return cb;
}
function star(it) {
  return h("button", {class: "icon" + (it.fav ? " on" : ""), type: "button", title: "Избранное (клавиша f)", "aria-label": "Избранное", "aria-pressed": it.fav ? "true" : "false",
    text: it.fav ? "★" : "☆", onclick: e => { e.stopPropagation(); A.fav(it); }});
}
function profitText(it) { return it.est_profit > 0 ? "+" + G.fmt(it.est_profit) : ""; }
function discText(it) { const sc = it.score; return sc && sc.market_n >= 6 && sc.discount_vs_market != null ? (sc.discount_vs_market > 0 ? "−" : "+") + Math.abs(Math.round(sc.discount_vs_market * 100)) + "%" : ""; }
function statusSelect(it) {
  const st = h("select", {"aria-label": "Мой статус", title: "Мой статус"}, h("option", {value: "", text: "Без статуса"}), Object.entries(G.STATUS).map(([k, v]) => h("option", {value: k, text: v})));
  st.value = it.user_status || ""; st.onchange = () => A.status(it, st.value); st.onclick = e => e.stopPropagation();
  return st;
}

// ---------- карточки ----------
function cardEl(it, i) {
  const sc = it.score, tr = G.trust(sc);
  const side = h("div", {class: "side"}, h("div", {class: "price num", text: it.price ? G.fmt(it.price) : "Цена не указана"}));
  if (it.price_drop) side.append(h("div", {class: "old num", text: G.fmt(it.first_price)}));
  if (it.unit_price) side.append(h("div", {class: "small num", text: Math.round(it.unit_price).toLocaleString("ru-RU") + (/^PSU/.test(it.item_key || "") ? " ₽/Вт" : " ₽/ГБ")}));
  if (sc && sc.market_n >= 6) side.append(h("div", {class: "small num", text: "рынок " + G.fmt(sc.median_ask) + " (" + discText(it) + ")", title: "Медиана запросных цен позиции и отличие цены лота от неё"}),
    h("div", {class: "small num", text: "быстро продать ≈ " + G.fmt(sc.quick_price), title: (sc.why || []).join("\n")}));
  if (it.est_profit > 0) side.append(h("div", {class: "profit num " + G.profitClass(it.est_profit), text: "≈ " + profitText(it) + " (" + G.pct(it.est_profit_pct) + ")", title: "Расчётная чистая прибыль после торга, издержек и штрафов"}));
  else if (sc && sc.reason && !sc.ok && sc.market_n) side.append(h("div", {class: "small", text: G.reason(sc.reason)}));
  side.append(h("div", {class: "chips", style: "justify-content:flex-end;margin-top:6px"},
    h("span", {class: "chip " + tr.cls, text: tr.text, title: tr.tip}),
    it.confidence ? h("span", {class: "chip " + (it.confidence === "high" ? "good" : "warn"), text: "доверие: " + G.CONF[it.confidence], title: "Насколько надёжно определены модель и рынок"}) : null));
  const note = h("input", {type: "text", placeholder: "Заметка", value: it.note || "", "aria-label": "Заметка"});
  note.onchange = () => A.save(it, {note: note.value}); note.onclick = e => e.stopPropagation();
  const u = G.safeUrl(it.url);
  const el = h("article", {class: "card" + (it.defect || it.gone || it.junk ? " bad" : ""), "data-i": i, tabindex: "-1"}, G.pic(it.image),
    h("div", null,
      h("button", {class: "title", type: "button", text: it.title, title: "Открыть детали справа (Enter)", onclick: () => { L.setCur(i); G.panelOpen(it); }}),
      it.description ? h("div", {class: "desc", text: it.description}) : null,
      h("div", {class: "meta", text: [it.city, it.date_text, it.category_title, it.item_key].filter(Boolean).join(" · ") || "—"}),
      h("div", {class: "chips", style: "margin-top:6px"}, G.badges(it)),
      G.pcBox(it),
      it.reason ? h("ul", {class: "reasons"}, it.reason.split(" | ").map(r => h("li", {text: r}))) : null),
    side,
    h("div", {class: "tools"}, star(it), h("label", {class: "chk", title: "Добавить в сравнение (c)"}, cmpBox(it), "Сравнить"), statusSelect(it), note,
      h("button", {class: "link", type: "button", text: "Скрыть продавца", title: "Не показывать лоты этого продавца", onclick: () => A.hide("seller", it)}),
      h("button", {class: "link", type: "button", text: "Скрыть модель", title: "Не показывать эту позицию", onclick: () => A.hide("model", it)}),
      u ? h("a", {class: "btn small primary", href: u, target: "_blank", rel: "noopener noreferrer", text: "На Авито", title: "Открыть объявление (o)"}) : null));
  el.onclick = e => { if (!e.target.closest("a,button,input,select,label")) L.setCur(i); };
  return el;
}
function gridEl(it, i) {
  const u = G.safeUrl(it.url), tr = G.trust(it.score);
  const el = h("article", {class: "gcard" + (it.defect ? " bad" : ""), "data-i": i, tabindex: "-1"}, G.pic(it.image),
    h("div", {class: "body"},
      h("button", {class: "title", type: "button", text: it.title, onclick: () => { L.setCur(i); G.panelOpen(it); }}),
      h("div", null, h("span", {class: "price num", text: G.fmt(it.price)}), " ",
        it.est_profit > 0 ? h("span", {class: "num " + G.profitClass(it.est_profit), text: profitText(it)}) : null),
      h("div", {class: "meta", text: [it.item_key, it.city].filter(Boolean).join(" · ") || "—"}),
      it.parts && it.parts.value ? h("div", {class: "small num", title: it.parts.verdict, text: "детали ≈ " + G.fmt(it.parts.value) + " (" + it.parts.priced + " из " + it.parts.expected + ")"}) : null,
      h("div", {class: "chips"}, h("span", {class: "chip " + tr.cls, text: tr.text, title: tr.tip}), G.badges(it, {short: true}).slice(0, 4)),
      h("div", {class: "foot"}, star(it), h("label", {class: "chk"}, cmpBox(it), "Сравнить"),
        u ? h("a", {class: "btn small push", href: u, target: "_blank", rel: "noopener noreferrer", text: "На Авито"}) : null)));
  el.onclick = e => { if (!e.target.closest("a,button,input,select,label")) { L.setCur(i); G.panelOpen(it); } };
  return el;
}
function appendCards() {
  const box = $("cards"), grid = G.view.mode === "grid";
  box.className = grid ? "grid" : "";
  for (let i = box.querySelectorAll("[data-i]").length; i < Math.min(L.shown, L.total); i++) {
    if (!L.rows[i]) break;
    box.append(grid ? gridEl(L.rows[i], i) : cardEl(L.rows[i], i));
  }
  mark();
}
new IntersectionObserver(es => {
  if (!es[0].isIntersecting || virtual() || G.state.tab !== "list" || L.shown >= L.total) return;
  L.shown = Math.min(L.total, L.shown + 40); appendCards(); ensure(0, L.shown + 40);
}, {rootMargin: "600px"}).observe($("sentinel"));

// ---------- таблица и компактный список ----------
const CELL = {
  cmp: it => cmpBox(it), fav: it => star(it),
  title: it => [G.pic(it.image, "thumb"), h("span", {class: "t", text: it.title, title: it.title})],
  price: it => h("span", {class: "num", text: G.fmt(it.price)}),
  median: it => h("span", {class: "num small", text: it.score && it.score.market_n >= 6 ? G.fmt(it.score.median_ask) : "—"}),
  disc: it => { const t = discText(it), good = it.score && it.score.discount_vs_market > 0.1; return h("span", {class: "num " + (good ? "p-mild" : "small"), text: t || "—", title: "Цена относительно медианы позиции"}); },
  profit: it => h("span", {class: "num " + G.profitClass(it.est_profit), text: profitText(it) || "—", title: "Расчётная чистая прибыль"}),
  n: it => { const tr = G.trust(it.score); return h("span", {class: "chip " + tr.cls, text: tr.text, title: tr.tip}); },
  model: it => h("span", {class: "t", text: it.item_key || "—", title: it.item_key || "Позиция не определена"}),
  modes: it => G.badges(it, {short: true}).slice(0, 3),
  city: it => h("span", {class: "t small", text: it.city || "—", title: it.city || ""}),
  delivery: it => it.delivery ? h("span", {class: "chip good", text: "да", title: "Есть Авито Доставка"}) : h("span", {class: "small", text: "—"}),
  age: it => { const fr = G.fresh(it); return h("span", {class: fr ? "fresh" : "small", text: it.date_text || "—", title: fr ? fr.tip : ""}); },
  listed: it => h("span", {class: "num small", text: G.days(it.days_listed)}),
  drop: it => h("span", {class: "num " + (it.price_drop ? "p-mild" : "small"), text: it.price_drop ? "↓ " + G.fmt(it.price_drop) : "—"}),
  seller: it => h("span", {class: "t small", text: it.seller_name || "—"}),
  rating: it => h("span", {class: "num small", text: it.rating != null ? "★ " + String(it.rating).replace(".", ",") : "—"}),
  risk: it => { const f = Object.keys(it.flags); return h("span", {class: "t " + (f.length ? "chip warn" : "small"), text: f.map(x => G.FLAGS[x] || x).join(", ") || "—"}); },
  status: it => h("span", {class: "small", text: G.STATUS[it.user_status] || "—"}),
  first: it => h("span", {class: "num small", text: G.when(it.first_seen), title: "Когда объявление впервые попало в базу"}),
  seen: it => h("span", {class: "num small", text: G.when(it.last_seen), title: "Когда в последний раз видели в выдаче"}),
  gone: it => h("span", {class: "num " + (it.gone ? "" : "small"), text: it.gone ? G.when(it.gone_at) : it.stale ? "не видели " + Math.round(it.unseen_days) + " дн." : "—",
    title: it.gone ? "Снято с публикации" : ""}),
  life: it => h("span", {class: "num small", text: it.lifetime_days != null ? G.days(it.lifetime_days) : "—", title: "Сколько прожило объявление до снятия"}),
  quick: it => h("span", {class: "num small", text: it.score && it.score.quick_price ? G.fmt(it.score.quick_price) : "—", title: it.score ? (it.score.why || []).join("\n") : ""}),
  roi: it => h("span", {class: "num " + (it.est_profit > 0 ? "p-mild" : "small"), text: it.est_profit > 0 ? G.pct(it.est_profit_pct) : "—",
    title: "Чистая прибыль в процентах к цене покупки после торга"}),
  perday: it => { const d = it.score && it.score.days_to_sell || 14;
    return h("span", {class: "num " + (it.est_profit > 0 ? "p-mild" : "small"), text: it.est_profit > 0 ? G.fmt(it.est_profit / Math.max(d, 0.5)) : "—",
      title: "Прибыль, делённая на ожидаемый срок продажи (" + Math.round(d) + " дн.)"}); },
  parts: it => { const p = it.parts; if (!p || !p.value) return h("span", {class: "small", text: "—", title: p ? p.verdict : "Не системный блок"});
    return h("span", {class: "num " + (p.diff >= 0 ? "p-mild" : "small"), title: p.verdict, text: G.fmt(p.value) + " (" + (p.diff >= 0 ? "+" : "−") + Math.abs(Math.round(p.diff)).toLocaleString("ru-RU") + ")"}); },
};
const RIGHT = new Set(["price", "median", "disc", "profit", "listed", "drop", "rating", "life", "quick", "parts", "roi", "perday"]);
function cols() {
  const def = new Map(G.DEFAULT_COLS.map(c => [c[0], c]));
  const on = G.view.cols.filter(c => c.on).map(c => ({key: c.key, label: def.get(c.key)[1], w: def.get(c.key)[2], pin: c.pin}));
  const list = on.filter(c => c.pin).concat(on.filter(c => !c.pin));
  let left = 0; for (const c of list) { c.left = c.pin ? left : null; if (c.pin) left += c.w; }
  return list;
}
const rowH = () => parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--row")) || 40;
function cellStyle(c) { return "width:" + c.w + "px" + (c.pin ? ";left:" + c.left + "px" : ""); }
function drawHead() {
  if (G.view.mode !== "table") { $("vhead").replaceChildren(); $("vhead").hidden = true; return; }
  $("vhead").hidden = false;
  $("vhead").replaceChildren(...cols().map(c => {
    const sk = G.COL_SORT[c.key], active = sk && (L.sort === sk || (c.key === "price" && L.sort === "price_desc"));
    const arrow = active ? ((L.sort === "price_desc") !== (L.dir === "flip") ? " ↓" : " ↑") : "";
    return h("div", {class: "cell" + (RIGHT.has(c.key) ? " r" : "") + (c.pin ? " pin" : ""), style: cellStyle(c), role: "columnheader",
      title: sk ? "Сортировать по колонке «" + c.label + "»" : c.label, text: c.label + (active ? (L.dir === "flip" ? " ▲" : " ▼") : ""),
      onclick: sk ? () => { if (L.sort === sk) L.dir = L.dir ? "" : "flip"; else { L.sort = sk; L.dir = ""; } $("f-sort").value = L.sort; if ($("f-sort").value !== L.sort) $("f-sort").value = ""; L.reload(); } : null});
  }));
}
function tableRow(it, i, list) {
  const row = h("div", {class: "vrow" + (i === L.cur ? " cur" : "") + (it.defect ? " bad" : ""), "data-i": i, role: "row", style: "top:" + i * rowH() + "px"});
  for (const c of list) row.append(h("div", {class: "cell" + (RIGHT.has(c.key) ? " r" : "") + (c.pin ? " pin" : ""), style: cellStyle(c), role: "cell"}, CELL[c.key](it)));
  return row;
}
function compactRow(it, i) {
  const tr = G.trust(it.score);
  return h("div", {class: "vrow cell crow" + (i === L.cur ? " cur" : "") + (it.defect ? " bad" : ""), "data-i": i, role: "row", style: "top:" + i * rowH() + "px;width:100%"},
    cmpBox(it), star(it), G.pic(it.image, "thumb"),
    h("span", {class: "grow", text: it.title, title: it.title}),
    G.badges(it, {short: true}).slice(0, 3),
    h("span", {class: "chip " + tr.cls, text: tr.text, title: tr.tip}),
    h("span", {class: "small", style: "width:110px;overflow:hidden;text-overflow:ellipsis", text: it.city || ""}),
    h("span", {class: "num " + G.profitClass(it.est_profit), style: "width:96px;text-align:right", text: profitText(it)}),
    h("span", {class: "num", style: "width:96px;text-align:right;font-weight:650", text: G.fmt(it.price)}));
}
function drawVirtual() {
  const sc = $("vscroll"), H = rowH(), headH = G.view.mode === "table" ? 34 : 0;
  $("vspace").style.height = L.total * H + "px";
  const first = Math.max(0, Math.floor((sc.scrollTop - headH) / H) - 8), last = Math.min(L.total, first + Math.ceil(sc.clientHeight / H) + 16);
  const list = cols(), rows = [];
  for (let i = first; i < last; i++) {
    const it = L.rows[i];
    if (!it) rows.push(h("div", {class: "vrow cell small", style: "top:" + i * H + "px;width:100%", text: "загрузка…"}));
    else rows.push(G.view.mode === "table" ? tableRow(it, i, list) : compactRow(it, i));
  }
  $("vspace").replaceChildren(...rows);
  ensure(first, last);
}
$("vscroll").addEventListener("scroll", () => requestAnimationFrame(drawVirtual), {passive: true});
$("vspace").addEventListener("click", e => {
  const row = e.target.closest(".vrow[data-i]");
  if (!row || e.target.closest("a,button,input,select,label")) return;
  const i = +row.dataset.i; L.setCur(i); if (L.rows[i]) G.panelOpen(L.rows[i]);
});

// ---------- отрисовка ----------
L.render = function () {
  const v = virtual();
  $("vwrap").hidden = !v; $("cards").hidden = v; $("cols-btn").hidden = G.view.mode !== "table";
  if (!L.total) {
    const hints = (L.hints || []).map(x => h("button", {class: "btn small", type: "button", text: "Снять «" + x.label + "» — найдётся " + G.num(x.count), onclick: () => clearParam(x.param)}));
    $("vwrap").hidden = true; $("cards").hidden = false; $("cards").className = "";
    $("cards").replaceChildren(h("div", {class: "empty"},
      h("div", {text: hints.length ? "Ничего не нашлось. Выдачу обнуляет один из фильтров:" : "Ничего не нашлось. Сбросьте фильтры или запустите сбор на вкладке «Статус»."}), hints));
    return;
  }
  if (v) { drawHead(); $("vscroll").scrollTop = 0; drawVirtual(); }
  else { $("cards").replaceChildren(); L.shown = Math.min(L.total, 40); appendCards(); }
};
function mark() { document.querySelectorAll("#cards [data-i]").forEach(el => el.classList.toggle("cur", +el.dataset.i === L.cur)); }
L.setCur = function (i) {
  if (!L.total) return;
  L.cur = Math.max(0, Math.min(L.total - 1, i));
  if (virtual()) {
    const sc = $("vscroll"), H = rowH(), top = L.cur * H, headH = G.view.mode === "table" ? 34 : 0;
    if (top < sc.scrollTop) sc.scrollTop = top; else if (top + H + headH > sc.scrollTop + sc.clientHeight) sc.scrollTop = top + H + headH - sc.clientHeight;
    drawVirtual();
  } else {
    if (L.cur >= L.shown) { L.shown = Math.min(L.total, L.cur + 20); appendCards(); ensure(0, L.shown + 40); }
    mark();
    const el = $("cards").querySelector('[data-i="' + L.cur + '"]'); if (el) el.scrollIntoView({block: "nearest"});
  }
  if (!$("panel").hidden && L.rows[L.cur]) G.panelOpen(L.rows[L.cur]);
};

// ---------- боковые фильтры ----------
L.facets = async function () {
  const f = await G.api("/api/facets");
  L.labels = f.filter_labels || {};
  $("stat").textContent = "активных " + G.num(f.total) + " · ушло " + G.num(f.gone) + " · давно не видели " + G.num(f.stale)
    + " · копится с " + G.day(f.since) + " · обновлено " + G.when(f.last_seen);
  $("stat").title = "База не очищается: ушедшие объявления остаются в ней с датами. Фильтр «Объявления» слева показывает их.";
  $("f-cats").replaceChildren(...f.categories.map(c => {
    const cb = h("input", {type: "checkbox", value: c.key, checked: L.cats.has(c.key)});
    cb.onchange = () => { cb.checked ? L.cats.add(c.key) : L.cats.delete(c.key); L.reload(); };
    return h("label", {class: "chk"}, cb, h("span", {text: c.title}), h("span", {class: "n num", text: c.n}));
  }));
  const fill = (k, opts, first) => { const sel = $("f-" + k), v = sel.value || L.pending[k] || ""; sel.replaceChildren(h("option", {value: "", text: first}), ...opts); sel.value = v; if (sel.value === v) delete L.pending[k]; };
  fill("region", f.regions.map(r => h("option", {value: r.key, text: r.title + " (" + G.num(r.n) + ")"})), "Любой регион");
  fill("model", f.models.map(m => h("option", {value: m.key, text: m.key + " (" + m.n + ")"})), "Любая модель или позиция");
  fill("mode", f.modes.map(m => h("option", {value: m.key, text: m.title + " (" + m.n + ")", title: G.MODE_TIPS[m.key] || ""})), "Любой режим поиска");
  const mc = $("m-cat"), mv = mc.value;
  mc.replaceChildren(h("option", {value: "", text: "Все категории"}), ...f.categories.map(c => h("option", {value: c.key, text: c.title}))); mc.value = mv;
  G.facetsData = f;
};
L.loadSaved = async function (list) {
  list = list || await G.api("/api/saved");
  $("saved").replaceChildren(...(list.length ? list.map(s => h("div", {class: "saved"},
    h("button", {class: "link", type: "button", text: s.name, title: "Применить поиск «" + s.name + "»", onclick: () => { L.restore(s.query); L.reload(); }}),
    h("button", {class: "icon" + (s.notify ? " on" : ""), type: "button", "aria-pressed": s.notify ? "true" : "false",
      title: s.notify ? "Уведомления по этому поиску включены — нажмите, чтобы выключить" : "Уведомлять о новых лотах по этому поиску", "aria-label": "Уведомления по поиску " + s.name, text: s.notify ? "🔔" : "🔕",
      onclick: async () => L.loadSaved(await G.api("/api/saved", {id: s.id, notify: !s.notify}))}),
    h("button", {class: "icon", type: "button", title: "Удалить поиск", "aria-label": "Удалить поиск " + s.name, text: "×",
      onclick: async () => L.loadSaved(await G.api("/api/saved", {delete: s.id}))})))
    : [h("div", {class: "small", text: "Пока нет. Настройте фильтры и сохраните."})]));
};
$("save-search").onclick = async () => {
  const name = prompt("Название поиска:", $("f-q").value || $("f-model").value || "Мой поиск");
  if (!name) return;
  const p = L.params(); p.delete("sort"); p.delete("dir");
  const notify = confirm("Уведомлять о новых лотах по этому поиску?\n\nОК — да, Отмена — просто сохранить.");
  try { L.loadSaved(await G.api("/api/saved", {name, query: p.toString(), notify})); } catch (e) { alert(e.message); }
};
let timer;
const soon = () => { clearTimeout(timer); timer = setTimeout(() => L.reload(), 300); };
for (const id of ["q", "pmin", "pmax", "city"]) $("f-" + id).addEventListener("input", soon);
for (const id of ["region", "rating", "age", "model", "mode", "conf", "ustatus", "disc", "status", "cond"].concat(CHECKS, SHOWS))
  $("f-" + id).addEventListener("change", () => { delete L.pending[id]; if (id === "age") $("f-hours").value = ""; L.reload(); });
$("f-sort").onchange = () => { L.sort = $("f-sort").value || "date"; L.dir = ""; L.reload(); };
$("f-reset").onclick = () => { L.restore(""); L.reload(); };
for (const b of $("viewmode").children) b.onclick = () => { G.view.mode = b.dataset.view; G.applyView(); G.saveView(); L.render(); };

// ---------- колонки и профили вида ----------
function drawCols() {
  const def = new Map(G.DEFAULT_COLS.map(c => [c[0], c]));
  let drag = null;
  $("cols-list").replaceChildren(...G.view.cols.map((c, i) => {
    const on = h("input", {type: "checkbox", checked: c.on, "aria-label": "Показывать колонку " + def.get(c.key)[1]});
    on.onchange = () => { c.on = on.checked; G.saveView(); L.render(); };
    const pin = h("input", {type: "checkbox", checked: c.pin, "aria-label": "Закрепить колонку " + def.get(c.key)[1]});
    pin.onchange = () => { c.pin = pin.checked; G.saveView(); L.render(); };
    const row = h("div", {class: "colrow", draggable: "true"}, h("span", {text: "⋮⋮", class: "small", "aria-hidden": "true"}),
      h("label", {class: "chk", style: "flex:1"}, on, def.get(c.key)[1] === "Ср." ? "Сравнение" : def.get(c.key)[1] === "★" ? "Избранное" : def.get(c.key)[1]),
      h("label", {class: "chk small"}, pin, "закрепить"),
      h("button", {class: "icon", type: "button", title: "Выше", "aria-label": "Переместить выше", text: "↑", onclick: () => move(i, i - 1)}),
      h("button", {class: "icon", type: "button", title: "Ниже", "aria-label": "Переместить ниже", text: "↓", onclick: () => move(i, i + 1)}));
    row.ondragstart = () => { drag = i; };
    row.ondragover = e => { e.preventDefault(); row.classList.add("over"); };
    row.ondragleave = () => row.classList.remove("over");
    row.ondrop = e => { e.preventDefault(); if (drag != null) move(drag, i); };
    return row;
  }));
  const all = Object.assign({}, G.PROFILES, G.profiles);
  $("profiles").replaceChildren(...Object.keys(all).map(name => h("span", {class: "chip"},
    h("button", {type: "button", text: name, title: "Применить профиль вида", onclick: () => applyProfile(all[name])}),
    G.profiles[name] ? h("button", {type: "button", title: "Удалить профиль", "aria-label": "Удалить профиль " + name, text: "×", onclick: () => { delete G.profiles[name]; saveProfiles(); drawCols(); }}) : null)));
}
function move(from, to) {
  if (to < 0 || to >= G.view.cols.length || from === to) return;
  G.view.cols.splice(to, 0, G.view.cols.splice(from, 1)[0]); G.saveView(); drawCols(); L.render();
}
function applyProfile(p) {
  if (p.mode) G.view.mode = p.mode;
  if (p.density) G.view.density = p.density;
  if (p.font) G.view.font = p.font;
  if (Array.isArray(p.cols)) {
    const first = p.cols.map(c => typeof c === "string" ? {key: c, on: true, pin: ["cmp", "fav", "title"].includes(c)} : c);
    const keys = new Set(first.map(c => c.key));
    G.view.cols = first.concat(G.view.cols.filter(c => !keys.has(c.key)).map(c => ({key: c.key, on: false, pin: false})));
    G.normCols();
  }
  if (p.sort) { L.sort = p.sort; L.dir = ""; $("f-sort").value = L.sort; }
  G.applyView(); G.saveView(); drawCols(); L.reload();
}
function saveProfiles() { G.api("/api/prefs", {key: "ui.profiles", value: G.profiles}).catch(() => {}); }
$("cols-btn").onclick = () => { drawCols(); $("cols").showModal(); };
$("profile-save").onclick = () => {
  const name = $("profile-name").value.trim();
  if (!name) { $("profile-name").focus(); return; }
  G.profiles[name] = {mode: G.view.mode, density: G.view.density, font: G.view.font, cols: JSON.parse(JSON.stringify(G.view.cols)), sort: L.sort};
  $("profile-name").value = ""; saveProfiles(); drawCols();
};

// ---------- горячие клавиши ----------
document.addEventListener("keydown", e => {
  const tag = (e.target.tagName || "").toLowerCase(), typing = tag === "input" || tag === "textarea" || tag === "select";
  if (e.key === "Escape") { if (typing) e.target.blur(); else if (!$("panel").hidden) G.panelClose(); return; }
  if (typing || e.ctrlKey || e.metaKey || e.altKey || document.querySelector("dialog[open]")) return;
  if (e.key === "?") { $("help").showModal(); return; }
  if (G.state.tab !== "list") return;
  const it = L.rows[L.cur];
  const keys = {
    j: () => L.setCur(L.cur + 1), k: () => L.setCur(L.cur < 0 ? 0 : L.cur - 1),
    Enter: () => it && G.panelOpen(it), o: () => it && A.open(it), f: () => it && A.fav(it),
    c: () => it && G.cmpToggle(it.id), x: () => it && A.status(it, "skipped"), "/": () => $("f-q").focus(),
  };
  const fn = keys[e.key] || keys[({"о": "j", "л": "k", "щ": "o", "а": "f", "с": "c", "ч": "x"})[e.key]];   // и в русской раскладке
  if (fn) { e.preventDefault(); fn(); }
});
window.addEventListener("hashchange", () => { L.restore(location.hash.slice(1)); G.tab("list"); });

G.tabs.list = function () { L.facets().then(() => L.reload()).catch(e => { $("stat").textContent = "Ошибка: " + e.message; }); L.loadSaved().catch(() => {}); };
L.restore(location.hash.slice(1));
})();
