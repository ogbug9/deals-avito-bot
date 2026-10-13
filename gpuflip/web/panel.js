"use strict";
// Боковая панель лота: галерея, действия, график цены, причины, расчёт, похожие лоты, аналоги, чек-лист.
(function () {
const h = G.h, svg = G.svg;
let current = null, seq = 0;

function chart(d) {
  const W = 420, H = 150, Lp = 52, R = 10, T = 8, B = 22;
  const a = d.price_history.map(p => [p.ts, p.price]);
  const b = d.market_history.map(m => [Date.parse(m.day + "T12:00:00") / 1000, m.median]);
  if (a.length) a.push([Date.now() / 1000, a[a.length - 1][1]]);          // цена держится до сегодняшнего дня
  const all = a.concat(b);
  if (all.length < 2) return h("div", {class: "small", text: "История ещё не накопилась: график появится после нескольких обходов."});
  const x0 = Math.min(...all.map(p => p[0])), x1 = Math.max(...all.map(p => p[0]), x0 + 3600);
  let y0 = Math.min(...all.map(p => p[1])), y1 = Math.max(...all.map(p => p[1]));
  const pad = (y1 - y0) * 0.15 || y1 * 0.1 || 1; y0 = Math.max(0, y0 - pad); y1 += pad;
  const X = t => Lp + (t - x0) / (x1 - x0) * (W - Lp - R), Y = v => T + (1 - (v - y0) / (y1 - y0)) * (H - T - B);
  const step = pts => pts.map((p, i) => (i ? "H" + X(p[0]).toFixed(1) + "V" + Y(p[1]).toFixed(1) : "M" + X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1))).join("");
  const line = pts => pts.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1)).join("");
  const g = svg("svg", {viewBox: "0 0 " + W + " " + H, width: "100%", role: "img", "aria-label": "Цена объявления и медиана позиции"});
  for (let i = 0; i <= 3; i++) {
    const v = y0 + (y1 - y0) * i / 3, y = Y(v);
    g.append(svg("line", {x1: Lp, x2: W - R, y1: y, y2: y, stroke: "var(--line)", "stroke-width": 1}));
    const t = svg("text", {x: Lp - 6, y: y + 4, "text-anchor": "end", "font-size": 11, fill: "var(--muted)"}); t.textContent = Math.round(v).toLocaleString("ru-RU"); g.append(t);
  }
  for (const [t, anchor] of [[x0, "start"], [x1, "end"]]) {
    const tx = svg("text", {x: X(t), y: H - 6, "text-anchor": anchor, "font-size": 11, fill: "var(--muted)"});
    tx.textContent = new Date(t * 1000).toLocaleDateString("ru-RU", {day: "numeric", month: "short"}); g.append(tx);
  }
  if (b.length) { g.append(svg("path", {d: line(b), fill: "none", stroke: "var(--series-b)", "stroke-width": 2}));
    for (const p of b) g.append(svg("circle", {cx: X(p[0]), cy: Y(p[1]), r: 3, fill: "var(--series-b)"})); }
  if (a.length) { g.append(svg("path", {d: step(a), fill: "none", stroke: "var(--series-a)", "stroke-width": 2}));
    for (const p of a.slice(0, -1)) g.append(svg("circle", {cx: X(p[0]), cy: Y(p[1]), r: 3, fill: "var(--series-a)"})); }
  return h("div", null, g, h("div", {class: "legend"},
    h("span", null, h("i", {style: "background:var(--series-a)"}), "цена объявления"),
    h("span", null, h("i", {style: "background:var(--series-b)"}), "медиана позиции")));
}

