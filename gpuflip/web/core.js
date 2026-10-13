"use strict";
// Общее ядро: помощники, запросы, настройки вида, вкладки. Всё живёт в пространстве G.
const G = {state: {}, tabs: {}, hooks: {}};
const $ = id => document.getElementById(id);

G.fmt = n => n == null ? "—" : Math.round(n).toLocaleString("ru-RU") + " ₽";
G.num = n => (n || 0).toLocaleString("ru-RU");
G.pct = x => x == null ? "—" : Math.round(x * 100) + "%";
G.when = ts => ts ? new Date(ts * 1000).toLocaleString("ru-RU", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"}) : "—";
G.day = ts => ts ? new Date(ts * 1000).toLocaleDateString("ru-RU", {day: "numeric", month: "short"}) : "—";
G.days = d => d == null ? "—" : d < 1 ? "меньше дня" : Math.round(d) + " дн.";
G.FLAGS = {mining: "майнинг", untested: "не тестировалась", prepay: "предоплата", repaired: "был ремонт", flashed: "прошивка",
  no_testing_allowed: "без проверки", issues: "есть нюансы", no_warranty: "без гарантии", model_from_desc: "модель не из заголовка",
  reseller: "перекупщик / дубли"};
G.REASONS = {profit_too_small: "прибыль мала", margin_too_small: "маржа мала", out_of_budget: "вне бюджета",
  too_cheap_scam_risk: "подозрительно дёшево", no_price: "нет цены", thin_market: "мало объявлений для оценки",
  low_data: "мало данных — без алерта", prepayment: "предоплата", illiquid: "долго продаётся", defective: "брак", wanted: "«куплю»",
  model_not_found: "модель не определена", vram_unknown: "объём памяти не указан", multi_model: "несколько моделей",
  not_gpu: "не видеокарта", no_delivery: "нет доставки", not_below_median: "не ниже медианы", position_not_recognized: "позиция не определена",
  gpu_not_priced: "видеокарта не оценена", only_gpu_recognized: "распознана только видеокарта",
  parts_not_25pct_above_price: "детали не на 25% дороже лота", gpu_absent: "без видеокарты", laptop: "ноутбук", monoblock: "моноблок",
  not_desktop: "не системный блок", fuzzy_price_above_60pct: "опечатка: цена выше 60% медианы", price_not_suspicious: "цена обычная",
  profit_below_urgent_threshold: "прибыль ниже порога", pc_not_supported_yet: "целый ПК", bundle: "целый ПК"};
G.reason = r => G.REASONS[(r || "").split(/[(:]/)[0]] || r || "";
G.STATUS = {new: "Новый", checked: "Проверил", bought: "Куплено", skipped: "Пропущено"};
G.CONF = {high: "высокое", medium: "среднее", low: "низкое"};
G.MODE_TIPS = {market: "Модель из заголовка, цена заметно ниже медианы позиции", bundles: "Целый ПК дешевле суммы своих деталей",
  regional: "Лот с Авито Доставкой не из Москвы дешевле московской медианы", badtext: "Модель найдена только в описании или с опечаткой",
  urgent: "Срочная продажа: порог прибыли снижен до 10%", categories: "Комплектующее дешевле медианы своей позиции",
  manual: "Модель не определилась, цена подозрительно низкая — проверить вручную"};

// Тексты объявлений — чужие данные: в DOM только через textContent.
G.h = function (tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v; else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el[k] = v; else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
};
G.svg = function (tag, attrs, ...kids) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  for (const kid of kids.flat()) if (kid != null) el.append(kid);
  return el;
};
G.api = async function (path, body) {
  const r = await fetch(path, body === undefined ? {} :
    {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
};
G.say = function (id, text, ok) { const el = $(id); el.textContent = text; el.className = "msg " + (ok ? "ok" : "err"); };
G.safeUrl = u => /^https:\/\/www\.avito\.ru\//.test(u || "") ? u : null;
G.pic = function (src, cls) {
  if (!src || !/^https:\/\//.test(src)) return G.h("div", {class: (cls || "pic") + " none", text: "нет фото"});
  const img = G.h("img", {class: cls || "pic", src, alt: "", loading: "lazy", referrerpolicy: "no-referrer"});
  img.onerror = () => img.replaceWith(G.h("div", {class: (cls || "pic") + " none", text: "нет фото"}));
  return img;
};

// ---------- метки лота: цвет и подсказка у каждой ----------
G.profitClass = p => p >= 5000 ? "p-strong" : p > 0 ? "p-mild" : "p-none";
G.trust = function (sc) {
  if (!sc || !sc.market_n) return {cls: "", text: "нет оценки", tip: "В позиции меньше 6 объявлений или она не определена — рынок не оценён"};
  if (sc.market_n >= 10) return {cls: "good", text: "n=" + sc.market_n, tip: "В позиции " + sc.market_n + " объявлений — данных достаточно, алерты разрешены"};
  if (sc.market_n >= 6) return {cls: "warn", text: "n=" + sc.market_n + " · мало данных", tip: "6–9 объявлений: оценка показана, но алерта не будет"};
  return {cls: "", text: "n=" + sc.market_n, tip: "Меньше 6 объявлений — оценки нет"};
};
G.fresh = function (it) {
  if (it.age_hours == null) return null;
  if (it.age_hours <= 1) return {cls: "info strong", text: "новое", tip: "Появилось меньше часа назад"};
  if (it.age_hours <= 24) return {cls: "info", text: "сегодня", tip: "Появилось за последние сутки"};
  return null;
};
G.badges = function (it, opt) {
  // значки: режимы, срочность, доставка, снижение цены, мало данных, риск. У каждого — подсказка при наведении.
  const out = [], chip = (cls, text, tip) => out.push(G.h("span", {class: "chip " + cls, text, title: tip}));
  for (const m of it.modes) chip("info", m.title, G.MODE_TIPS[m.key] || "");
  const fr = G.fresh(it); if (fr) chip(fr.cls, fr.text, fr.tip);
  if (it.urgent) chip("warn", "⚡ срочно / торг", "В тексте есть маркер срочности или торга — порог прибыли ниже");
  if (it.delivery) chip("good", "доставка", "Есть Авито Доставка");
  if (it.price_drop) chip("good", "↓ " + G.fmt(it.price_drop), "Цена снижена с " + G.fmt(it.first_price) + " (на " + G.pct(it.price_drop_pct) + ")");
  if (it.days_listed >= 7) chip("", "висит " + Math.round(it.days_listed) + " дн.", "Столько дней объявление известно — можно торговаться смелее");
  if (it.reseller) chip("warn", "перекупщик", "У продавца несколько одинаковых лотов или объявление продублировано — штраф к оценке");
  if (it.gone) chip("bad", "ушло " + G.day(it.gone_at) + (it.lifetime_days != null ? " · прожило " + G.days(it.lifetime_days) : ""),
    "Объявление снято с публикации (проверено по карточке " + G.when(it.checked_at) + "). Запись остаётся в базе для анализа");
  else if (it.stale) chip("", "не видели " + Math.round(it.unseen_days) + " дн.", "Давно не попадалось в выдаче: ушло вглубь или снято. Будет проверено по карточке");
  if (it.defect) chip("bad", "брак", "В тексте есть стоп-слово — лот отсеян");
  if (it.wanted) chip("warn", "куплю", "Это объявление о покупке, а не о продаже");
  if (!opt || !opt.short) for (const f of Object.keys(it.flags)) if (f !== "reseller") chip("warn", G.FLAGS[f] || f, "Признак риска: штраф " + Math.round(it.flags[f] * 100) + "% к оценке");
  if (it.avito_condition) chip("", it.avito_condition, "Состояние, указанное на Авито");
  return out;
};

// ---------- настройки вида: хранятся в браузере и на сервере ----------
G.DEFAULT_COLS = [
  ["cmp", "Ср.", 44, true, true], ["fav", "★", 40, true, true], ["title", "Название", 300, true, true],
  ["price", "Цена", 100, true, false], ["median", "Рынок", 100, true, false], ["disc", "К рынку", 86, true, false],
  ["profit", "Прибыль", 110, true, false], ["n", "Данные", 120, true, false], ["model", "Позиция", 170, true, false],
  ["modes", "Режимы", 190, true, false], ["city", "Где", 170, true, false], ["delivery", "Дост.", 60, true, false],
  ["age", "Появилось", 110, true, false], ["listed", "Висит", 80, false, false], ["drop", "Снижение", 100, false, false],
  ["seller", "Продавец", 150, false, false], ["rating", "Рейтинг", 80, false, false], ["risk", "Риск", 150, false, false],
  ["status", "Статус", 110, false, false],
  ["first", "В базе с", 120, false, false], ["seen", "Видели", 120, false, false], ["gone", "Ушло", 110, false, false],
  ["life", "Прожило", 90, false, false],
];
G.COL_SORT = {title: "title", price: "price_asc", median: "median", disc: "pct", profit: "profit", n: "n", model: "model",
  city: "city", age: "date", listed: "age", drop: "drop", rating: "rating", first: "first", seen: "seen", gone: "gone", life: "life"};
G.defaultView = () => ({mode: "cards", density: "normal", font: 14, theme: "auto",
  cols: G.DEFAULT_COLS.map(c => ({key: c[0], on: c[3], pin: c[4]}))});
G.PROFILES = {   // стартовые профили вида; свои сохраняются поверх
  "для охоты": {mode: "compact", density: "compact", sort: "date"},
  "для анализа": {mode: "table", density: "normal", sort: "profit",
    cols: ["cmp", "fav", "title", "price", "median", "disc", "profit", "n", "model", "listed", "drop", "seller", "rating", "risk"]},
};
G.view = G.defaultView();
G.profiles = {};
try { Object.assign(G.view, JSON.parse(localStorage.getItem("gpuflip-view") || "{}")); } catch (e) {}
G.normCols = function () {     // новые колонки дописываются, исчезнувшие убираются
  const known = new Map(G.DEFAULT_COLS.map(c => [c[0], c]));
  const cur = (G.view.cols || []).filter(c => known.has(c.key));
  for (const c of G.DEFAULT_COLS) if (!cur.find(x => x.key === c[0])) cur.push({key: c[0], on: c[3], pin: c[4]});
  G.view.cols = cur;
};
G.applyView = function () {
  const r = document.documentElement;
  if (G.view.theme === "auto") r.removeAttribute("data-theme"); else r.setAttribute("data-theme", G.view.theme);
  r.setAttribute("data-density", G.view.density);
  r.style.setProperty("--fs", G.view.font + "px");
  $("theme").textContent = "Тема: " + ({auto: "авто", light: "светлая", dark: "тёмная"})[G.view.theme];
  for (const [id, k] of [["p-density", "density"], ["p-font", "font"], ["p-theme", "theme"]]) if ($(id)) $(id).value = G.view[k];
  for (const b of $("viewmode").children) b.classList.toggle("on", b.dataset.view === G.view.mode);
};
let saveTimer;
G.saveView = function () {
  try { localStorage.setItem("gpuflip-view", JSON.stringify(G.view)); } catch (e) {}
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => G.api("/api/prefs", {key: "ui.view", value: G.view}).catch(() => {}), 600);
};
G.normCols(); G.applyView();
$("theme").onclick = () => { const t = ["auto", "light", "dark"]; G.view.theme = t[(t.indexOf(G.view.theme) + 1) % 3]; G.applyView(); G.saveView(); };

// ---------- корзина сравнения ----------
G.cmp = [];
try { G.cmp = JSON.parse(localStorage.getItem("gpuflip-cmp") || "[]").slice(0, 6); } catch (e) {}
G.cmpToggle = function (id) {
  const i = G.cmp.indexOf(id);
  if (i >= 0) G.cmp.splice(i, 1);
  else if (G.cmp.length >= 6) { alert("В сравнении уже 6 лотов — уберите один."); return false; }
  else G.cmp.push(id);
  G.cmpChanged();
  return true;
};
let cmpTimer;
G.cmpChanged = function () {
  try { localStorage.setItem("gpuflip-cmp", JSON.stringify(G.cmp)); } catch (e) {}
  clearTimeout(cmpTimer);      // корзина хранится и на сервере — переживает перезапуск приложения
  cmpTimer = setTimeout(() => G.api("/api/prefs", {key: "ui.compare", value: {ids: G.cmp, ref: (G.compare && G.compare.ref) || null}}).catch(() => {}), 500);
  $("cmpbar").hidden = !G.cmp.length || G.state.tab === "compare";
  $("cmp-count").textContent = "Сравнить (" + G.cmp.length + ")";
  document.querySelectorAll("[data-cmp]").forEach(cb => { cb.checked = G.cmp.includes(cb.dataset.cmp); });
  if (G.hooks.compareChanged) G.hooks.compareChanged();
};
$("cmp-open").onclick = () => G.tab("compare");
$("cmp-reset").onclick = () => { G.cmp = []; G.cmpChanged(); };

// ---------- вкладки ----------
G.tab = function (name) {
  if (!G.tabs[name]) name = "list";
  G.state.tab = name;
  for (const b of $("tabs").children) b.classList.toggle("on", b.dataset.tab === name);
  for (const t of Object.keys(G.tabs)) $("tab-" + t).hidden = t !== name;
  clearInterval(G.state.polling); G.state.polling = null;
  $("cmpbar").hidden = !G.cmp.length || name === "compare";
  try { localStorage.setItem("gpuflip-tab", name); } catch (e) {}
  G.tabs[name]();
};
for (const b of $("tabs").children) b.onclick = () => G.tab(b.dataset.tab);

// ---------- диалоги ----------
document.querySelectorAll("dialog [data-close]").forEach(b => { b.onclick = () => b.closest("dialog").close(); });
$("help-btn").onclick = () => $("help").showModal();
