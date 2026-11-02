/* Главная кабинета v3 (issue #635, PR 2): вкладки «Требует внимания» и «Взять себе».
 *
 * Ванильный JS без сборки (ADR-014), отдельным файлом — CSP кабинета не пускает
 * inline-скрипты. Разметку не рисует: строки всех вкладок отдаёт сервер
 * (home.html, у строки — data-tabs), JS только прячет и показывает их.
 *
 * «Взять себе» — тот же POST …/api/requests/{id}/claim, что у очереди, с тем же
 * CSRF-контрактом (Content-Type: application/json, Origin браузер ставит сам).
 * После ответа — перезагрузка: числа карточек, вкладок и меню пересчитывает
 * сервер, а итог показывается всплывающей строкой (sessionStorage переживает
 * перезагрузку этой вкладки и только её).
 */
(function () {
  "use strict";

  var panel = document.querySelector("[data-home]");
  var toast = document.querySelector("[data-home-toast]");
  var FLASH_KEY = "staff-home-flash";
  var TAB_KEY = "staff-home-tab";

  /* Тексты по кодам каталога ошибок (R-8) — как у очереди (queue.js). */
  var MESSAGES = {
    "ERR-REQUESTS-003": "Заявку уже взяли или закрыли — список обновлён.",
    "ERR-REQUESTS-002": "Заявка не найдена — список обновлён.",
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-AUTH-003": "Нет доступа к этому действию."
  };

  var toastTimer = null;
  function showToast(text) {
    if (!toast || !text) return;
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.hidden = true; }, 5000);
  }

  function selectTab(key) {
    if (!panel || !panel.querySelector('.home-tab[data-home-tab="' + key + '"]')) return false;
    panel.querySelectorAll(".home-tab").forEach(function (tab) {
      tab.setAttribute("aria-pressed", String(tab.dataset.homeTab === key));
    });
    panel.querySelectorAll(".home-row").forEach(function (row) {
      row.hidden = row.dataset.tabs.split(" ").indexOf(key) < 0;
    });
    panel.querySelectorAll("[data-tab-more], [data-tab-empty]").forEach(function (node) {
      node.hidden = (node.dataset.tabMore || node.dataset.tabEmpty) !== key;
    });
    return true;
  }

  /* Вкладки панели и карточки «Просрочено» / «Открытые заявки» над ней. */
  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-home-tab]");
    if (!trigger || !selectTab(trigger.dataset.homeTab)) return;
    if (trigger.classList.contains("home-tab")) return;
    /* Карточка: вкладка выбрана — довести взгляд до списка. */
    event.preventDefault();
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
    panel.querySelector("h2").focus({ preventScroll: true });
  });

  /* Тот же текст, что у «Взять» в окне заявки (#636): номер заявки, не комнаты. */
  function claimed(data) {
    return data && data.daily_number != null
      ? "Заявка #" + data.daily_number + " теперь ваша" : "Заявка теперь ваша";
  }

  function reloadWith(text) {
    try {
      sessionStorage.setItem(FLASH_KEY, text);
      var pressed = panel.querySelector('.home-tab[aria-pressed="true"]');
      if (pressed) sessionStorage.setItem(TAB_KEY, pressed.dataset.homeTab);
    } catch (error) { /* приватный режим без хранилища — итог просто не покажется */ }
    window.location.reload();
  }

  async function claim(button) {
    button.disabled = true;
    try {
      var response = await fetch(panel.dataset.claimEndpoint + "/" + button.dataset.claim + "/claim", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      });
      var body = null;
      try { body = await response.json(); } catch (error) { /* не JSON */ }
      if (response.ok) {
        reloadWith(claimed(body));
        return;
      }
      var code = body && body.error ? body.error.code : "";
      reloadWith(MESSAGES[code] || "Не получилось (" + (code || response.status) + "). Попробуйте ещё раз.");
    } catch (error) {
      button.disabled = false;
      showToast("Нет связи — попробуйте ещё раз.");
    }
  }

  if (panel) {
    panel.addEventListener("click", function (event) {
      var button = event.target.closest("[data-claim]");
      if (button && !button.disabled) claim(button);
    });
  }

  try {
    var savedTab = sessionStorage.getItem(TAB_KEY);
    var flash = sessionStorage.getItem(FLASH_KEY);
    sessionStorage.removeItem(TAB_KEY);
    sessionStorage.removeItem(FLASH_KEY);
    if (savedTab) selectTab(savedTab);
    if (flash) showToast(flash);
  } catch (error) { /* хранилище недоступно — главная как есть */ }
})();
