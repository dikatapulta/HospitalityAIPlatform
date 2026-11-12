/* Действия пути номера после выезда (spec 0046 §5): JSON-запросы turnover_api,
 * тексты ошибок по кодам каталога (R-8) и вопрос выселения (ERR-HOUSEKEEPING-004).
 * Общие для «Выездов и уборки» (turnovers.js) и «Номеров» (room_board.js,
 * spec 0061 §4): одни тексты на две страницы (P-12). Подключается перед
 * скриптом страницы; отдельный файл — CSP default-src 'self'.
 *
 * CSRF-контракт JSON-действий (докстринг router.py): каждый POST несёт
 * Content-Type: application/json — Origin браузер добавляет сам.
 */
(function () {
  "use strict";

  var NO_HOUSEKEEPING = "В кабинете пока нет сотрудников хаускипинга — выезд здесь никто " +
    "не увидит. Сообщите им, как раньше.";

  /* Дружелюбные тексты по кодам каталога ошибок (R-8). Отказ по праву зависит
   * от действия: кнопок не по праву на доске нет, 403 значит — права сменились. */
  var MESSAGES = {
    "ERR-HOUSEKEEPING-001": "Этого выезда уже нет на доске — список обновлён.",
    "ERR-HOUSEKEEPING-002": "Этот номер уже отметили — список обновлён.",
    "ERR-HOUSEKEEPING-005": NO_HOUSEKEEPING,
    "ERR-HOUSEKEEPING-007": "Это время сегодня уже прошло — укажите позже.",
    "ERR-HOUSEKEEPING-008": "Такого номера нет в списке номеров отеля — проверьте номер.",
    "ERR-HOUSEKEEPING-009": "Слишком много отметок подряд — подождите несколько минут.",
    "ERR-GUESTS-001": "Гостя в этом номере уже выселили — отметьте выезд ещё раз.",
    "ERR-AUTH-002": "Сессия истекла — войдите заново."
  };
  var FORBIDDEN = {
    accept: "Принимает номер старший хаускипинга или менеджер.",
    cancel: "Отменить выезд может ресепшен, старший хаускипинга или менеджер.",
    deadline: "Срочность отмечает ресепшен, старший хаускипинга или менеджер.",
    report: "Отметить выезд может ресепшен или менеджер."
  };
  var FORBIDDEN_DEFAULT = "Отмечать уборку может хаускипинг.";

  /* POST по CSRF-контракту; вернуть { ok, status, code, data }. */
  async function send(url, body) {
    try {
      var response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      var data = null;
      try { data = await response.json(); } catch (error) { /* не JSON */ }
      var code = !response.ok && data && data.error ? data.error.code : "";
      return { ok: response.ok, status: response.status, code: code, data: data };
    } catch (error) {
      return { ok: false, status: 0, code: "network", data: null };
    }
  }

  function failureText(result, action) {
    if (result.code === "network") return "Нет связи — попробуйте ещё раз.";
    if (result.code === "ERR-HOUSEKEEPING-003") return FORBIDDEN[action] || FORBIDDEN_DEFAULT;
    return MESSAGES[result.code] ||
      "Не получилось (" + (result.code || result.status) + "). Попробуйте ещё раз.";
  }

  /* «Отметить выезд»: гость заселён в кабинете (004) — спросить и повторить с
   * подтверждением выселения; отказ в вопросе — null. */
  async function report(endpoint, room, checkOutStay) {
    var result = await send(endpoint, { room_number: room, check_out_stay: !!checkOutStay });
    if (!result.ok && result.code === "ERR-HOUSEKEEPING-004") {
      var question = "В кабинете в " + room + " заселён гость. Выселить его и отправить " +
        "номер на уборку? Доступ к чату сразу погаснет.";
      if (window.confirm(question)) return report(endpoint, room, true);
      return null;
    }
    return result;
  }

  window.TurnoverActions = { send: send, failureText: failureText, report: report };
})();
