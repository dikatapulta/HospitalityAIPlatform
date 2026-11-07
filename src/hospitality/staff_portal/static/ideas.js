/* «Предложить идею» (spec 0071 §3, §8–9; #871): черновик карточки поверх чата
 * static/mentor_chat.js, «Удалить» в «Моих идеях» и «Разобрано» во «Всех идеях».
 *
 * Черновик — #idea-draft в конце ленты: поле с пересказом и кнопки «Отправить
 * разработчику» · «Продолжить разговор». Правка текста модель не зовёт (§3 п. 3):
 * пересказ уходит из поля в POST /send. Новое предложение модели (done с draft)
 * заменяет текст и переносит карточку в конец ленты; новое сообщение автора её
 * прячет. Тексты — #idea-texts (templates/ideas_chat.html); всё — узлами DOM.
 */
(function () {
  "use strict";

  function post(endpoint, body) {
    return fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  async function errorCode(response) {
    try {
      var data = await response.json();
      return data && data.error ? data.error.code : null;
    } catch (error) {
      return null;
    }
  }

  /* ---------- «Мои идеи» и «Все идеи»: кнопки карточек ---------- */

  document.querySelectorAll(".idea-delete").forEach(function (button) {
    button.addEventListener("click", async function () {
      if (!window.confirm(button.dataset.confirm)) return;
      button.disabled = true;
      var response = await post(button.dataset.endpoint).catch(function () { return null; });
      if (response && response.ok) button.closest(".idea-card").remove();
      else button.disabled = false;
    });
  });

  document.querySelectorAll(".idea-reviewed").forEach(function (button) {
    button.addEventListener("click", async function () {
      button.disabled = true;
      var response = await post(button.dataset.endpoint).catch(function () { return null; });
      if (response && response.ok) window.location.reload();
      else button.disabled = false;
    });
  });

  /* ---------- чат: черновик карточки ---------- */

  var chat = window.MentorChat;
  var card = document.getElementById("idea-draft");
  var textsNode = document.getElementById("idea-texts");
  if (!chat || !card || !textsNode) return;

  var texts = JSON.parse(textsNode.textContent);
  var api = chat.root.dataset.api;
  var field = document.getElementById("idea-draft-text");
  var sendButton = document.getElementById("idea-send");
  var continueButton = document.getElementById("idea-continue");
  var note = document.getElementById("idea-draft-note");
  var sentNote = null;

  function hideCard() {
    card.hidden = true;
    note.hidden = true;
  }

  function clearSentNote() {
    if (sentNote) sentNote.remove();
    sentNote = null;
  }

  function showNote(text) {
    note.textContent = text;
    note.hidden = false;
  }

  chat.idea.sending = function () {
    hideCard();
    clearSentNote();
  };

  chat.idea.done = function (result) {
    if (!result.draft) return;
    card.dataset.id = result.draft.id;
    field.value = result.draft.summary;
    note.hidden = true;
    chat.log.appendChild(card);
    card.hidden = false;
    chat.scrollDown();
  };

  chat.idea.reset = function () {
    hideCard();
    clearSentNote();
    delete card.dataset.id;
  };

  continueButton.addEventListener("click", function () {
    hideCard();
    chat.input.focus();
  });

  sendButton.addEventListener("click", async function () {
    var summary = field.value.trim();
    if (!summary || !card.dataset.id || chat.isBusy()) return;
    sendButton.disabled = true;
    var response = await post(api + "/send", { idea_id: card.dataset.id, summary: summary })
      .catch(function () { return null; });
    sendButton.disabled = false;
    if (!response || !response.ok) {
      var code = response ? await errorCode(response) : null;
      showNote(code === "ERR-MENTOR-005" ? texts.already_sent : texts.unavailable);
      return;
    }
    /* Отправлено: диалог закрыт — лента начинается заново с приветствия. */
    chat.log.querySelectorAll(".mentor-msg").forEach(function (msg) { msg.remove(); });
    chat.idea.reset();
    var reset = document.getElementById("mentor-reset");
    if (reset) reset.hidden = true;
    sentNote = document.createElement("p");
    sentNote.className = "idea-sent";
    sentNote.textContent = texts.sent;
    chat.log.appendChild(sentNote);
    chat.scrollDown();
  });

  if (!card.hidden) chat.log.appendChild(card);
})();
