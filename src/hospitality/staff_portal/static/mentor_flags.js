/* «Неточность в статье» (spec 0072 §6): кнопка у статьи «Кодекса» и под проверкой ответа
 * «Тренировки» и «Экзамена». Состояние — #mentor-flags-state (адрес, свои отметки, тексты),
 * его кладёт сервер (staff_portal/mentor_flags.page_context). Нажатие отмечает статью,
 * повторное — снимает; вид кнопки — по ответу сервера: статьи нет в нынешнем кодексе —
 * отметка не встаёт. Кнопки «Кодекса» — .mentor-flag[data-article] из шаблона; под
 * проверкой ответа их строит static/mentor_chat.js через window.MentorFlags.button(номер),
 * поэтому этот скрипт подключается раньше него.
 */
(function () {
  "use strict";

  var state = document.getElementById("mentor-flags-state");
  if (!state) return;
  var config = JSON.parse(state.textContent);
  var mine = new Set(config.mine);
  var texts = config.texts;

  function paint(button) {
    var on = mine.has(button.dataset.article);
    button.textContent = on ? texts.on : texts.off;
    button.setAttribute("aria-pressed", on ? "true" : "false");
    button.title = on ? texts.undo : "";
  }

  /* Одна статья может стоять на странице дважды (две проверки по ней) — красим все. */
  function paintArticle(number) {
    document.querySelectorAll(".mentor-flag[data-article]").forEach(function (button) {
      if (button.dataset.article === number) paint(button);
    });
  }

  async function toggle(button) {
    var number = button.dataset.article;
    button.disabled = true;
    try {
      var response = await fetch(config.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ article_number: number, flagged: !mine.has(number) })
      });
      if (response.ok) {
        if ((await response.json()).flagged) mine.add(number);
        else mine.delete(number);
      }
    } catch (error) { /* нет связи: вид кнопки остаётся прежним */ }
    button.disabled = false;
    paintArticle(number);
  }

  function bind(button) {
    paint(button);
    button.addEventListener("click", function () { toggle(button); });
  }

  function create(number) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-quiet mentor-flag";
    button.dataset.article = number;
    bind(button);
    return button;
  }

  document.querySelectorAll(".mentor-flag[data-article]").forEach(bind);
  window.MentorFlags = { button: create };
})();
