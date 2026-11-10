// Экраны страницы меню и история браузера (#755): меню → корзина → оформление → заказ —
// записи истории, поэтому системная «Назад» телефона ведёт на прошлый экран, а не в чат.
// Вклеивается на место своей метки в menu.js (menu/page.py) и пользуется его `state`,
// `render`, `loadData`, `refreshData`, `refreshOrder`, `closeSheet` и `api`.
  // mode: "push" — новая запись, "replace" — подмена текущей, без него история не меняется.
  // depth — шагов от записи меню: корзина 1, оформление 2, заказ 1 (из меню) или 2 (вместо оформления).
  const depthNow = () => (history.state && history.state.depth) || 0;
  function go(screen, mode) {
    if (mode) {
      const depth = mode === "push" ? depthNow() + 1 : screen === "menu" ? 0 : depthNow();
      const id = screen === "order" ? state.order.id : null;
      history[mode + "State"]({screen, depth, order: id}, "", location.pathname + (id ? "#order=" + id : screen === "menu" ? "" : "#" + screen));
    }
    state.screen = screen; closeSheet(); render(); window.scrollTo(0, 0);
  }
  // Назад к меню или корзине: своя запись есть — шаги истории (экран нарисует popstate), нет — подмена.
  function leave(screen) {
    const steps = depthNow() - (screen === "cart" ? 1 : 0);
    if (steps <= 0) go(screen, "replace");
    else if (!state.leaving) { state.leaving = true; history.go(-steps); }
  }
  function showOrder(order, mode) {
    state.order = order;
    go("order", mode);
    clearTimeout(state.poll);
    if (ACTIVE.includes(order.stage)) state.poll = setTimeout(refreshOrder, 8000);
  }
  function toMenu() { clearTimeout(state.poll); leave("menu"); if (state.screen === "menu") refreshData(); }

  window.addEventListener("popstate", (e) => {
    const s = e.state || {};
    state.leaving = false;
    if (!state.data) return;
    if (s.screen === "order" && state.order && state.order.id === s.order) { showOrder(state.order); return; }
    clearTimeout(state.poll);
    go(s.screen === "cart" || s.screen === "checkout" ? s.screen : "menu");
    if (state.screen === "menu") refreshData();
  });

  // Первая загрузка; не удалась (нет связи, сбой) — экран с «Попробовать снова» и один автоповтор через 3 с (#755).
  async function start(again) {
    state.screen = "loading"; render();
    if (!(await loadData())) {
      state.screen = "failed"; render();
      if (again) setTimeout(() => { if (state.screen === "failed") start(false); }, 3000);
      return;
    }
    const match = location.hash.match(/^#order=([0-9a-f-]{36})$/);
    if (match) {
      const r = await api("/orders/" + match[1], {method: "GET"});
      if (r.ok) { showOrder(r.data, "replace"); return; }
    }
    go("menu", "replace");
  }
