/* Страница «Сотрудники» кабинета (spec 0033 §7, PR F серии #48; spec 0037 §4/§5).
 *
 * Ванильный JS (канон queue.js/checkin.js): JSON-действия по CSRF-контракту
 * (Content-Type: application/json + Origin от fetch), дружелюбные тексты по
 * кодам каталога ошибок. Разметку JS не сочиняет — она вся в team.html;
 * динамически появляются только тексты свежей ссылки приглашения и логина
 * (textContent, не innerHTML): сервер отдаёт ссылку ровно один раз.
 *
 * Логин подставляется подсказкой из имени, пока менеджер не тронул поле
 * (spec 0037 §4): серверу подсказка не нужна, он проверяет только присланное.
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
  var inviteLoginEl = root.querySelector("[data-invite-login]");
  var nameInput = inviteForm.querySelector("input[name=invited_name]");
  var loginInput = inviteForm.querySelector("input[name=login]");
  var loginError = inviteForm.querySelector("[data-login-error]");
  var loginTouched = false;

  /* Формат логина (spec 0037 §2), как его проверяет сервер
   * (staff_credentials.normalize_login): латиница проверяется ДО перевода в
   * заглавные — иначе `straße` стал бы `STRASSE`. Сервер остаётся последним словом. */
  var LOGIN_FORMAT = /^[A-Za-z][A-Za-z0-9]{2,11}$/;
  var LOGIN_FORMAT_TEXT = "Логин — латинские буквы и цифры, от 3 до 12 знаков, первой — буква.";

  /* Таблица ru + kk (spec 0037 §4) — полная: букв вне её в подсказке не
   * бывает. Ъ и Ь выпадают; латиница остаётся как есть. */
  var TRANSLIT = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "E", "Ж": "ZH",
    "З": "Z", "И": "I", "Й": "Y", "К": "K", "Л": "L", "М": "M", "Н": "N", "О": "O",
    "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U", "Ф": "F", "Х": "KH", "Ц": "TS",
    "Ч": "CH", "Ш": "SH", "Щ": "SHCH", "Ы": "Y", "Э": "E", "Ю": "YU", "Я": "YA",
    "Ъ": "", "Ь": "",
    "Ә": "A", "Ғ": "G", "Қ": "K", "Ң": "N", "Ө": "O", "Ұ": "U", "Ү": "U", "Һ": "H", "І": "I"
  };

  function latinWord(word) {
    var out = "";
    for (var i = 0; i < word.length; i++) {
      var letter = word[i];
      if (letter >= "A" && letter <= "Z") out += letter;
      else if (Object.prototype.hasOwnProperty.call(TRANSLIT, letter)) out += TRANSLIT[letter];
    }
    return out;
  }

  /* Подсказка из имени (spec 0037 §4): два слова и больше — три буквы
   * первого + первая второго, короткое первое берётся целиком и добирается
   * буквами второго до четырёх; одно слово — четыре буквы; меньше трёх —
   * пусто, логин вписывает менеджер. */
  function suggestLogin(name) {
    /* NFC: вставленное имя бывает в NFD, где «Й» — это «И» + бреве. */
    var words = name.normalize("NFC").toUpperCase().split(/\s+/).map(latinWord).filter(Boolean);
    var login = "";
    if (words.length >= 2) {
      login = words[0].length >= 3 ? words[0].slice(0, 3) + words[1][0] : (words[0] + words[1]).slice(0, 4);
    } else if (words.length === 1) {
      login = words[0].slice(0, 4);
    }
    return login.length >= 3 ? login : "";
  }

  function showLoginError(text) {
    loginError.textContent = text;
    loginError.hidden = !text;
  }

  /* Дружелюбные тексты по кодам каталога ошибок (R-8). */
  var MESSAGES = {
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-AUTH-003": "Нет доступа к этому действию.",
    "ERR-AUTH-004": "Приглашение уже недействительно — обновите страницу.",
    "ERR-AUTH-008": "Сотрудник не найден — обновите страницу.",
    "ERR-AUTH-011": "Свою роль и свой доступ менять нельзя — попросите другого менеджера.",
    "ERR-PLATFORM-002": "Проверьте имя и роль."
  };

  function requestError(message, code, httpStatus) {
    var error = new Error(message);
    error.code = code;
    error.httpStatus = httpStatus;
    return error;
  }

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
      throw requestError(
        MESSAGES[code] || "Не получилось (" + (code || response.status) + "). Попробуйте ещё раз.",
        code, response.status);
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

  nameInput.addEventListener("input", function () {
    if (loginTouched) return;
    loginInput.value = suggestLogin(nameInput.value);
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
    if (!LOGIN_FORMAT.test(rawLogin)) {
      showLoginError(LOGIN_FORMAT_TEXT);
      return;
    }
    setBusy(true);
    try {
      var invite = await post("/invites", { invited_name: name, login: login, role_key: role.value });
      inviteLoginEl.textContent = invite.login;
      inviteUrlEl.textContent = invite.invite_url;
      inviteResult.hidden = false;
      inviteForm.reset();
      loginTouched = false;
    } catch (error) {
      /* ERR-AUTH-012: 409 — логин занят в отеле, 422 — не тот формат. */
      if (error.code === "ERR-AUTH-012") {
        /* Пример не должен совпасть с занятым: 12-значный логин на «2» → «3». */
        var suggestion = login.length < 12
          ? login + "2"
          : login.slice(0, 11) + (login.slice(-1) === "2" ? "3" : "2");
        showLoginError(error.httpStatus === 409
          ? "Логин " + login + " уже занят в отеле — выберите другой, например " + suggestion + "."
          : LOGIN_FORMAT_TEXT);
      } else {
        showStatus(error.message);
      }
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