// Разброс цен позиции: каждая точка — объявление, вертикальная черта — медиана, ромб — этот лот.
function spread(s) {
  if (s.prices.length < 3) return null;
  const W = 420, H = 56, P = 12, lo = s.prices[0], hi = s.prices[s.prices.length - 1], span = hi - lo || 1;
  const X = v => P + (v - lo) / span * (W - 2 * P);
  const g = svg("svg", {viewBox: "0 0 " + W + " " + H, width: "100%", role: "img", "aria-label": "Разброс цен позиции и место этого лота"});
  g.append(svg("line", {x1: P, x2: W - P, y1: 24, y2: 24, stroke: "var(--line)", "stroke-width": 2}));
  s.prices.forEach((p, i) => g.append(svg("circle", {cx: X(p), cy: 24 + ((i % 3) - 1) * 5, r: 3, fill: "var(--muted)", opacity: .55})));
  if (s.median != null) g.append(svg("line", {x1: X(s.median), x2: X(s.median), y1: 8, y2: 40, stroke: "var(--series-b)", "stroke-width": 2}));
  if (s.price) { const x = X(Math.max(lo, Math.min(hi, s.price))); g.append(svg("path", {d: "M" + x + ",13 l7,11 l-7,11 l-7,-11 z", fill: "var(--series-a)"})); }
  for (const [v, anchor, x] of [[lo, "start", P], [hi, "end", W - P]]) {
    const t = svg("text", {x, y: H - 2, "text-anchor": anchor, "font-size": 11, fill: "var(--muted)"}); t.textContent = Math.round(v).toLocaleString("ru-RU") + " ₽"; g.append(t);
  }
  return h("div", null, g, h("div", {class: "legend"},
    h("span", null, h("i", {style: "background:var(--series-a);height:9px;width:9px;transform:rotate(45deg)"}), "этот лот"),
    h("span", null, h("i", {style: "background:var(--series-b)"}), "медиана " + G.fmt(s.median)),
    h("span", {text: "всего " + s.n + " объявлений"})));
}

