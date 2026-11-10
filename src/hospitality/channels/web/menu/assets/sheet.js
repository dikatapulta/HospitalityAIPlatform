// Листы снизу страницы меню: карточка товара и «Отменить заказ?» (#755: «×» и свайп вниз).
// Вклеивается на место своей метки в menu.js (menu/page.py) и пользуется его
// `state`, `sheet`, `scrim`, `t`, `esc`, `I` и данными каталога.
  function openSheet(html) {
    sheet.innerHTML = '<div class="grabber" aria-hidden="true"></div><button class="icon-btn sheet-x" data-act="close-sheet" aria-label="' +
      esc(t("close")) + '">' + I.x + "</button>" + html;
    sheet.hidden = false; scrim.hidden = false;
    requestAnimationFrame(() => { sheet.classList.add("open"); scrim.classList.add("open"); });
  }
  function closeSheet() {
    if (sheet.hidden) return;
    sheet.classList.remove("open"); scrim.classList.remove("open"); state.sheetItem = null;
    setTimeout(() => { if (!state.sheetItem) { sheet.hidden = true; scrim.hidden = true; } }, 260);
  }
  function openItem(id) {
    const item = itemsOf(cat())[id];
    if (!item) return;
    state.sheetItem = id; state.sheetQty = Math.max(1, cart()[id] || 1);
    const can = cat().accepting_orders && item.is_available;
    openSheet(plate(item) + '<h3 id="sheet-title">' + esc(L(item.names)) + "</h3>" + (L(item.descriptions) ? '<p class="desc">' + esc(L(item.descriptions)) + "</p>" : "") +
      '<p class="dish-price num" style="margin-top:10px;font-size:19px">' + money(item.price_minor) + "</p>" + (can
        ? '<div class="sheet-row"><div class="mini-stepper"><button data-sq="-1" aria-label="' + esc(t("less")) + '">' + I.minus + '</button><b class="num" id="sq"></b><button data-sq="1" aria-label="' +
          esc(t("more")) + '">' + I.plus + '</button></div><button class="cta" data-act="sheet-add" id="sheet-add"></button></div>'
        : '<div class="notice" style="margin-top:16px">' + I.info + "<b>" + esc(item.is_available ? tc("closed") : t("sold_out")) + "</b><span>" +
          esc(item.is_available ? t("closed_note") : tc("sold_out_note")) + "</span></div>"));
    updateSheet();
  }
  function updateSheet() {
    const button = $("#sheet-add");
    if (!button) return;
    const item = itemsOf(cat())[state.sheetItem];
    $("#sq").textContent = state.sheetQty;
    button.innerHTML = "<span>" + esc(cart()[item.id] ? t("update") : t("add")) + '</span><span class="num">' + money(item.price_minor * state.sheetQty) + "</span>";
  }

  // Лист закрывается свайпом вниз, когда он прокручен к началу (#755); мышью — «×» и фон.
  let drag = null;
  sheet.addEventListener("touchstart", (e) => { drag = sheet.scrollTop <= 0 ? {y: e.touches[0].clientY, dy: 0, at: Date.now()} : null; }, {passive: true});
  sheet.addEventListener("touchmove", (e) => {
    if (!drag) return;
    const dy = e.touches[0].clientY - drag.y;
    if (!drag.dy && dy < 8) { if (dy < 0) drag = null; return; } // вверх — прокрутка листа, дрожь пальца — ещё не жест
    e.preventDefault(); drag.dy = Math.max(1, dy);
    sheet.style.transition = "none"; sheet.style.transform = "translateY(" + drag.dy + "px)";
  }, {passive: false});
  function endDrag(e) {
    if (!drag) return;
    const close = e.type === "touchend" && (drag.dy > 100 || (drag.dy > 40 && Date.now() - drag.at < 250));
    drag = null; sheet.style.transition = ""; sheet.style.transform = "";
    if (close) closeSheet();
  }
  sheet.addEventListener("touchend", endDrag);
  sheet.addEventListener("touchcancel", endDrag);
