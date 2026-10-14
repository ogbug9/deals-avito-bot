"use strict";
// Боковая панель лота: галерея, действия, график цены, причины, расчёт, похожие лоты, аналоги, чек-лист.
(function () {
const h = G.h, svg = G.svg;
let current = null, seq = 0;

// Графики рисуются под реальную ширину контейнера (шрифт не мельчает), перерисовываются при изменении размера,
// по щелчку «⤢» открываются крупно, при наведении показывают значение ближайшей точки.
G.chartBox = function (draw, legend, label) {
  const box = h("div", {class: "chartbox"});
  const area = h("div", {class: "chartarea"});
  const tip = h("div", {class: "charttip", hidden: true});
  const big = h("button", {class: "btn small chartbig", type: "button", text: "⤢", title: "Открыть крупно", onclick: () => G.chartZoom(draw, legend, label)});
  box.append(big, area, tip, legend || "");
  let lastW = 0;
  const paint = () => {
    const w = Math.round(area.clientWidth);
    if (!w || Math.abs(w - lastW) < 4) return;
    lastW = w;
    area.replaceChildren(draw(w, Math.max(170, Math.min(320, Math.round(w * 0.42))), tip));
  };
  new ResizeObserver(paint).observe(area);
  requestAnimationFrame(paint);
  return box;
};
G.chartZoom = function (draw, legend, label) {
  let dlg = $("chart-dlg");
  if (!dlg) { dlg = h("dialog", {id: "chart-dlg", class: "chartdlg"}); document.body.append(dlg); dlg.onclick = e => { if (e.target === dlg) dlg.close(); }; }
  const W = Math.min(1100, Math.round(window.innerWidth / (G.view.zoom || 1) * 0.86)), H = Math.round(Math.min(520, window.innerHeight * 0.6));
  const tip = h("div", {class: "charttip", hidden: true});
  dlg.replaceChildren(h("div", {class: "toolbar"}, h("h2", {text: label || "График"}),
    h("button", {class: "btn small push", type: "button", text: "Закрыть ✕", onclick: () => dlg.close()})),
    h("div", {class: "chartarea", style: "position:relative"}, draw(W, H, tip), tip), legend ? legend.cloneNode(true) : "");
  dlg.style.maxWidth = (W + 48) + "px";
  dlg.showModal();
};
function hover(g, tip, pts, fmt) {          // подсказка: ближайшая по горизонтали точка
  g.addEventListener("pointermove", e => {
    const r = g.getBoundingClientRect(), vb = g.viewBox.baseVal, x = (e.clientX - r.left) / r.width * vb.width;
    let best = null;
    for (const p of pts) if (!best || Math.abs(p.x - x) < Math.abs(best.x - x)) best = p;
    if (!best) return;
    tip.hidden = false; tip.textContent = fmt(best);
    tip.style.left = Math.min(r.width - 150, Math.max(0, best.x / vb.width * r.width + 8)) + "px";
    tip.style.top = Math.max(0, best.y / vb.height * r.height - 30) + "px";
  });
  g.addEventListener("pointerleave", () => { tip.hidden = true; });
}

function chart(d) {
  const a0 = d.price_history.map(p => [p.ts, p.price]);
  const b = d.market_history.map(m => [Date.parse(m.day + "T12:00:00") / 1000, m.median]);
  const a = a0.slice(); if (a.length) a.push([Date.now() / 1000, a[a.length - 1][1]]);
  const all = a.concat(b);
  if (all.length < 2) return h("div", {class: "small", text: "История ещё не накопилась: график появится после нескольких обходов."});
  const sc = d.score || {};
  const draw = (W, H, tip) => {
    const fs = 12, Lp = 64, R = 12, T = 10, B = 26;
    const x0 = Math.min(...all.map(p => p[0])), x1 = Math.max(...all.map(p => p[0]), x0 + 3600);
    const ys = all.map(p => p[1]).concat([sc.quick_price, sc.comfort_price].filter(Boolean));
    let y0 = Math.min(...ys), y1 = Math.max(...ys);
    const pad = (y1 - y0) * 0.15 || y1 * 0.1 || 1; y0 = Math.max(0, y0 - pad); y1 += pad;
    const X = t => Lp + (t - x0) / (x1 - x0) * (W - Lp - R), Y = v => T + (1 - (v - y0) / (y1 - y0)) * (H - T - B);
    const step = pts => pts.map((p, i) => (i ? "H" + X(p[0]).toFixed(1) + "V" + Y(p[1]).toFixed(1) : "M" + X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1))).join("");
    const line = pts => pts.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1)).join("");
    const g = svg("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img", "aria-label": "Цена объявления и медиана позиции"});
    const text = (x, y, s, anchor, fill) => { const t = svg("text", {x, y, "text-anchor": anchor, "font-size": fs, fill: fill || "var(--muted)"}); t.textContent = s; g.append(t); };
    for (let i = 0; i <= 4; i++) {
      const v = y0 + (y1 - y0) * i / 4, y = Y(v);
      g.append(svg("line", {x1: Lp, x2: W - R, y1: y, y2: y, stroke: "var(--line)", "stroke-width": 1}));
      text(Lp - 8, y + 4, Math.round(v).toLocaleString("ru-RU"), "end");
    }
    const ticks = Math.max(2, Math.min(6, Math.floor(W / 140)));
    for (let i = 0; i < ticks; i++) {
      const t = x0 + (x1 - x0) * i / (ticks - 1);
      text(X(t), H - 8, new Date(t * 1000).toLocaleDateString("ru-RU", {day: "numeric", month: "short"}), i === 0 ? "start" : i === ticks - 1 ? "end" : "middle");
    }
    for (const [v, name, col] of [[sc.quick_price, "быстрая продажа", "var(--good)"], [sc.comfort_price, "спокойная", "var(--accent)"]]) {
      if (!v) continue;
      g.append(svg("line", {x1: Lp, x2: W - R, y1: Y(v), y2: Y(v), stroke: col, "stroke-width": 1.5, "stroke-dasharray": "5 4", opacity: .85}));
      text(W - R - 4, Y(v) - 5, name + " " + G.fmt(v), "end", col);
    }
    const pts = [];
    if (b.length) { g.append(svg("path", {d: line(b), fill: "none", stroke: "var(--series-b)", "stroke-width": 2.5}));
      for (const p of b) { g.append(svg("circle", {cx: X(p[0]), cy: Y(p[1]), r: 3.5, fill: "var(--series-b)"})); pts.push({x: X(p[0]), y: Y(p[1]), t: p[0], v: p[1], s: "медиана"}); } }
    if (a.length) { g.append(svg("path", {d: step(a), fill: "none", stroke: "var(--series-a)", "stroke-width": 2.5}));
      for (const p of a.slice(0, -1)) { g.append(svg("circle", {cx: X(p[0]), cy: Y(p[1]), r: 3.5, fill: "var(--series-a)"})); pts.push({x: X(p[0]), y: Y(p[1]), t: p[0], v: p[1], s: "цена лота"}); } }
    if (tip) hover(g, tip, pts, p => p.s + ": " + G.fmt(p.v) + " · " + new Date(p.t * 1000).toLocaleDateString("ru-RU", {day: "numeric", month: "short"}));
    return g;
  };
  const legend = h("div", {class: "legend"},
    h("span", null, h("i", {style: "background:var(--series-a)"}), "цена объявления"),
    h("span", null, h("i", {style: "background:var(--series-b)"}), "медиана позиции"),
    sc.quick_price ? h("span", null, h("i", {style: "background:var(--good)"}), "быстрая продажа") : null,
    sc.comfort_price ? h("span", null, h("i", {style: "background:var(--accent)"}), "спокойная") : null);
  return G.chartBox(draw, legend, "Цена и медиана — " + (d.item_key || d.title));
}

