/* Каркас кабинета v3 (issue #635): тема, меню профиля, «Ещё» на телефоне.
 *
 * Подключён в <head> БЕЗ defer (layout.html): тему надо поставить до первой
 * отрисовки, иначе страница мигнёт светлой и перекрасится в тёмную. Остальное
 * ждёт DOMContentLoaded.
 *
 * Тема — выбор этого браузера: localStorage, по умолчанию «Системная» (атрибута
 * нет, палитру выбирает prefers-color-scheme в styles.css). «Тёмная» и
 * «Светлая» ставят data-theme на <html> — токены styles.css знают оба случая.
 *
 * Разметки JS не рисует: меню и лист «Ещё» — в layout.html, здесь только
 * `hidden` и aria-атрибуты (канон signal.js). Выход — обычная POST-форма
 * в разметке; её перехватывает push.js, чтобы сначала отписать телефон.
 */
(function () {
  "use strict";

  var THEME_KEY = "staff-theme";
  var root = document.documentElement;

  function storedTheme() {
    try {
      return window.localStorage.getItem(THEME_KEY);
    } catch (error) {
      return null; /* Хранилище закрыто — тема как в системе. */
    }
  }

  function applyTheme(choice) {
    if (choice === "dark" || choice === "light") root.setAttribute("data-theme", choice);
    else root.removeAttribute("data-theme");
  }

  applyTheme(storedTheme());

  function currentChoice() {
    return root.getAttribute("data-theme") || "system";
  }

  function markTheme() {
    var choice = currentChoice();
    document.querySelectorAll("[data-theme-choice]").forEach(function (button) {
      button.setAttribute("aria-pressed", String(button.dataset.themeChoice === choice));
    });
  }

  function chooseTheme(choice) {
    applyTheme(choice);
    try {
      if (choice === "system") window.localStorage.removeItem(THEME_KEY);
      else window.localStorage.setItem(THEME_KEY, choice);
    } catch (error) {
      /* Не запомнили — тема продержится до следующей страницы. */
    }
    markTheme();
  }

  function init() {
    markTheme();

    var profileButton = document.querySelector("[data-profile-toggle]");
    var profileMenu = document.getElementById("app-profile-menu");
    var sheet = document.getElementById("app-more");
    var sheetOpener = null;

    function closeProfile(refocus) {
      if (!profileMenu || profileMenu.hidden) return;
      profileMenu.hidden = true;
      profileButton.setAttribute("aria-expanded", "false");
      if (refocus) profileButton.focus();
    }

    function openSheet(opener) {
      sheetOpener = opener;
      sheet.hidden = false;
      opener.setAttribute("aria-expanded", "true");
      root.classList.add("is-locked");
      sheet.querySelector("button[data-more-close]").focus();
    }

    function closeSheet() {
      if (!sheet || sheet.hidden) return;
      sheet.hidden = true;
      root.classList.remove("is-locked");
      if (sheetOpener) {
        sheetOpener.setAttribute("aria-expanded", "false");
        sheetOpener.focus();
      }
    }

    document.addEventListener("click", function (event) {
      var target = event.target;
      var theme = target.closest("[data-theme-choice]");
      if (theme) {
        chooseTheme(theme.dataset.themeChoice);
        return;
      }
      if (profileButton && target.closest("[data-profile-toggle]")) {
        var open = profileMenu.hidden;
        profileMenu.hidden = !open;
        profileButton.setAttribute("aria-expanded", String(open));
        return;
      }
      if (profileMenu && !profileMenu.hidden && !target.closest("#app-profile-menu")) {
        closeProfile(false);
      }
      var opener = target.closest("[data-more-open]");
      if (sheet && opener) {
        openSheet(opener);
        return;
      }
      if (target.closest("[data-more-close]")) closeSheet();
    });

    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      if (sheet && !sheet.hidden) closeSheet();
      else closeProfile(true);
    });

    /* Окно растянули шире телефона при открытом «Ещё» — листа не видно, а
     * прокрутка заперта: закрываем. */
    var phone = window.matchMedia("(max-width: 960px)");
    var onChange = function () { if (!phone.matches) closeSheet(); };
    if (phone.addEventListener) phone.addEventListener("change", onChange);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
