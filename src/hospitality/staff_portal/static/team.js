/* Страница «Сотрудники» кабинета (spec 0033 §7, PR F серии #48; spec 0037 §4/§5).
 *
 * Ванильный JS (канон queue.js/checkin.js): JSON-действия по CSRF-контракту
 * (Content-Type: application/json + Origin от fetch), дружелюбные тексты по
 * кодам каталога ошибок. Правило логина, запрос и уровень со службами — общие
 * с «Пригласить списком» (team_batch.js): они в team_common.js (window.Team). Разметку JS не сочиняет — она вся в team.html;
 * динамически появляются только тексты свежей ссылки приглашения и логина
 * (textContent, не innerHTML): сервер отдаёт ссылку ровно один раз.
 *
 * Логин подставляется подсказкой из имени, пока менеджер не тронул поле
 * (spec 0037 §4): серверу подсказка не нужна, он проверяет только присланное.
 *
 * Уровень и службы (spec 0045 §6): у менеджера блока служб нет — он видит всё;
 * сотруднику и старшему нужна хотя бы одна служба — это проверяется здесь до
 * запроса, а сервер остаётся последним словом (ERR-AUTH-015).
 *
 * Состав и роли меняются редко — поллинга здесь нет: после успешного действия
 * страница перезагружается, свежий список приходит с сервера. Исключение —
 * свежие ссылки: «Новая ссылка» у приглашения (spec 0037 §4) и ссылка нового
 * пароля (spec 0044 §4, #944) — их, как ссылку приглашения, страница
 * показывает без перезагрузки.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-endpoint]");
  if (!root) return;
  var endpoint = root.dataset.endpoint;
  var managerRole = root.dataset.managerRole;
  var status = root.querySelector("[data-team-status]");
  var inviteForm = root.querySelector("[data-invite-form]");
  var inviteResult = root.querySelector("[data-invite-result]");
  var inviteUrlEl = root.querySelector("[data-invite-url]");
  var inviteLoginEl = root.querySelector("[data-invite-login]");
  var nameInput = inviteForm.querySelector("input[name=invited_name]");
  var loginInput = inviteForm.querySelector("input[name=login]");
  var loginError = inviteForm.querySelector("[data-login-error]");
  var loginTouched = false;
  var Team = window.Team;

  function showLoginError(text) {
    loginError.textContent = text;
    loginError.hidden = !text;
  }

  function showStatus(text) {
    status.textContent = text;
    status.hidden = !text;
  }

  function post(path, payload) {
    return Team.post(endpoint + path, payload);
  }

  var setBusy = Team.setBusy;

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

  /* ---------- Уровень и службы (spec 0045 §6) ---------- */

  var accessForms = Array.prototype.slice.call(document.querySelectorAll("[data-access-form]"));
  [inviteForm].concat(accessForms).forEach(function (form) {
    Team.bindRoleForm(form, managerRole);
  });

  /* Форма приглашения — по кнопке «Пригласить» в шапке (#758): список
   * сотрудников открывают чаще, и он стоит первым. */
  var inviteOpen = root.querySelector("[data-invite-open]");
  var invitePanel = root.querySelector("[data-invite-panel]");
  inviteOpen.addEventListener("click", function () {
    var open = invitePanel.hidden;
    invitePanel.hidden = !open;
    inviteOpen.setAttribute("aria-expanded", String(open));
    if (open) nameInput.focus();
  });

  nameInput.addEventListener("input", function () {
    if (loginTouched) return;
    loginInput.value = Team.suggestLogin(nameInput.value);
    /* Подстановка не шлёт событие input полю логина — ошибку гасим сами. */
    showLoginError("");
  });
  loginInput.addEventListener("input", function () {
    loginTouched = true;
    showLoginError("");
  });

  inviteForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    var name = nameInput.value.trim();
    var rawLogin = loginInput.value.trim();
    var login = rawLogin.toUpperCase();
    var role = inviteForm.querySelector("input[name=role_key]:checked");
    showStatus("");
    showLoginError("");
    if (!name) {
      showStatus("Укажите имя сотрудника.");
      return;
    }
    if (!Team.LOGIN_FORMAT.test(rawLogin)) {
      showLoginError(Team.LOGIN_FORMAT_TEXT);
      return;
    }
    var serviceKeys = Team.chosenServices(inviteForm, managerRole);
    if (serviceKeys === null) return;
    setBusy(true);
    try {
      var invite = await post("/invites", {
        invited_name: name, login: login, role_key: role.value, service_keys: serviceKeys
      });
      inviteLoginEl.textContent = invite.login;
      inviteUrlEl.textContent = invite.invite_url;
      inviteResult.hidden = false;
      inviteForm.reset();
      Team.syncServices(inviteForm, managerRole);
      loginTouched = false;
    } catch (error) {
      /* ERR-AUTH-012: 409 — логин занят в отеле, 422 — не тот формат. */
      if (error.code === "ERR-AUTH-012") {
        showLoginError(error.httpStatus === 409
          ? Team.loginTakenText(login)
          : Team.LOGIN_FORMAT_TEXT);
      } else {
        showStatus(error.message);
      }
    } finally {
      setBusy(false);
    }
  });

  /* «Поделиться» — системный лист телефона (WhatsApp/Telegram одним нажатием);
   * нет Web Share API (десктоп) — фолбэк на копирование. Один обработчик на
   * ссылку приглашения и ссылку нового пароля (spec 0044 §4). */
  async function shareOrCopy(url, title, share) {
    if (share && navigator.share) {
      try {
        await navigator.share({ title: title, text: url });
      } catch (error) {
        /* Пользователь закрыл системный лист — это не ошибка. */
      }
      return;
    }
    try {
      await navigator.clipboard.writeText(url);
      showStatus("Ссылка скопирована.");
    } catch (error) {
      showStatus("Скопируйте ссылку вручную — доступ к буферу обмена закрыт.");
    }
  }

  inviteResult.addEventListener("click", function (event) {
    var url = inviteUrlEl.textContent;
    var share = event.target.closest("[data-invite-share]");
    if (!url || !(share || event.target.closest("[data-invite-copy]"))) return;
    shareOrCopy(url, "Приглашение в кабинет", Boolean(share));
  });

  /* «Новая ссылка» у приглашения (spec 0037 §4): прежняя гаснет, новая
   * показывается один раз, без перезагрузки — иначе пропала бы. */
  var reissueResult = document.querySelector("[data-reissue-result]");
  var reissueUrlEl = reissueResult && reissueResult.querySelector("[data-reissue-url]");
  var reissueLoginEl = reissueResult && reissueResult.querySelector("[data-reissue-login]");
  if (reissueResult) {
    reissueResult.addEventListener("click", function (event) {
      var url = reissueUrlEl.textContent;
      var share = event.target.closest("[data-reissue-share]");
      if (!url || !(share || event.target.closest("[data-reissue-copy]"))) return;
      shareOrCopy(url, "Приглашение в кабинет", Boolean(share));
    });
  }

  async function reissueInvite(button) {
    var login = button.dataset.login;
    var expired = button.dataset.expired === "1";
    /* Живую ссылку новая гасит — спросить; истёкшей терять нечего. */
    if (!expired && !window.confirm("Выдать новую ссылку для " + login +
        "? Прежняя ссылка перестанет работать.")) return;
    setBusy(true);
    showStatus("");
    try {
      var invite = await post("/invites/" + button.dataset.inviteId + "/reissue");
      reissueLoginEl.textContent = invite.login;
      reissueUrlEl.textContent = invite.invite_url;
      reissueResult.hidden = false;
      /* Строка приглашения теперь живая: пометка и «Отозвать» вместо «Убрать». */
      var row = button.closest(".team-row");
      var state = row.querySelector("[data-invite-state]");
      state.textContent = reissueResult.closest("[data-reissued-text]").dataset.reissuedText;
      state.classList.remove("hint-alert");
      row.querySelectorAll("[data-expired]").forEach(function (item) { item.dataset.expired = "0"; });
      row.querySelector("[data-action=revoke-invite]").textContent = "Отозвать";
      reissueResult.scrollIntoView({ block: "center" });
    } catch (error) {
      /* ERR-AUTH-012 (409): логин истёкшего приглашения успели занять. */
      showStatus(error.code === "ERR-AUTH-012"
        ? "Логин " + login + " уже занят в отеле — уберите приглашение и пригласите заново " +
          "с другим логином."
        : error.message);
    } finally {
      setBusy(false);
    }
  }

  /* Ссылка нового пароля (spec 0044 §4; менеджеру — у сотрудника и старшего,
   * #944): показывается один раз, без перезагрузки — иначе пропала бы. */
  var resetResult = document.querySelector("[data-reset-result]");
  var resetUrlEl = resetResult.querySelector("[data-reset-url]");
  var resetLoginEl = resetResult.querySelector("[data-reset-login]");
  resetResult.addEventListener("click", function (event) {
    var url = resetUrlEl.textContent;
    var share = event.target.closest("[data-reset-share]");
    if (!url || !(share || event.target.closest("[data-reset-copy]"))) return;
    shareOrCopy(url, "Новый пароль для кабинета", Boolean(share));
  });

  async function resetPassword(button) {
    var login = button.dataset.login;
    /* Менеджеру — ещё строка об уведомлении суперадмину (её даёт шаблон). */
    var notice = root.dataset.resetNotice;
    if (!window.confirm("Сбросить пароль " + login + "? Появится одноразовая ссылка — по ней " +
        "сотрудник задаст новый пароль. Логин и история останутся прежними, а старый пароль " +
        "работает, пока ссылкой не воспользовались." + (notice ? " " + notice : ""))) return;
    setBusy(true);
    showStatus("");
    try {
      var reset = await post("/members/" + button.dataset.userId + "/password-reset");
      resetLoginEl.textContent = reset.login;
      resetUrlEl.textContent = reset.reset_url;
      resetResult.hidden = false;
      resetResult.scrollIntoView({ block: "center" });
    } catch (error) {
      showStatus(error.message);
    } finally {
      setBusy(false);
    }
  }

  function eraseConfirmText(button) {
    var login = button.dataset.login;
    var who = login || "«" + button.dataset.name + "»";
    return "Стереть учётку " + who + "? Сотрудник пропадёт из списка и больше не сможет войти. " +
      "Его действия останутся в журнале" + (login ? ", логин " + login + " не освободится" : "") +
      ". Отменить нельзя.";
  }

  /* «Как войти» (spec 0037 §5): прямая ссылка на кабинет отеля — без
   * сессии она ведёт на вход отеля, код вводить не нужно. */
  var hotelLink = document.querySelector("[data-hotel-link]");
  var hotelLinkStatus = document.querySelector("[data-hotel-link-status]");
  hotelLink.addEventListener("click", async function () {
    var text;
    try {
      await navigator.clipboard.writeText(hotelLink.dataset.hotelLink);
      text = "Ссылка скопирована.";
    } catch (error) {
      text = "Скопируйте ссылку вручную: " + hotelLink.dataset.hotelLink;
    }
    hotelLinkStatus.textContent = text;
    hotelLinkStatus.hidden = false;
  });

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-action]");
    if (!button) return;
    if (button.dataset.action === "revoke-invite") {
      /* «Убрать» истёкшее — ссылка уже мёртвая, спрашивать не о чем. */
      act("/invites/" + button.dataset.inviteId + "/revoke", null,
        button.dataset.expired === "1" ? null : "Отозвать приглашение? Ссылка перестанет работать.");
    } else if (button.dataset.action === "reissue-invite") {
      reissueInvite(button);
    } else if (button.dataset.action === "deactivate") {
      act("/members/" + button.dataset.userId + "/deactivate", null,
        "Отключить сотрудника? Он выйдет из кабинета сразу, вернуть можно только новым приглашением.");
    } else if (button.dataset.action === "reset-password") {
      resetPassword(button);
    } else if (button.dataset.action === "erase") {
      act("/members/" + button.dataset.userId + "/erase", null, eraseConfirmText(button));
    } else if (button.dataset.action === "reset-signature") {
      /* «Сбросить подпись» (spec 0077 §4): телефон потерян — ключи и образец. */
      act("/members/" + button.dataset.userId + "/signature-reset", null,
        "Сбросить подпись " + button.dataset.name + "? Привязанные устройства и образец " +
        "подписи удалятся — сотрудник настроит подпись заново.");
    }
  });

  accessForms.forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var serviceKeys = Team.chosenServices(form, managerRole);
      if (serviceKeys === null) return;
      act("/members/" + form.dataset.userId + "/access",
        { role_key: Team.selectedRole(form), service_keys: serviceKeys });
    });
  });
})();
