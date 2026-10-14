"use strict";
// Раскладка: ширина фильтров и панели лота тянется мышью, фильтры сворачиваются, панель отстёгивается в окно,
// масштаб интерфейса — Ctrl + / Ctrl − / Ctrl 0. Всё запоминается в настройках вида.
(function () {
const root = document.documentElement;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const zoom = () => G.view.zoom || 1;

function drag(handle, onMove, onEnd) {
  handle.addEventListener("pointerdown", e => {
    if (e.button !== 0) return;
    e.preventDefault();
    handle.classList.add("drag");
    handle.setPointerCapture(e.pointerId);
    const move = ev => onMove(ev);
    const up = () => { handle.classList.remove("drag"); handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up); onEnd && onEnd(); G.saveView(); };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  });
}

// ширина фильтров
const gutter = $("side-grip");
if (gutter) {
  drag(gutter, e => {
    const left = $("tab-list").getBoundingClientRect().left;
    G.view.sideW = clamp(Math.round((e.clientX - left) / zoom()), 180, 520);
    root.style.setProperty("--side-w", G.view.sideW + "px");
  });
  gutter.ondblclick = () => G.toggleFilters();
}
G.toggleFilters = function () { G.view.filters = G.view.filters === false; G.applyView(); G.saveView(); };

// ширина пристёгнутой панели
const grip = document.createElement("div");
grip.className = "pgrip"; grip.title = "Потяните, чтобы изменить ширину панели";
document.body.append(grip);
function placeGrip() {
  const p = $("panel");
  grip.hidden = p.hidden || G.view.float;
  if (!grip.hidden) grip.style.left = (window.innerWidth / zoom() - (G.view.panelW || 480)) + "px";
}
drag(grip, e => {
  G.view.panelW = clamp(Math.round(window.innerWidth / zoom() - e.clientX / zoom()), 340, Math.round(window.innerWidth / zoom() * .75));
  root.style.setProperty("--panel-w", G.view.panelW + "px");
  placeGrip();
});
new MutationObserver(placeGrip).observe($("panel"), {attributes: true, attributeFilter: ["hidden"]});
window.addEventListener("resize", placeGrip);

// плавающее окно: перенос за полосу сверху, размер — за правый нижний угол
G.panelBar = function (title) {
  const h = G.h;
  const bar = h("div", {class: "pbar"},
    h("span", {class: "grip", text: "⋮⋮", title: G.view.float ? "Перетащите окно" : "Панель пристёгнута справа"}),
    h("span", {class: "ptitle", text: title || ""}),
    h("button", {class: "btn small", type: "button", text: G.view.float ? "Пристегнуть" : "Отстегнуть",
      title: G.view.float ? "Вернуть панель к правому краю" : "Сделать панель плавающим окном: двигать и растягивать", onclick: () => {
        G.view.float = !G.view.float; G.applyView(); G.saveView(); placeGrip();
        const b = bar.querySelector(".btn"); b.textContent = G.view.float ? "Пристегнуть" : "Отстегнуть";
      }}),
    h("button", {class: "btn small", type: "button", text: "✕", title: "Закрыть (Esc)", onclick: G.panelClose}));
  bar.addEventListener("pointerdown", e => {
    if (!G.view.float || e.target.closest("button")) return;
    const p = $("panel"), r = p.getBoundingClientRect(), z = zoom();
    const dx = e.clientX - r.left, dy = e.clientY - r.top;
    bar.setPointerCapture(e.pointerId);
    const move = ev => {
      G.view.fx = clamp(Math.round((ev.clientX - dx) / z), 0, Math.round(window.innerWidth / z - 120));
      G.view.fy = clamp(Math.round((ev.clientY - dy) / z), 0, Math.round(window.innerHeight / z - 60));
      root.style.setProperty("--fx", G.view.fx + "px"); root.style.setProperty("--fy", G.view.fy + "px");
    };
    const up = () => { bar.removeEventListener("pointermove", move); bar.removeEventListener("pointerup", up); G.saveView(); };
    bar.addEventListener("pointermove", move); bar.addEventListener("pointerup", up);
  });
  return bar;
};
let sizeTimer;
new ResizeObserver(() => {
  if (!G.view.float || $("panel").hidden) return;
  clearTimeout(sizeTimer);
  sizeTimer = setTimeout(() => {
    const r = $("panel").getBoundingClientRect(), z = zoom();
    G.view.fw = Math.round(r.width / z); G.view.fh = Math.round(r.height / z);
    root.style.setProperty("--fw", G.view.fw + "px"); root.style.setProperty("--fh", G.view.fh + "px");
    G.saveView();
  }, 300);
}).observe($("panel"));

// масштаб с клавиатуры
document.addEventListener("keydown", e => {
  if (!(e.ctrlKey || e.metaKey)) return;
  const steps = [0.8, 0.9, 1, 1.1, 1.2, 1.35, 1.5];
  const i = steps.findIndex(s => s >= zoom() - 0.001);
  let z = null;
  if (e.key === "=" || e.key === "+") z = steps[Math.min(steps.length - 1, i + 1)];
  else if (e.key === "-") z = steps[Math.max(0, i - 1)];
  else if (e.key === "0") z = 1;
  if (z == null) return;
  e.preventDefault();
  G.view.zoom = z; G.applyView(); G.saveView(); placeGrip();
});
placeGrip();
})();
