"use strict";
// Живой индикатор работы в шапке и панель «Сбор» во вкладке «Статус»: что делается сейчас, прогресс добора
// по разделам, выбор разделов для добора, кнопки управления. Опрос /api/app: часто, пока идёт работа, реже — в простое.
(function () {
const h = G.h;
let timer = null, cats = [], last = null;

const JOB_RU = {collect: "Добор", full: "Полный обход", light: "Лёгкий цикл", scan: "Обход", analysis: "Анализ"};

function describe(a) {
  const p = a.progress || {};
  if (p.job && p.captcha) return {cls: "bad", text: "Нужна капча — пройдите её в окне Chrome сборщика", tip: "Сбор ждёт до 3 минут, потом остановится"};
  if (p.job) {
    const bits = [p.title || JOB_RU[p.job] || "Работаю"];
    if (p.source) bits.push(p.source);
    if (p.page && p.job !== "analysis") bits.push("стр. " + p.page);
    if (p.step && p.of && p.job !== "collect") bits.push(p.step + " из " + p.of);
    if (p.new) bits.push("новых " + p.new);
    return {cls: p.job === "analysis" ? "info" : "run", text: bits.join(" · "), tip: (p.own ? "" : "Отдельный процесс (команда collect). ") + "Начато " + G.when(p.started)};
  }
  if (a.job_pending) return {cls: "run", text: "Добор поставлен в очередь — начнётся через несколько секунд", tip: ""};
  if (a.paused) return {cls: "idle", text: "Сбор на паузе", tip: "Включить — вкладка «Статус»"};
  const next = a.next_at ? "следующий обход около " + new Date(a.next_at * 1000).toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"}) : "";
  const tail = p.summary && p.finished && Date.now() / 1000 - p.finished < 3600 ? "последний: " + p.summary : a.status_line || "";
  return {cls: a.last_rc === 3 ? "warn" : "idle", text: [tail, next].filter(Boolean).join(" · ") || "Ожидание", tip: a.status_line || ""};
}

function renderHeader(a) {
  const d = describe(a), el = $("activity");
  if (!el) return;
  el.className = "activity " + d.cls;
  $("act-text").textContent = d.text;
  el.title = d.tip;
  $("actbar").hidden = !(a.progress && a.progress.job);
  $("actbar").className = "actbar" + (a.progress && a.progress.captcha ? " bad" : "");
}

function renderLive(a) {
  const box = $("s-live");
  if (!box) return;
  const p = a.progress || {};
  const parts = [h("div", {class: "live-head"}, h("i", {class: "pulse " + describe(a).cls}), h("b", {text: describe(a).text}))];
  if (p.job && p.started) parts.push(h("div", {class: "small", text: "Идёт " + G.days((Date.now() / 1000 - p.started) / 86400).replace("меньше дня", Math.max(1, Math.round((Date.now() / 1000 - p.started) / 60)) + " мин.")}));
  if (p.cats && p.target) {
    for (const [k, n] of Object.entries(p.cats)) {
      const c = cats.find(x => x.key === k), share = Math.min(1, n / p.target);
      parts.push(h("div", {class: "catbar"}, h("span", {class: "name", text: c ? c.title : k}),
        h("span", {class: "cbar"}, h("i", {style: "width:" + (share * 100).toFixed(1) + "%"})),
        h("span", {class: "num small", text: G.num(n) + " / " + G.num(p.target)})));
    }
  }
  box.replaceChildren(...parts);
  const running = !!(p.job || a.job_pending);
  if ($("s-collect")) { $("s-collect").disabled = running && p.job === "collect"; $("s-stopjob").hidden = !(p.job === "collect" && p.own); }
  if ($("s-analyse")) $("s-analyse").disabled = p.job === "analysis";
}

function renderCats() {
  const box = $("s-cats");
  if (!box || box.dataset.ready) return;
  box.dataset.ready = "1";
  const keep = (() => { try { return JSON.parse(localStorage.getItem("gpuflip-collect-cats") || "null"); } catch (e) { return null; } })();
  box.replaceChildren(...cats.map(c => {
    const cb = h("input", {type: "checkbox", value: c.key, checked: keep ? keep.includes(c.key) : ["pc", "protsessory", "materinskie_platy", "operativnaya_pamyat"].includes(c.key)});
    cb.onchange = () => { try { localStorage.setItem("gpuflip-collect-cats", JSON.stringify([...box.querySelectorAll("input:checked")].map(x => x.value))); } catch (e) {} };
    return h("label", {class: "chk"}, cb, h("span", {text: c.title}), h("span", {class: "n num", text: G.num(c.n)}));
  }));
}

async function poll() {
  clearTimeout(timer);
  let a = null;
  try { a = await G.api("/api/app"); } catch (e) { timer = setTimeout(poll, 10000); return; }
  last = a;
  renderHeader(a);
  if (G.state.tab === "status") {
    renderLive(a);
    if (a.progress && a.progress.job) G.api("/api/scan").then(s => { if ($("s-log")) $("s-log").textContent = s.log || "—"; }).catch(() => {});
  }
  timer = setTimeout(poll, a.progress && a.progress.job ? 2500 : 8000);
}
G.activity = {poll, last: () => last};

async function act(action, extra, okText) {
  try {
    const r = await G.api("/api/app", Object.assign({action}, extra || {}));
    G.say("s-msg", okText || "Готово", true);
    renderHeader(r); renderLive(r); poll();
  } catch (e) { G.say("s-msg", e.message, false); }
}
G.api("/api/facets").then(f => { cats = f.categories || []; renderCats(); }).catch(() => {});
document.addEventListener("click", e => {
  const id = e.target && e.target.id;
  if (id === "s-collect") {
    const sel = [...document.querySelectorAll("#s-cats input:checked")].map(x => x.value);
    if (!sel.length) return G.say("s-msg", "Отметьте хотя бы один раздел", false);
    act("collect", {cats: sel, target: Number($("s-target").value) || 1000}, "Добор запущен: " + sel.length + " разд.");
  } else if (id === "s-stopjob") act("stop_job", null, "Останавливаю после текущей страницы");
  else if (id === "s-analyse") act("analyse", null, "Анализ начнётся через несколько секунд");
});
const prev = G.tabs.status;
G.tabs.status = function () { prev(); renderCats(); if (last) renderLive(last); poll(); };
poll();
})();
