/* «Наставник»: ход экзамена (spec 0048 §12; #779) поверх чата static/mentor_chat.js.
 *
 * «Начать экзамен» — POST /exam/start (JSON: ход попытки), затем вопрос потоком
 * /exam/next. Ответ уходит в /exam/answer с attempt_id и turn — попыткой и номером
 * хода, которые видит сотрудник: вопрос сменился в другой вкладке — ERR-MENTOR-004,
 * а не проверка не того вопроса. В done проверки — ход: засчитан ли ответ, итог.
 * Тексты и ход попытки при загрузке — #mentor-exam-state (templates/mentor_chat.html);
 * всё — узлами DOM, без innerHTML.
 */
(function () {
  "use strict";

  var chat = window.MentorChat;
  var stateNode = document.getElementById("mentor-exam-state");
  if (!chat || !stateNode) return;

  var state = JSON.parse(stateNode.textContent);
  var texts = state.texts;
  var progress = state.progress;  /* идущая попытка или null */
  var api = chat.root.dataset.api;
  var topic = document.getElementById("exam-topic");
  var start = document.getElementById("exam-start");

  function format(template, values) {
    return template.replace(/\{(\w+)\}/g, function (all, key) {
      return values[key] === undefined ? all : String(values[key]);
    });
  }

  function addLine(className, text) {
    var line = document.createElement("p");
    line.className = className;
    line.textContent = text;
    chat.log.appendChild(line);
    chat.scrollDown();
  }

  function setClosed(text) {
    chat.input.dataset.closed = text;
    chat.setOpen(false);
  }

  function showResult(exam) {
    var card = document.createElement("div");
    card.className = "mentor-result " + (exam.passed ? "mentor-result-pass" : "mentor-result-fail");
    var title = document.createElement("b");
    title.textContent = format(exam.passed ? texts.passed : texts.failed, exam);
    var score = document.createElement("p");
    score.textContent = format(texts.score, exam);
    card.appendChild(title);
    card.appendChild(score);
    chat.log.appendChild(card);
    chat.scrollDown();
  }

  async function next() {
    if (chat.isBusy() || !progress) return;
    chat.log.querySelectorAll(".mentor-next").forEach(function (button) { button.remove(); });
    var label = format(texts.label, { n: progress.answered + 1, total: progress.total });
    var result = await chat.call("/exam/next", { attempt_id: progress.attempt_id }, label);
    if (result && result.type === "done" && result.exam) {
      progress = result.exam;
      chat.setOpen(true);
      if (window.matchMedia("(hover: hover)").matches) chat.input.focus();
    } else if (!result || result.code !== "ERR-MENTOR-004") {
      chat.addNext(next);  /* вопрос не дошёл — можно взять ещё раз */
    }
  }

  chat.exam.answer = function (message) {
    return chat.call("/exam/answer", {
      message: message, attempt_id: progress.attempt_id, turn: progress.turn
    });
  };

  chat.exam.checked = function (result) {
    progress = result.exam;
    if (!progress.counted) addLine("mentor-note", texts.not_counted);
    if (progress.finished) {
      showResult(progress);
      progress = null;
      setClosed(texts.closed);
    } else {
      chat.addNext(next);
    }
  };

  start.addEventListener("click", async function () {
    if (chat.isBusy()) return;
    if (progress && !window.confirm(texts.restart)) return;
    chat.setBusy(true);
    var response = null;
    try {
      response = await fetch(api + "/exam/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chapter_number: topic.value ? Number(topic.value) : null })
      });
    } catch (error) { /* нет связи — ниже та же строка, что при отказе */ }
    chat.setBusy(false);
    if (response && response.status === 401) {
      window.location.reload();  /* сессия истекла: страница уведёт на вход */
      return;
    }
    if (!response || !response.ok) {
      addLine("mentor-cut", chat.root.dataset.unavailable);
      return;
    }
    progress = await response.json();
    chat.log.querySelectorAll(".mentor-msg, .mentor-next, .mentor-note, .mentor-cut, .mentor-result")
      .forEach(function (node) { node.remove(); });
    chat.hello.hidden = true;
    setClosed(texts.waiting);
    await next();
  });

  /* Попытка идёт, а открытого вопроса нет (вопрос оборвался, страницу обновили). */
  if (progress && !chat.isOpen()) chat.addNext(next);
})();
