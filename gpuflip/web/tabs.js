"use strict";
// Вкладки «Сегодня», «Рынок», «Сделки», «Статус», «Настройки» и запуск интерфейса.
(function () {
const h = G.h, svg = G.svg;
const tile = (label, value, sub, tip) => h("div", {class: "tile", title: tip || null}, h("span", {class: "small", text: label}), h("b", {class: "num", text: value}), sub ? h("span", {class: "small", text: sub}) : null);
const tsToDate = ts => ts ? new Date(ts * 1000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10) : "";
const dateToTs = v => v ? Date.parse(v + "T12:00:00") / 1000 : null;

// ---------- Сегодня ----------
G.tabs.today = async function () {
  let d;
  try { d = await G.api("/api/today"); } catch (e) { $("t-best").replaceChildren(h("div", {class: "note", text: e.message})); return; }
  const top = d.best[0];
  $("t-tiles").replaceChildren(
    tile("Новых кандидатов сегодня", G.num(d.new_candidates), "всего активных: " + G.num(d.candidates), "Лоты, впервые прошедшие режим поиска сегодня"),
    tile("Лучший по прибыли", top ? "+" + G.fmt(top.est_profit) : "—", top ? top.item_key || top.title.slice(0, 30) : "кандидатов нет"),
    tile("Новых объявлений", G.num(d.new_listings), "цена снижена у: " + G.num(d.price_drops)),
    tile("Ушло лотов", G.num(d.gone), "из них кандидатов: " + G.num(d.gone_candidates), "Объявления, которые при перепроверке оказались сняты"),
    tile("Сбор", d.app.state === "running" ? "идёт" : d.app.paused ? "пауза" : "ждёт", d.app.status_line));
  $("t-best").replaceChildren(...(d.best.length ? d.best.map(it => {
    const u = G.safeUrl(it.url), tr = G.trust(it.score);
    return h("div", {class: "simrow"}, G.pic(it.image, "thumb"),
      h("button", {class: "link grow", type: "button", text: it.title, title: "Показать в списке", onclick: () => { G.list.restore("profit=1&sort=profit"); G.tab("list"); }}),
      h("span", {class: "chips"}, G.badges(it, {short: true}).slice(0, 3)),
      h("span", {class: "chip " + tr.cls, text: tr.text, title: tr.tip}),
      h("span", {class: "num", text: G.fmt(it.price)}),
      h("span", {class: "num " + G.profitClass(it.est_profit), style: "width:110px;text-align:right", text: "+" + G.fmt(it.est_profit)}),
      u ? h("a", {class: "btn small", href: u, target: "_blank", rel: "noopener noreferrer", text: "Авито"}) : null);
  }) : [h("div", {class: "small", text: "Кандидатов сейчас нет. Они появляются, когда лот заметно дешевле медианы своей позиции."})]));
  const sch = d.app.schedule;
  $("t-collect").textContent = d.app.status_line + (d.app.next_at ? " · следующий цикл около " + G.when(d.app.next_at) : "")
    + (d.app.requests_limit ? " · запросов за час: " + d.app.requests_hour + " из " + d.app.requests_limit : "")
    + (sch && sch.slowed ? " · защита от блокировок: интервалы увеличены в " + Math.round(sch.factor) + " раз(а)" : "");
};
$("t-all").onclick = () => { G.list.restore("profit=1&sort=profit"); G.tab("list"); };

// ---------- Рынок ----------
function spark(hist) {
  if (hist.length < 2) return h("span", {class: "small", text: "копится"});
  const W = 120, H = 26, v = hist.map(x => x.median), lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
  const d = v.map((y, i) => (i ? "L" : "M") + (i / (v.length - 1) * (W - 4) + 2).toFixed(1) + "," + (H - 3 - (y - lo) / span * (H - 6)).toFixed(1)).join("");
  return svg("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, "aria-hidden": "true"}, svg("path", {d, fill: "none", stroke: "var(--series-a)", "stroke-width": 2}));
}
function modelsBtn() { const n = G.compare.models.length; $("m-compare").textContent = "Сравнить модели (" + n + ")"; $("m-compare").disabled = n < 2; }
G.hooks.modelsChanged = () => { modelsBtn(); document.querySelectorAll("[data-model]").forEach(cb => { cb.checked = G.compare.models.includes(cb.dataset.model); }); };
G.tabs.market = async function () {
  let d;
  try { d = await G.api("/api/market?days=" + $("m-days").value + "&cat=" + encodeURIComponent($("m-cat").value)); }
  catch (e) { $("m-note").textContent = "Ошибка: " + e.message; return; }
  $("m-note").textContent = (d.history_days < 2
    ? "Динамика появится, когда накопится история хотя бы за два дня: медианы записываются при каждом обходе. "
    : "История медиан за " + d.history_days + " дн. «Новых» и «ушло» — за выбранный период. ")
    + "Ликвидность — медианный срок жизни ушедших объявлений; пока ушедших мало, она помечена «грубо». "
    + (d.analogs_error || (d.analogs_approx ? "«₽ за пункт» считается по ориентировочному индексу из words\\analogs.json." : ""));
  const LIQ = {high: ["good", "высокая"], medium: ["warn", "средняя"], low: ["bad", "низкая"], none: ["", "нет данных"]};
  $("m-rows").replaceChildren(...d.rows.map(r => {
    const cb = h("input", {type: "checkbox", "data-model": r.key, checked: G.compare.models.includes(r.key), "aria-label": "Сравнить модель " + r.key});
    cb.onclick = e => e.stopPropagation();
    cb.onchange = () => {
      const m = G.compare.models, i = m.indexOf(r.key);
      if (i >= 0) m.splice(i, 1); else if (m.length >= 4) { cb.checked = false; alert("Сравнить можно до 4 моделей."); } else m.push(r.key);
      modelsBtn();
    };
    const liq = LIQ[r.liq_level];
    const tr = h("tr", {class: "click", title: "Показать объявления этой позиции"},
      h("td", {class: "c"}, cb), h("td", {text: r.key}), h("td", {class: "r num", text: r.n}),
      h("td", null, h("span", {class: "chip " + (r.trust === "ok" ? "good" : r.trust === "low" ? "warn" : ""),
        text: r.trust === "ok" ? "достаточно" : r.trust === "low" ? "мало данных" : "оценки нет", title: "От 10 объявлений — алерты; 6–9 — только оценка; меньше — оценки нет"})),
      h("td", {class: "r num", text: G.fmt(r.median), title: r.used_only ? "Только б/у: " + r.new_n + " новых не учтено" : ""}),
      h("td", {class: "r num", text: r.quick ? G.fmt(r.quick) : "—", title: (r.why || []).join("\n")},
        r.ceiling_key ? h("span", {class: "small", text: " ▾", title: "Ограничено ценой более сильной " + r.ceiling_key}) : null),
      h("td", {class: "r num small", text: r.comfort ? G.fmt(r.comfort) : "—"}), h("td", {class: "r num small", text: G.fmt(r.min)}),
      h("td", {class: "r num", text: r.new}), h("td", {class: "r num", text: r.gone + " / " + r.gone_total, title: "За период / за всё время"}),
      h("td", {class: "r num small", text: r.sold_median ? G.fmt(r.sold_median) : "—", title: r.sold_median ? "Медиана последней цены " + r.gone_total + " ушедших лотов" : "Ушедших лотов этой позиции меньше трёх"}),
      h("td", {class: "r num " + (r.change > 0 ? "up" : r.change < 0 ? "down" : ""), text: r.change == null ? "—" : (r.change > 0 ? "+" : "") + (r.change * 100).toFixed(1) + "%"}),
      h("td", null, h("span", {class: "chip " + liq[0], text: liq[1] + (r.liq_days != null ? " · " + G.days(r.liq_days) : ""),
        title: r.liq_days == null ? "Ушедших объявлений этой позиции меньше трёх" : "По " + r.liq_n + " ушедшим объявлениям"}),
        r.liq_rough ? h("span", {class: "small", text: " грубо", title: "Оценка грубая: ушедших объявлений меньше восьми"}) : null),
      h("td", {class: "r num small", text: r.per_index ? G.fmt(r.per_index) : "—", title: r.index ? "Индекс " + r.index + " (ориентировочно)" : "Индекса нет в таблице аналогов"}),
      h("td", null, spark(r.history)));
    tr.onclick = () => { G.list.restore("model=" + encodeURIComponent(r.key)); G.tab("list"); };
    return tr;
  }));
  if (!d.rows.length) $("m-rows").append(h("tr", null, h("td", {colspan: 15, class: "small", text: "Нет позиций хотя бы с тремя объявлениями."})));
  modelsBtn();
};
$("m-days").onchange = () => G.tabs.market(); $("m-cat").onchange = () => G.tabs.market();
$("m-compare").onclick = () => G.tab("compare");

// ---------- Сделки ----------
function dealRow(d, reload) {
  const inp = (type, value, key, conv) => {
    const el = h("input", {type, value: value == null ? "" : value, style: "width:" + (type === "date" ? 138 : type === "number" ? 100 : 160) + "px", "aria-label": key});
    el.onchange = async () => { try { await G.api("/api/deals", {id: d.id, [key]: conv ? conv(el.value) : el.value}); reload(); } catch (e) { alert(e.message); } };
    return el;
  };
  const numv = v => v === "" ? null : parseFloat(v);
  const u = G.safeUrl(d.url);
  return h("tr", null,
    h("td", null, h("div", {text: d.title, style: "max-width:260px;overflow-wrap:anywhere"}), h("div", {class: "small", text: d.item_key || ""}),
      u ? h("a", {class: "link", href: u, target: "_blank", rel: "noopener noreferrer", text: "объявление"}) : null),
    h("td", {class: "r"}, inp("number", d.buy_price, "buy_price", numv)),
    h("td", null, inp("date", tsToDate(d.bought_at), "bought_at", dateToTs)),
    h("td", {class: "r"}, inp("number", d.costs || 0, "costs", numv)),
    h("td", {class: "r"}, inp("number", d.sell_price, "sell_price", numv)),
    h("td", null, inp("date", tsToDate(d.sold_at), "sold_at", dateToTs)),
    h("td", {class: "r num", text: d.days == null ? "—" : Math.round(d.days) + (d.closed ? "" : " (идёт)")}),
    h("td", {class: "r num " + (d.net > 0 ? "p-strong" : d.net < 0 ? "up" : ""), text: d.net == null ? "—" : (d.net > 0 ? "+" : "") + G.fmt(d.net)}),
    h("td", {class: "r num", text: d.roi == null ? "—" : G.pct(d.roi)}),
    h("td", {class: "r num small", text: d.est_profit == null ? "—" : G.fmt(d.est_profit) + (d.est_gap == null ? "" : " / " + (d.est_gap > 0 ? "+" : "") + G.fmt(d.est_gap)),
      title: "Расчётная прибыль на момент покупки / отклонение фактической от неё"}),
    h("td", null, inp("text", d.note, "note")),
    h("td", null, h("button", {class: "link", type: "button", text: "Удалить", onclick: async () => { if (confirm("Удалить запись о сделке?")) { await G.api("/api/deals", {delete: d.id}); reload(); } }})));
}
G.tabs.deals = async function () {
  let d;
  try { d = await G.api("/api/deals?days=" + $("d-days").value); } catch (e) { $("d-rows").replaceChildren(); G.say("d-msg", e.message, false); return; }
  const s = d.summary;
  $("d-tiles").replaceChildren(
    tile("Сделок", G.num(s.count), "закрыто: " + s.closed + " · в работе: " + s.open),
    tile("Чистая прибыль", s.closed ? (s.net > 0 ? "+" : "") + G.fmt(s.net) : "—", s.roi == null ? "закрытых сделок нет" : "ROI " + G.pct(s.roi)),
    tile("Вложено в закрытые", G.fmt(s.invested), "заморожено в открытых: " + G.fmt(s.open_money)),
    tile("Средний оборот", s.avg_days == null ? "—" : Math.round(s.avg_days) + " дн.", "от покупки до продажи"),
    tile("Расчёт против факта", s.est_total == null ? "—" : G.fmt(s.est_total) + " → " + G.fmt(s.real_for_est),
      s.est_total == null ? "нет закрытых сделок с расчётом" : "по сделкам, созданным из лотов", "Сколько обещал расчёт и сколько получилось на деле"));
  $("d-rows").replaceChildren(...(d.deals.length ? d.deals.map(x => dealRow(x, G.tabs.deals))
    : [h("tr", null, h("td", {colspan: 12, class: "small", text: "Сделок пока нет. Нажмите «Куплено» в карточке лота или добавьте запись вручную ниже."}))]));
};
$("d-days").onchange = () => G.tabs.deals();
$("d-add").onclick = async () => {
  try { await G.api("/api/deals", {title: $("d-title").value, buy_price: $("d-buy").value, costs: $("d-costs").value});
    $("d-title").value = ""; $("d-buy").value = ""; G.say("d-msg", "Добавлено", true); G.tabs.deals(); }
  catch (e) { G.say("d-msg", e.message, false); }
};

// ---------- Статус ----------
async function loadStatus() {
  const s = await G.api("/api/status"), a = s.app;
  $("s-tiles").replaceChildren(
    tile("Последний обход", s.last.finished ? G.when(s.last.finished) : "ещё не было", s.last.sources ? "источников: " + s.last.sources : ""),
    tile("Просмотрено", G.num(s.last.seen), "новых: " + G.num(s.last.new)),
    tile("Отсеяно", G.num(s.last.rejected), "кандидатов: " + G.num(s.last.candidates)),
    tile("Капчи за 7 дней", G.num(s.week.captcha), "блокировок: " + G.num(s.week.blocked)),
    tile("Ошибки за 7 дней", G.num(s.week.errors), "обходов источников: " + G.num(s.week.sources)));
  $("s-line").textContent = a.status_line + (a.next_at ? " · следующий около " + G.when(a.next_at) : "");
  const sch = a.schedule;
  $("s-protect").textContent = sch ? "Запросов за последний час: " + a.requests_hour + " из " + a.requests_limit + ". "
    + (sch.slowed ? "Защита включена: интервалы увеличены в " + Math.round(sch.factor) + " раз(а), вернутся к норме после чистых циклов. " : "Защита от блокировок: в норме. ")
    + (sch.light ? "Лёгкий цикл: около " + G.when(sch.next_light_at) + ". " : "Лёгкий цикл выключен. ") + "Полный обход: около " + G.when(sch.next_full_at) + "." : "";
  $("s-pause").hidden = !a.desktop; $("s-pause").textContent = a.paused ? "Продолжить" : "Пауза";
  $("s-run").disabled = a.state === "running";
  $("s-modes").replaceChildren(...s.by_mode.map(m => h("tr", null,
    h("td", {text: m.title, title: G.MODE_TIPS[m.mode] || ""}), h("td", {class: "r num", text: G.num(m.viewed)}), h("td", {class: "r num", text: G.num(m.candidates)}),
    h("td", {class: "r num", text: m.candidates ? G.fmt(m.avg_profit) : "—"}),
    h("td", {class: "r num", text: m.inactive_share == null ? "ещё не проверялись" : G.pct(m.inactive_share) + " (" + m.inactive + " из " + m.checked + ")"}),
    h("td", {class: "small", text: Object.entries(m.rejected).slice(0, 4).map(([k, v]) => G.reason(k) + " " + v).join(", ") || "—"}))));
  $("s-cycles").replaceChildren(...(s.cycles.length ? s.cycles.map(c => h("tr", null,
    h("td", {class: "num", text: G.when(c.finished)}), h("td", {text: c.source}), h("td", {class: "r num", text: c.seen}),
    h("td", {class: "r num", text: c.new}), h("td", {class: "r num", text: c.rejected}), h("td", {class: "r num", text: c.candidates}),
    h("td", {class: "r num", text: c.captcha || ""}), h("td", {class: "small", text: c.error || ""})))
    : [h("tr", null, h("td", {colspan: 8, class: "small", text: "Обходов ещё не было."}))]));
  $("s-backups").textContent = s.backups.length ? "Копии базы (папка backups): " + s.backups.slice(0, 4).join(", ") + (s.backups.length > 4 ? " и ещё " + (s.backups.length - 4) : "") : "Копий базы пока нет.";
  try { $("s-log").textContent = (await G.api("/api/scan")).log || "—"; } catch (e) {}
  return a;
}
async function appAction(action, msgId) {
  try { const r = await G.api("/api/app", {action}); if (msgId) G.say(msgId, r.update ? r.update.message : "Готово", r.update ? r.update.ok : true); if (G.state.tab === "status") loadStatus(); return r; }
  catch (e) { if (msgId) G.say(msgId, e.message, false); }
}
$("s-pause").onclick = () => appAction($("s-pause").textContent === "Пауза" ? "pause" : "resume");
$("s-run").onclick = () => appAction("run_now", "s-msg");
G.tabs.status = function () { loadStatus().catch(() => {}); G.state.polling = setInterval(() => loadStatus().catch(() => {}), 7000); };

// ---------- Настройки ----------
let meta = {categories: [], regions: []};
function srcRow(s) {
  const sel = (items, val) => { const el = h("select", null, items.map(i => h("option", {value: i.key, text: i.title}))); el.value = val; return el; };
  const n = v => h("input", {type: "number", min: "0", step: "500", value: v || "", placeholder: "—"});
  const tr = h("tr", null,
    h("td", {class: "c"}, h("input", {type: "checkbox", checked: s.enabled, "aria-label": "Включён"})),
    h("td", {class: "c"}, h("input", {type: "checkbox", checked: s.priority, "aria-label": "Лёгкий цикл"})),
    h("td", null, h("input", {type: "text", value: s.name, "aria-label": "Название"})),
    h("td", null, sel(meta.categories, s.category)), h("td", null, sel(meta.regions, s.region)),
    h("td", {class: "c"}, h("input", {type: "checkbox", checked: s.delivery_only, "aria-label": "Только с доставкой"})),
    h("td", null, n(s.pmin)), h("td", null, n(s.pmax)),
    h("td", null, h("input", {type: "text", value: s.query, placeholder: "необязательно"})),
    h("td", null, h("input", {type: "number", min: "1", max: "20", value: s.max_pages, style: "width:64px"})),
    h("td", null, h("button", {class: "link", type: "button", text: "Удалить"})));
  tr.querySelector("button").onclick = () => tr.remove();
  return tr;
}
function readSources() {
  return [...$("src-rows").children].map(tr => {
    const c = [...tr.children].map(td => td.firstElementChild);
    return {enabled: c[0].checked, priority: c[1].checked, name: c[2].value, category: c[3].value, region: c[4].value, delivery_only: c[5].checked,
      pmin: +c[6].value || 0, pmax: +c[7].value || 0, query: c[8].value, max_pages: +c[9].value || 1};
  });
}
function showConfig(c) {
  $("tg-state").textContent = c.telegram.configured ? "Настроен: токен " + c.telegram.token_hint + ", chat id " + c.telegram.chat_id
    : c.telegram.token_set ? "Токен задан (" + c.telegram.token_hint + "), не хватает chat id." : "Не настроен — алерты идут только в журнал и уведомления Windows.";
  $("tg-chat").value = c.telegram.chat_id || ""; $("tg-token").value = "";
  $("sc-light").checked = c.light_cycle; $("sc-min").value = c.light_min_sec; $("sc-max").value = c.light_max_sec;
  $("sc-full").value = c.full_interval_sec; $("sc-limit").value = c.max_requests_per_hour;
  $("a-version").textContent = "Версия " + c.version.version + (c.version.commit ? " (" + c.version.commit + ")" : "") + (c.version.frozen ? ", собранное приложение" : ", запуск из папки проекта");
  if (c.analogs) $("a-version").textContent += " · " + c.analogs;
}
async function loadExcluded(list) {
  list = list || await G.api("/api/excluded");
  $("ex-list").replaceChildren(...(list.length ? list.map(x => h("span", {class: "chip"}, (x.kind === "seller" ? "продавец: " : "модель: ") + x.title,
    h("button", {type: "button", title: "Вернуть", "aria-label": "Вернуть " + x.title, text: "×", onclick: async () => loadExcluded(await G.api("/api/exclude", {kind: x.kind, value: x.value, on: false}))})))
    : [h("span", {class: "small", text: "Никого не скрывали. Скрыть продавца или модель можно из карточки лота."})]));
}
G.tabs.settings = async function () {
  try { const d = await G.api("/api/sources"); meta = d; $("src-rows").replaceChildren(...d.sources.map(srcRow));
    if (!d.saved) G.say("src-msg", "Показан набор по умолчанию — сохраните, чтобы закрепить.", true); } catch (e) { G.say("src-msg", e.message, false); }
  G.api("/api/config").then(showConfig).catch(() => {});
  loadExcluded().catch(() => {});
  G.api("/api/app").then(a => { $("a-autostart-row").hidden = !a.desktop; $("a-autostart").checked = a.autostart; $("a-home").textContent = "Папка данных: " + a.home; }).catch(() => {});
};
$("src-add").onclick = () => $("src-rows").append(srcRow({enabled: true, priority: false, name: "", category: "videokarty", region: "moskva",
  delivery_only: false, pmin: 0, pmax: 0, query: "", max_pages: 2}));
$("src-save").onclick = async () => {
  try { const d = await G.api("/api/sources", readSources()); $("src-rows").replaceChildren(...d.sources.map(srcRow)); G.say("src-msg", "Сохранено", true); }
  catch (e) { G.say("src-msg", e.message, false); }
};
$("tg-save").onclick = async () => {
  const tg = {chat_id: $("tg-chat").value}; if ($("tg-token").value.trim()) tg.token = $("tg-token").value;
  try { showConfig(await G.api("/api/config", {telegram: tg})); G.say("tg-msg", "Сохранено", true); } catch (e) { G.say("tg-msg", e.message, false); }
};
$("tg-test").onclick = async () => {
  G.say("tg-msg", "Отправляю…", true);
  try { const r = await G.api("/api/telegram_test", {}); G.say("tg-msg", r.ok ? "Тестовое сообщение отправлено — проверьте Telegram." : "Не получилось: " + r.message, r.ok); }
  catch (e) { G.say("tg-msg", e.message, false); }
};
$("sc-save").onclick = async () => {
  try { showConfig(await G.api("/api/config", {light_cycle: $("sc-light").checked, light_min_sec: $("sc-min").value, light_max_sec: $("sc-max").value,
    full_interval_sec: $("sc-full").value, max_requests_per_hour: $("sc-limit").value})); G.say("sc-msg", "Сохранено — применится со следующего цикла", true); }
  catch (e) { G.say("sc-msg", e.message, false); }
};
for (const [id, key, conv] of [["p-density", "density", String], ["p-font", "font", Number]])
  $(id).onchange = () => { G.view[key] = conv($(id).value); G.applyView(); G.saveView(); };
$("a-autostart").onchange = () => appAction($("a-autostart").checked ? "autostart_on" : "autostart_off", "a-msg");
$("a-update").onclick = () => { G.say("a-msg", "Проверяю…", true); appAction("check_updates", "a-msg"); };

// ---------- запуск ----------
(async function init() {
  try {
    const p = await G.api("/api/prefs");
    if (p["ui.view"]) { Object.assign(G.view, p["ui.view"]); G.normCols(); G.applyView(); }
    if (p["ui.profiles"]) G.profiles = p["ui.profiles"];
    if (p["ui.compare"] && Array.isArray(p["ui.compare"].ids)) { G.cmp = p["ui.compare"].ids.slice(0, 6); G.compare.ref = p["ui.compare"].ref || G.compare.ref; }
  } catch (e) {}
  let start = "today";
  try { start = localStorage.getItem("gpuflip-tab") || "today"; } catch (e) {}
  if (location.hash.length > 1) start = "list";
  G.cmpChanged();
  G.list.facets().catch(() => {});
  G.tab(start);
  setInterval(() => G.list.facets().catch(() => {}), 60000);
})();
})();
