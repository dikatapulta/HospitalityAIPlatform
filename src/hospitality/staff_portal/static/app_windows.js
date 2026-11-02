/* Окна каркаса кабинета v3 (#636): окно поверх страницы, всплывашка, клавиши.
 *
 * Окно — элемент с data-window в разметке (_app_windows.html, queue.html):
 * «Новая заявка» по центру, карточка заявки панелью справа, на телефоне оба —
 * листом снизу (shell.css). Здесь только поведение: открыть и закрыть, фокус
 * внутри окна (Tab ходит по кругу), Esc, клавиша N — новая заявка, Ctrl+K / ⌘K —
 * поле поиска страницы (data-search-input), всплывашка внизу экрана с кнопкой
 * «Отменить». Разметки JS не рисует — канон signal.js.
 *
 * Подключён в <head> с defer (layout.html): выполняется раньше скриптов страниц,
 * и они застают window.StaffWindows — open(id, opener), close(), toast(text, undo).
 * open шлёт окну событие staff-window-open, close — staff-window-close
 * (detail.silent — фокус возвращать не надо): форма загружается заново, панель
 * заявки забывает, какую заявку показывала, и возвращает фокус на её номер.
 */
(function () {
  "use strict";

  var TOAST_MS = 4000;
  /* С «Отменить» дольше: человек успевает прочесть и передумать. */
  var TOAST_UNDO_MS = 7000;
  var NEW_REQUEST_ID = "new-request-dialog";

  var root = document.documentElement;
  var current = null;
  var opener = null;
  var toastTimer = null;

  function visible(element) {
    return !element.disabled && !element.closest("[hidden]") && element.getClientRects().length > 0;
  }

  function focusables(container) {
    var all = container.querySelectorAll(
      "a[href], button, input, select, textarea, [tabindex]:not([tabindex='-1'])"
    );
    return Array.prototype.filter.call(all, visible);
  }

  function open(id, from) {
    var win = document.getElementById(id);
    if (!win) return false;
    if (current && current !== win) close(true);
    if (current !== win) opener = from || document.activeElement;
    current = win;
    win.hidden = false;
    root.classList.add("is-locked");
    win.dispatchEvent(new CustomEvent("staff-window-open"));
    var target = win.querySelector("[data-window-autofocus]") || win.querySelector("[role=dialog]");
    if (target) target.focus();
    return true;
  }

  /* `silent` — фокус не возвращать: окно сменяется другим. */
  function close(silent) {
    if (!current) return;
    var win = current;
    current = null;
    win.hidden = true;
    root.classList.remove("is-locked");
    win.dispatchEvent(new CustomEvent("staff-window-close", { detail: { silent: Boolean(silent) } }));
    if (!silent && opener && document.body.contains(opener) && visible(opener)) opener.focus();
    opener = null;
  }

  function hideToast() {
    var box = document.querySelector("[data-toast]");
    if (box) box.hidden = true;
  }

  /* `undo` — функция обратного действия или null: кнопка «Отменить» только с ней. */
  function toast(text, undo) {
    var box = document.querySelector("[data-toast]");
    if (!box) return;
    box.querySelector("[data-toast-text]").textContent = text;
    var button = box.querySelector("[data-toast-undo]");
    button.hidden = !undo;
    button.onclick = undo ? function () { hideToast(); undo(); } : null;
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, undo ? TOAST_UNDO_MS : TOAST_MS);
  }

  function trapTab(event) {
    var dialog = current.querySelector("[role=dialog]");
    var items = focusables(dialog);
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    var active = document.activeElement;
    var outside = !dialog.contains(active) || active === dialog;
    if (event.shiftKey && (active === first || outside)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || outside)) {
      event.preventDefault();
      first.focus();
    }
  }

  function typing(target) {
    return Boolean(target.closest && target.closest("input, textarea, select, [contenteditable]"));
  }

  /* Меню профиля или «Ещё» открыты (shell.js) — клавиша N их не перекрывает. */
  function shellMenuOpen() {
    return document.querySelector(".app-menu:not([hidden]), .app-sheet:not([hidden])") !== null;
  }

  document.addEventListener("keydown", function (event) {
    if (current) {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      } else if (event.key === "Tab") {
        trapTab(event);
      }
      return;
    }
    if ((event.metaKey || event.ctrlKey) && !event.altKey && event.code === "KeyK") {
      var search = document.querySelector("[data-search-input]");
      if (search) {
        event.preventDefault();
        search.focus();
        search.select();
      }
      return;
    }
    /* event.code, а не key: N — та же клавиша и в русской раскладке («Т»). */
    if (event.code !== "KeyN" || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    if (event.repeat || typing(event.target) || shellMenuOpen()) return;
    if (open(NEW_REQUEST_ID)) event.preventDefault();
  });

  document.addEventListener("click", function (event) {
    var closer = event.target.closest("[data-window-close]");
    if (closer && current && current.contains(closer)) {
      close();
      return;
    }
    var link = event.target.closest("[data-new-request-open]");
    /* Ctrl/⌘-клик и средняя кнопка — новая вкладка со страницей формы, как раньше. */
    if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return;
    if (open(NEW_REQUEST_ID, link)) event.preventDefault();
  });

  window.StaffWindows = {
    open: open,
    close: close,
    toast: toast,
    isOpen: function (id) { return current !== null && current.id === id; }
  };
})();