// Разброс цен позиции: точки — объявления, полоса — средняя половина цен, черта — медиана, ромб — этот лот.
function spread(s) {
  if (s.prices.length < 3) return null;
  const q = (arr, f) => { const i = (arr.length - 1) * f, lo = Math.floor(i); return arr[lo] + (arr[Math.ceil(i)] - arr[lo]) * (i - lo); };
  const p25 = q(s.prices, .25), p75 = q(s.prices, .75);
  const draw = (W, H0, tip) => {
    const H = Math.max(84, Math.min(120, Math.round(H0 * 0.5))), P = 16, fs = 12, mid = H / 2 - 6;
    const lo = s.prices[0], hi = s.prices[s.prices.length - 1], span = hi - lo || 1;
    const X = v => P + (v - lo) / span * (W - 2 * P);
    const g = svg("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img", "aria-label": "Разброс цен позиции и место этого лота"});
    g.append(svg("rect", {x: X(p25), y: mid - 14, width: Math.max(2, X(p75) - X(p25)), height: 28, rx: 6, fill: "var(--sel)", opacity: .8}));
    g.append(svg("line", {x1: P, x2: W - P, y1: mid, y2: mid, stroke: "var(--line)", "stroke-width": 2}));
    const pts = [];
    s.prices.forEach((p, i) => { const cx = X(p), cy = mid + ((i % 3) - 1) * 6; g.append(svg("circle", {cx, cy, r: 3.5, fill: "var(--muted)", opacity: .6})); pts.push({x: cx, y: cy, v: p}); });
    if (s.median != null) g.append(svg("line", {x1: X(s.median), x2: X(s.median), y1: mid - 20, y2: mid + 20, stroke: "var(--series-b)", "stroke-width": 2.5}));
    if (s.price) { const x = X(Math.max(lo, Math.min(hi, s.price))); g.append(svg("path", {d: "M" + x + "," + (mid - 13) + " l8,13 l-8,13 l-8,-13 z", fill: "var(--series-a)", stroke: "var(--card)", "stroke-width": 1.5})); }
    for (const [v, anchor, x] of [[lo, "start", P], [hi, "end", W - P]]) {
      const t = svg("text", {x, y: H - 6, "text-anchor": anchor, "font-size": fs, fill: "var(--muted)"}); t.textContent = Math.round(v).toLocaleString("ru-RU") + " ₽"; g.append(t);
    }
    if (tip) hover(g, tip, pts, p => G.fmt(p.v));
    return g;
  };
  const legend = h("div", {class: "legend"},
    h("span", null, h("i", {style: "background:var(--series-a);height:9px;width:9px;transform:rotate(45deg)"}), "этот лот"),
    h("span", null, h("i", {style: "background:var(--series-b)"}), "медиана " + G.fmt(s.median)),
    h("span", null, h("i", {style: "background:var(--sel);height:9px"}), "половина цен: " + G.fmt(p25) + " – " + G.fmt(p75)),
    h("span", {text: "всего " + s.n + " объявлений"}));
  return G.chartBox(draw, legend, "Разброс цен — " + s.key);
}

