/* Страница «Сотрудники» кабинета (spec 0033 §7, PR F серии #48).
 *
 * Ванильный JS (канон queue.js/checkin.js): JSON-действия по CSRF-контракту
 * (Content-Type: application/json + Origin от fetch), дружелюбные тексты по
 * кодам каталога ошибок. Разметку JS не сочиняет — она вся в team.html;
 * единственное, что появляется динамически, это текст свежей ссылки
 * приглашения (textContent, не innerHTML): сервер отдаёт её ровно один раз.
 *
 * Состав и роли меняются редко — поллинга здесь нет: после успешного действия
 * страница перезагружается, свежий список приходит с сервера.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-endpoint]");
  if (!root) return;
  var endpoint = root.dataset.endpoint;
  var status = root.querySelector("[data-team-status]");
  var inviteForm = root.querySelector("[data-invite-form]");
  var inviteResult = root.querySelector("[data-invite-result]");
  var inviteUrlEl = root.querySelector("[data-invite-url]");

  /* Дружелюбные тексты по кодам каталога ошибок (R-8). */
  var MESSAGES = {
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-AUTH-003": "Нет доступа к этому действию.",
    "ERR-AUTH-004": "Приглашение уже недействительно — обновите страницу.",
    "ERR-AUTH-008": "Сотрудник не найден — обновите страницу.",
    "ERR-AUTH-011": "Свою роль и свой доступ менять нельзя — попросите другого менеджера.",
    "ERR-PLATFORM-002": "Проверьте имя и роль."
  };

  function showStatus(text) {
    status.textContent = text;
    status.hidden = !text;
  }

  async function post(path, payload) {
    var response = await fetch(endpoint + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    });
    var data = null;
    try { data = await response.json(); } catch (error) { /* не конверт */ }
    if (!response.ok) {
      var code = data && data.error && data.error.code;
      throw new Error(MESSAGES[code] || "Не получилось (" + (code || response.status) + "). Попробуйте ещё раз.");
    }
    return data;
  }

  function setBusy(busy) {
    document.querySelectorAll("button").forEach(function (button) { button.disabled = busy; });
  }

  async function act(path, payload, confirmText) {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true);
    showStatus("");
    try {
      await post(path, payload);
      window.location.reload();
    } catch (error) {
      showStatus(error.message);
      setBusy(false);
    }
  }

  inviteForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    var name = inviteForm.querySelector("input[name=invited_name]").value.trim();
    var role = inviteForm.querySelector("input[name=role_key]:checked");
    if (!name) {
      showStatus("Укажите имя сотрудника.");
      return;
    }
    setBusy(true);
    showStatus("");
    try {
      var invite = await post("/invites", { invited_name: name, role_key: role.value });
      inviteUrlEl.textContent = invite.invite_url;
      inviteResult.hidden = false;
      inviteForm.reset();
    } catch (error) {
      showStatus(error.message);
    } finally {
      setBusy(false);
    }
  });

  /* «Поделиться» — системный лист телефона (WhatsApp/Telegram одним нажатием);
   * нет Web Share API (десктоп) — фолбэк на копирование. */
  inviteResult.addEventListener("click", async function (event) {
    var url = inviteUrlEl.textContent;
    if (!url) return;
    if (event.target.closest("[data-invite-share]") && navigator.share) {
      try {
        await navigator.share({ title: "Приглашение в кабинет", text: url });
      } catch (error) {
        /* Пользователь закрыл системный лист — это не ошибка. */
      }
      return;
    }
    if (event.target.closest("[data-invite-share]") || event.target.closest("[data-invite-copy]")) {
      try {
        await navigator.clipboard.writeText(url);
        showStatus("Ссылка скопирована.");
      } catch (error) {
        showStatus("Скопируйте ссылку вручную — доступ к буферу обмена закрыт.");
      }
    }
  });

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-action]");
    if (!button) return;
    if (button.dataset.action === "revoke-invite") {
      act("/invites/" + button.dataset.inviteId + "/revoke", null,
        "Отозвать приглашение? Ссылка перестанет работать.");
    } else if (button.dataset.action === "deactivate") {
      act("/members/" + button.dataset.userId + "/deactivate", null,
        "Отключить сотрудника? Он выйдет из кабинета сразу, вернуть можно только новым приглашением.");
    }
  });

  document.querySelectorAll("[data-role-form]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      act("/members/" + form.dataset.userId + "/role",
        { role_key: form.querySelector("select[name=role_key]").value });
    });
  });
})();