function gallery(d) {
  const photos = (d.photos || []).filter(p => /^https:\/\//.test(p));
  if (!photos.length) return h("div", {class: "gallery"}, h("div", {class: "pic none", style: "width:100%;height:160px", text: "нет фото"}));
  let i = 0;
  const img = h("img", {src: photos[0], alt: "Фото лота", referrerpolicy: "no-referrer"});
  const thumbs = h("div", {class: "thumbs"});
  const show = n => { i = (n + photos.length) % photos.length; img.src = photos[i]; [...thumbs.children].forEach((t, k) => t.classList.toggle("on", k === i)); };
  photos.forEach((p, k) => thumbs.append(h("img", {src: p, alt: "Фото " + (k + 1), class: k ? "" : "on", referrerpolicy: "no-referrer", onclick: () => show(k)})));
  const box = h("div", {class: "gallery"}, img);
  if (photos.length > 1) box.append(h("button", {class: "nav prev", type: "button", "aria-label": "Предыдущее фото", text: "‹", onclick: () => show(i - 1)}),
    h("button", {class: "nav next", type: "button", "aria-label": "Следующее фото", text: "›", onclick: () => show(i + 1)}));
  return h("div", null, box, photos.length > 1 ? thumbs : null,
    d.deep_at ? null : h("div", {class: "small", text: "Показано фото из выдачи. Все фото и полное описание появятся после чтения карточки (лоты, прошедшие предфильтр по цене)."}));
}

function render(d) {
  const p = $("panel"), u = G.safeUrl(d.url), sc = d.score, tr = G.trust(sc);
  const rows = [];
  const add = (k, v) => rows.push(h("tr", null, h("td", {text: k}), h("td", null, v)));
  if (sc && sc.market_n) {
    add("Рынок позиции", G.fmt(sc.median_ask) + " (n=" + sc.market_n + ")" + (sc.precise ? " — точная позиция" : ""));
    if (sc.discount_vs_market != null && sc.market_n >= 6) add("Цена к рынку", (sc.discount_vs_market > 0 ? "ниже на " : "выше на ") + G.pct(Math.abs(sc.discount_vs_market)));
    if (sc.sell_price) add("Оценка продажи", G.fmt(sc.sell_price));
    if (sc.target_buy) add("Покупка после торга", G.fmt(sc.target_buy));
    if (sc.risk_loss) add("Штраф за риск", "−" + G.fmt(sc.risk_loss));
    if (sc.net != null && sc.target_buy) add("Чистыми после торга", G.fmt(sc.net) + " (" + G.pct(sc.net_pct) + ")");
    if (sc.ask_net != null && sc.target_buy) add("Чистыми без торга", G.fmt(sc.ask_net));
    if (sc.days_to_sell) add("Срок продажи в расчёте", Math.round(sc.days_to_sell) + " дн. — " + (sc.days_real ? "по " + sc.days_n + " ушедшим лотам" + (sc.days_n < 8 ? ", оценка грубая" : "") : "условный: данных об ушедших нет"));
    if (!sc.ok && sc.reason) add("Почему не кандидат", G.reason(sc.reason));
  }
  if (d.liquidity && d.liquidity.days != null) add("Ликвидность позиции", d.liquidity.label + ", ~" + G.days(d.liquidity.days) + (d.liquidity.rough ? " (оценка грубая, " + d.liquidity.n + " лотов)" : ""));
  if (d.price_drop) add("Цена снижена", "на " + G.fmt(d.price_drop) + " (" + G.pct(d.price_drop_pct) + "), было " + G.fmt(d.first_price));
  if (d.days_listed != null) add(d.gone ? "Прожило до снятия" : "Висит", G.days(d.gone && d.lifetime_days != null ? d.lifetime_days : d.days_listed));
  if (d.posted_at) add("Опубликовано (оценка)", G.when(d.posted_at));
  add("В базе с", G.when(d.first_seen));
  add("Последний раз в выдаче", G.when(d.last_seen) + (d.stale ? " — давно не видели" : ""));
  if (d.gone) add("Снято с публикации", G.when(d.gone_at) + ", последняя цена " + G.fmt(d.price));
  if (d.checked_at) add("Карточка проверена", G.when(d.checked_at));
  if (d.price_history && d.price_history.length > 1) add("Изменений цены", String(d.price_history.length - 1));
  const risks = Object.entries(d.flags).map(([k, v]) => (G.FLAGS[k] || k) + " — " + Math.round(v * 100) + "%");
  if (risks.length) add("Признаки риска", risks.join("; "));
  add("Состояние", d.avito_condition || "не проверено (карточка не читалась)");
  if (d.avito_model) add("Модель по Авито", d.avito_model);
  if (d.model_source && d.model_source !== "title") add("Модель определена", d.model_source === "fuzzy" ? "с исправлением опечатки" : "по описанию");
  add("Продавец", [d.seller_name, d.seller_type, d.rating != null ? "★ " + String(d.rating).replace(".", ",") + (d.reviews ? " (" + d.reviews + ")" : "") : null,
    d.seller_ads ? "лотов в базе: " + d.seller_ads : null].filter(Boolean).join(" · ") || "нет данных");

  const note = h("textarea", {rows: 2, placeholder: "Заметка", "aria-label": "Заметка"}); note.value = d.note || "";
  note.onchange = () => G.act.save(current, {note: note.value}).then(() => G.list.refreshRow(current));
  const st = h("select", {"aria-label": "Мой статус", style: "width:auto"}, h("option", {value: "", text: "Без статуса"}), Object.entries(G.STATUS).map(([k, v]) => h("option", {value: k, text: v})));
  st.value = d.user_status || ""; st.onchange = () => G.act.status(current, st.value);
  const inCmp = G.cmp.includes(d.id);

  const check = h("div", null, d.checklist_items.map(c => {
    const cb = h("input", {type: "checkbox", checked: !!d.checklist[c.key]});
    cb.onchange = () => { d.checklist[c.key] = cb.checked; G.act.save(current, {checklist: d.checklist}); };
    return h("label", {class: "chk"}, cb, c.label);
  }));

  const sim = d.similar, eq = d.equivalents;
  p.replaceChildren(
    h("button", {class: "btn small close", type: "button", text: "Закрыть ✕", title: "Закрыть панель (Esc)", onclick: G.panelClose}),
    h("h2", {text: d.title, style: "padding-right:110px;overflow-wrap:anywhere"}),
    h("div", {class: "meta", text: [d.city, d.date_text, d.category_title, d.item_key].filter(Boolean).join(" · ")}),
    h("div", {style: "display:flex;align-items:baseline;gap:12px;margin:8px 0"},
      h("span", {class: "price num", text: G.fmt(d.price)}),
      d.est_profit > 0 ? h("span", {class: "profit num " + G.profitClass(d.est_profit), text: "≈ +" + G.fmt(d.est_profit) + " (" + G.pct(d.est_profit_pct) + ")"}) : null,
      h("span", {class: "chip " + tr.cls, text: tr.text, title: tr.tip})),
    gallery(d),
    h("div", {class: "chips gap"}, G.badges(d)),
    h("div", {class: "chips gap"},
      u ? h("a", {class: "btn primary", href: u, target: "_blank", rel: "noopener noreferrer", text: "Открыть на Авито"}) : null,
      h("button", {class: "btn" + (d.fav ? " on" : ""), type: "button", text: d.fav ? "★ В избранном" : "☆ В избранное", onclick: () => G.act.fav(current)}),
      h("button", {class: "btn" + (inCmp ? " on" : ""), type: "button", text: inCmp ? "✓ В сравнении" : "В сравнение", onclick: () => { G.cmpToggle(d.id); render(d); }}),
      h("button", {class: "btn", type: "button", text: "Куплено", title: "Создать запись в журнале сделок", onclick: () => G.act.bought(current)}),
      st),
    d.reason ? h("div", null, h("h3", {text: "Почему это кандидат"}), h("ul", {class: "reasons"}, d.reason.split(" | ").map(r => h("li", {text: r})))) : null,
    h("h3", {text: "Описание"}), h("div", {style: "white-space:pre-line;overflow-wrap:anywhere", text: d.description || "—"}),
    h("h3", {text: "Цена и медиана"}), chart(d),
    h("h3", {text: "Расчёт"}), h("table", {class: "kv"}, h("tbody", null, rows)),
    sim && sim.key ? h("div", null, h("h3", {text: "Похожие лоты — " + sim.key}), spread(sim),
      sim.cheaper_than_share != null ? h("div", {class: "small", text: "Этот лот дешевле " + G.pct(sim.cheaper_than_share) + " объявлений позиции."}) : null,
      sim.items.map(x => h("div", {class: "simrow"},
        h("span", {class: "grow", text: x.title, title: x.title}),
        h("span", {class: "chip " + (x.label === "дешевле рынка" ? "good" : x.label === "дороже рынка" ? "" : ""), text: x.label}),
        h("span", {class: "num", text: G.fmt(x.price)}),
        h("span", {class: "num small", style: "width:74px;text-align:right", text: (x.vs_this > 0 ? "+" : "") + Math.round(x.vs_this).toLocaleString("ru-RU")}),
        G.safeUrl(x.url) ? h("a", {class: "link", href: x.url, target: "_blank", rel: "noopener noreferrer", text: "↗", title: "Открыть на Авито"}) : null)),
      sim.items.length ? null : h("div", {class: "small", text: "Других объявлений этой позиции в базе нет."})) : null,
    eq && eq.group ? h("div", null, h("h3", {text: "Эквиваленты дешевле"}),
      h("div", {class: "small", text: "Группа «" + eq.group + "». Индекс производительности ориентировочный — проверьте по своим источникам (words\\analogs.json)."}),
      eq.items.length ? eq.items.map(x => h("div", {class: "simrow"},
        h("span", {class: "grow", text: x.key}),
        h("span", {class: "small", text: x.index_diff == null ? "индекс не задан" : "индекс " + (x.index_diff > 0 ? "+" : "") + Math.round(x.index_diff * 100) + "%"}),
        h("span", {class: "num", text: G.fmt(x.median) + " (n=" + x.n + ")"}),
        x.saving ? h("span", {class: "chip good", text: "−" + G.fmt(x.saving)}) : null,
        h("button", {class: "link", type: "button", text: "показать", onclick: () => { G.list.restore("model=" + encodeURIComponent(x.key)); G.tab("list"); }})))
        : h("div", {class: "small gap", text: "Моделей той же группы с медианой ниже в базе нет."})) : null,
    h("h3", {text: "Проверка на месте"}), check,
    h("h3", {text: "Заметка"}), note,
    h("div", {class: "chips gap"},
      h("button", {class: "link", type: "button", text: d.seller_hidden ? "Продавец скрыт" : "Скрыть продавца", onclick: () => G.act.hide("seller", current)}),
      h("button", {class: "link", type: "button", text: d.model_hidden ? "Модель скрыта" : "Скрыть модель", onclick: () => G.act.hide("model", current)})));
}

G.panelOpen = async function (it) {
  const my = ++seq;
  current = it;
  $("panel").hidden = false; $("main").classList.add("with-panel");
  try {
    const d = await G.api("/api/listing?id=" + encodeURIComponent(it.id));
    if (my !== seq) return;
    Object.assign(it, {fav: d.fav, user_status: d.user_status, note: d.note});
    render(d); $("panel").scrollTop = 0;
  } catch (e) { $("panel").replaceChildren(h("button", {class: "btn small", type: "button", text: "Закрыть", onclick: G.panelClose}), h("div", {class: "note", text: e.message})); }
};
G.panelRefresh = function (it) { if (current && !$("panel").hidden && current.id === it.id) G.panelOpen(it); };
G.panelClose = function () { $("panel").hidden = true; $("main").classList.remove("with-panel"); current = null; seq++; };
})();