// «Линейка»: соседи по серии выше и ниже, с ценами и ссылками на самый дешёвый живой лот.
function ladderSection(d) {
  const L = d.ladder;
  if (!L || !L.items || L.items.length < 2) return null;
  const name = k => k.replace(/^(CPU|RAM|MB|PSU) /, "");
  const rows = L.items.map(x => {
    const c = x.cheapest, u = c ? G.safeUrl(c.url) : null;
    return h("tr", {class: x.current ? "cur" : ""},
      h("td", null, h("span", {text: name(x.key)}), x.key === L.best_value ? h("span", {class: "star", text: " ★", title: "Дешевле всех за единицу производительности"}) : null,
        x.index ? h("span", {class: "small", text: " · " + x.index + "¹"}) : null),
      h("td", {class: "r num", text: G.fmt(x.median), title: x.n + " объявлений²"}),
      h("td", {class: "r num small", text: x.quick ? G.fmt(x.quick) : "—", title: "Быстрая продажа"}),
      h("td", {class: "r num " + (x.current ? "small" : x.diff < 0 ? "down" : "up"), text: x.current ? "эта" : (x.diff > 0 ? "+" : "−") + Math.abs(Math.round(x.diff)).toLocaleString("ru-RU")}),
      h("td", {class: "r"}, c ? h("span", {class: "ladder-act"},
          u ? h("a", {class: "link num", href: u, target: "_blank", rel: "noopener noreferrer", text: G.fmt(c.price) + " ↗", title: "Самый дешёвый живой лот³: " + c.title}) : h("span", {class: "num", text: G.fmt(c.price)}),
          h("button", {class: "link", type: "button", text: "все", title: "Показать все объявления этой позиции", onclick: () => { G.list.restore("model=" + encodeURIComponent(x.key)); G.list.reload(); G.tab("list"); }}))
        : h("span", {class: "small", text: "—"})));
  });
  return h("div", null, h("h3", {text: "Линейка: что выше и ниже — " + L.label}),
    h("div", {class: "tablewrap"}, h("table", {class: "t ladder"},
      h("thead", null, h("tr", null, h("th", {text: "Модель"}), h("th", {class: "r", text: "Медиана"}), h("th", {class: "r", text: "Быстро"}),
        h("th", {class: "r", text: "Разница"}), h("th", {class: "r", text: "Самый дешёвый"}))),
      h("tbody", null, rows))),
    h("ol", {class: "notes"},
      L.kind === "index" ? h("li", {text: "Индекс производительности: RTX 3060 12GB = 100. Ориентировочный, из words\\analogs.json — проверьте по тестам."}) : h("li", {text: "Порядок — по медиане цены внутри поколения / типа."}),
      h("li", {text: "Медиана — запросные цены живых объявлений за 14 дней, без мусора и брака."}),
      h("li", {text: "Самый дешёвый — не дешевле 60% медианы: явные приманки и цены-заглушки скрыты."})));
}

