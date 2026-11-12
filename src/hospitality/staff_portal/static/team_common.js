/* Общее страницы «Сотрудники»: правило логина, уровень и службы формы, JSON-запрос.
 *
 * Им пользуются приглашение по одному (team.js) и списком (team_batch.js,
 * spec 0072 §4): одно правило на два места (P-12). Подключается перед ними;
 * отдельный файл — CSP default-src 'self'. Сервер остаётся последним словом.
 */
(function () {
  "use strict";

  /* Формат логина (spec 0037 §2), как его проверяет сервер
   * (staff_credentials.normalize_login): латиница проверяется ДО перевода в
   * заглавные — иначе `straße` стал бы `STRASSE`. */
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

  /* «Уже занят» с примером; пример не должен совпасть с занятым: 12-значный
   * логин на «2» → «3». */
  function loginTakenText(login) {
    var example = login.length < 12
      ? login + "2"
      : login.slice(0, 11) + (login.slice(-1) === "2" ? "3" : "2");
    return "Логин " + login + " уже занят в отеле — выберите другой, например " + example + ".";
  }

  /* Дружелюбные тексты по кодам каталога ошибок (R-8). */
  var MESSAGES = {
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-AUTH-003": "Нет доступа к этому действию.",
    "ERR-AUTH-004": "Приглашение уже недействительно — обновите страницу.",
    "ERR-AUTH-008": "Сотрудник не найден — обновите страницу.",
    "ERR-AUTH-011": "Свой уровень и свой доступ менять нельзя — попросите другого менеджера.",
    "ERR-AUTH-013": "Учётку суперадмина меняют только командой на сервере.",
    "ERR-AUTH-015": "Такой службы в отеле нет — обновите страницу.",
    "ERR-AUTH-019": "Слишком много приглашений за сутки — продолжите завтра или попросите другого менеджера.",
    "ERR-AUTH-020": "Слишком много неотвеченных приглашений менеджеров — уберите лишние в «Ожидают принятия» или дождитесь, пока их примут.",
    "ERR-PLATFORM-002": "Проверьте имя и роль."
  };

  /* JSON-действие по CSRF-контракту (Content-Type: application/json, Origin
   * ставит fetch). Отказ — Error с текстом, кодом, статусом и `details`
   * конверта (строки списка, spec 0072 §4). */
  async function post(url, payload) {
    var response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    });
    var data = null;
    try { data = await response.json(); } catch (error) { /* не конверт */ }
    if (!response.ok) {
      var envelope = (data && data.error) || {};
      var error = new Error(MESSAGES[envelope.code] ||
        "Не получилось (" + (envelope.code || response.status) + "). Попробуйте ещё раз.");
      error.code = envelope.code;
      error.httpStatus = response.status;
      error.details = envelope.details || [];
      throw error;
    }
    return data;
  }

  function setBusy(busy) {
    document.querySelectorAll("button").forEach(function (button) { button.disabled = busy; });
  }

  /* ---------- Уровень и службы формы (spec 0045 §6) ---------- */

  function selectedRole(form) {
    var radio = form.querySelector("input[name=role_key]:checked");
    if (radio) return radio.value;
    return form.querySelector("select[name=role_key]").value;
  }

  /* У менеджера блока служб нет — он видит всё. */
  function syncServices(form, managerRole) {
    form.querySelector("[data-services]").hidden = selectedRole(form) === managerRole;
  }

  function showServicesError(form, text) {
    var error = form.querySelector("[data-services-error]");
    error.textContent = text;
    error.hidden = !text;
  }

  /* Службы к отправке; null — не выбрано ни одной, а уровню они нужны. */
  function chosenServices(form, managerRole) {
    if (selectedRole(form) === managerRole) return [];
    var keys = Array.prototype.map.call(
      form.querySelectorAll("input[name=service_keys]:checked"),
      function (box) { return box.value; });
    showServicesError(form, keys.length ? "" : "Выберите хотя бы одну службу.");
    return keys.length ? keys : null;
  }

  /* Форма с уровнем и службами: блок служб следует за уровнем, ошибка гаснет. */
  function bindRoleForm(form, managerRole) {
    syncServices(form, managerRole);
    form.addEventListener("change", function () {
      syncServices(form, managerRole);
      showServicesError(form, "");
    });
  }

  window.Team = {
    LOGIN_FORMAT: LOGIN_FORMAT,
    LOGIN_FORMAT_TEXT: LOGIN_FORMAT_TEXT,
    suggestLogin: suggestLogin,
    loginTakenText: loginTakenText,
    post: post,
    setBusy: setBusy,
    selectedRole: selectedRole,
    syncServices: syncServices,
    chosenServices: chosenServices,
    bindRoleForm: bindRoleForm
  };
})();