// Калькулятор торга: двигаешь цену покупки — сразу видно чистую прибыль и цену, ниже которой не уйти в минус.
function bargain(d) {
  const sc = d.score, e = d.econ;
  if (!sc || !sc.quick_price || !d.price || !e) return null;
  const k = 1 + e.capital * e.days + Math.min(0.6, e.risk_p) * e.risk_share;
  const net = (buy, sell) => sell * (1 - e.fee) - buy - e.fixed - buy * (k - 1);
  const breakeven = (sc.quick_price * (1 - e.fee) - e.fixed) / k;
  const min = Math.round(d.price * 0.5 / 100) * 100, max = Math.round(d.price);
  const start = Math.round(sc.target_buy || d.price * (1 - e.bargain));
  const out = h("div", {class: "calc-out"}), inp = h("input", {type: "range", min, max, step: 100, value: start, "aria-label": "Цена покупки"});
  const show = () => {
    const b = Number(inp.value), q = net(b, sc.quick_price), c = sc.comfort_price ? net(b, sc.comfort_price) : null;
    out.replaceChildren(
      h("div", null, h("span", {class: "small", text: "Купить за "}), h("b", {class: "num", text: G.fmt(b)}),
        h("span", {class: "small", text: " (" + (b < d.price ? "−" + G.pct(1 - b / d.price) + " к цене" : "без торга") + ")"})),
      h("div", null, h("span", {class: "small", text: "Чистыми при быстрой продаже: "}), h("b", {class: "num " + (q > 0 ? "p-strong" : "up"), text: G.fmt(q) + " (" + G.pct(q / b) + ")"})),
      c != null ? h("div", null, h("span", {class: "small", text: "При спокойной: "}), h("b", {class: "num", text: G.fmt(c)})) : null);
  };
  inp.oninput = show; show();
  return h("div", null, h("h3", {text: "Калькулятор торга"}), inp, out,
    h("div", {class: "small", text: "Безубыточная цена покупки — " + G.fmt(breakeven) + ": дороже неё сделка уходит в минус при быстрой продаже. "
      + "Учтены издержки " + G.fmt(e.fixed) + ", заморозка денег на " + Math.round(e.days) + " дн. и штраф за риск."}));
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

function pcSection(d) {
  const p = d.parts;
  if (!p) return null;
  const kv = (k, v, cls) => h("tr", {class: cls || ""}, h("td", {colspan: "2", text: k}), h("td", {class: "r num", text: v}), h("td"));
  const rows = p.items.map(x => h("tr", {class: x.value ? "" : "dim", title: x.note},
    h("td", null, h("i", {class: "dot k-" + x.kind}), " " + x.label),
    h("td", {text: G.partName(x) || "—"}),
    h("td", {class: "r num", text: x.value ? G.fmt(x.value) : "—"}),
    h("td", {class: "small", text: G.PART_STATUS[x.status] + (x.n && x.value ? ", n=" + x.n : "")})));
  const foot = p.value ? [
    kv("Оценённые детали (быстрая продажа)", G.fmt(p.value), "sum"),
    kv("Цена лота", G.fmt(d.price)),
    kv(p.diff >= 0 ? "Детали дороже лота на" : "До цены лота не хватает", G.fmt(Math.abs(p.diff)), p.diff >= 0 ? "gain" : ""),
    p.net != null ? kv("Чистыми при продаже по частям (≈ " + Math.round(p.days) + " дн.)", G.fmt(p.net), p.net > 0 ? "gain" : "") : null] : [];
  return h("div", null, h("h3", {text: "Что внутри ПК и сколько это стоит"}),
    p.value ? G.pcBar(p, d.price) : null,
    p.value ? h("div", {class: "legend"}, p.items.filter(x => x.value).map(x => h("span", null, h("i", {class: "dot k-" + x.kind}), " " + x.label.toLowerCase())),
      h("span", null, h("i", {class: "markdot"}), " цена лота")) : null,
    h("table", {class: "t pct"},
      h("thead", null, h("tr", null, h("th", {text: "Деталь"}), h("th", {text: "Что названо"}), h("th", {class: "r", text: "Быстро продать"}), h("th", {text: "Откуда цена"}))),
      h("tbody", null, rows), foot.length ? h("tfoot", null, foot) : null),
    p.warn ? h("div", {class: "note", text: "Осторожно: " + p.warn + "."}) : null,
    h("div", {class: "small", style: "margin-top:6px", text: "Сумма — нижняя граница: в неё входят только детали, названные в тексте и имеющие рынок в базе"
      + (p.missing.length ? "; не оценено: " + p.missing.join(", ") + ", корпус" : "; не оценён только корпус") + ". «Чистыми» — после торга, издержек на каждую деталь и риска."
      + (p.approx ? " Значок ≈ и «приблизительно» — оценка по цене за ГБ у похожих позиций или по малому числу объявлений." : "")
      + (d.deep_at ? "" : " Описание в выдаче обрезано: после чтения карточки разбор станет полнее.")}));
}

function render(d) {
  const p = $("panel"), u = G.safeUrl(d.url), sc = d.score, tr = G.trust(sc);
  const rows = [];
  const add = (k, v) => rows.push(h("tr", null, h("td", {text: k}), h("td", null, v)));
  if (sc && sc.market_n) {
    add("Рынок позиции", G.fmt(sc.median_ask) + " (n=" + sc.market_n + ")" + (sc.precise ? " — точная позиция" : ""));
    if (sc.discount_vs_market != null && sc.market_n >= 6) add("Цена к рынку", (sc.discount_vs_market > 0 ? "ниже на " : "выше на ") + G.pct(Math.abs(sc.discount_vs_market)));
    if (sc.quick_price) add("Быстрая продажа (≈ неделя)", G.fmt(sc.quick_price) + " — " + sc.basis);
    if (sc.comfort_price) add("Спокойная продажа (2–4 недели)", G.fmt(sc.comfort_price) + (sc.comfort_net ? ", чистыми " + G.fmt(sc.comfort_net) : ""));
    if (sc.sold_n) add("Ушедшие лоты позиции", sc.sold_n + " шт." + (sc.sold_median ? ", медиана цены " + G.fmt(sc.sold_median) : ""));
    if (sc.ceiling_key) add("Сильнее и дешевле", sc.ceiling_key + " — " + G.fmt(sc.ceiling));
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
  add("Состояние", d.avito_condition || (d.cond === "new" ? "по тексту похоже на новое" : "по тексту б/у") + " (карточка не читалась)");
  if (d.junk) add("Мусор", d.junk_title);
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
  p.replaceChildren(...[      // пустые блоки отбрасываем: replaceChildren(null) напечатал бы слово «null»
    G.panelBar(d.item_key || d.category_title),
    h("h2", {text: d.title, style: "overflow-wrap:anywhere"}),
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
    pcSection(d),
    h("h3", {text: "Описание"}), h("div", {style: "white-space:pre-line;overflow-wrap:anywhere", text: d.description || "—"}),
    h("h3", {text: "Цена и медиана"}), chart(d),
    h("h3", {text: "Расчёт"}), h("table", {class: "kv"}, h("tbody", null, rows)),
    bargain(d),
    sc && sc.why && sc.why.length ? h("div", null, h("h3", {text: "Как посчитана цена продажи"}),
      h("ul", {class: "reasons"}, sc.why.map(t => h("li", {text: t})))) : null,
    sim && sim.key ? h("div", null, h("h3", {text: "Похожие лоты — " + sim.key}), spread(sim),
      sim.cheaper_than_share != null ? h("div", {class: "small", text: "Этот лот дешевле " + G.pct(sim.cheaper_than_share) + " объявлений позиции."}) : null,
      sim.items.map(x => h("div", {class: "simrow"},
        h("span", {class: "grow", text: x.title, title: x.title}),
        h("span", {class: "chip " + (x.label === "дешевле рынка" ? "good" : x.label === "дороже рынка" ? "" : ""), text: x.label}),
        h("span", {class: "num", text: G.fmt(x.price)}),
        h("span", {class: "num small", style: "width:74px;text-align:right", text: (x.vs_this > 0 ? "+" : "") + Math.round(x.vs_this).toLocaleString("ru-RU")}),
        G.safeUrl(x.url) ? h("a", {class: "link", href: x.url, target: "_blank", rel: "noopener noreferrer", text: "↗", title: "Открыть на Авито"}) : null)),
      sim.items.length ? null : h("div", {class: "small", text: "Других объявлений этой позиции в базе нет."})) : null,
    ladderSection(d),
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
      h("button", {class: "link", type: "button", text: d.model_hidden ? "Модель скрыта" : "Скрыть модель", onclick: () => G.act.hide("model", current)}))].filter(x => x != null && x !== false));
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
