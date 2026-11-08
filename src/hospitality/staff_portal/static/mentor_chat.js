/* «Наставник»: чат «Спросить», «Тренировки» и «Экзамена» с потоком ответа (spec 0048 §5, §7,
 * §12; #492, #779). Ход экзамена — static/mentor_exam.js поверх window.MentorChat; тем же
 * чатом идут «Новая идея» и «Интервью» (spec 0071; черновик — static/ideas.js).
 *
 * Перенос static/app.js «Наставника». Поток text/event-stream читается fetch'ем
 * (EventSource не умеет POST с телом): события delta {text}…, затем done {answer,
 * sources} или error {code, message}. Текст модели и сотрудника вставляется только
 * как текст: абзацы, **жирный**, [Статья N] → ссылка на кодекс и метка оценки
 * собираются узлами DOM, без innerHTML — ответ модели может нести любой текст.
 * Разметку селекторов задаёт templates/mentor_chat.html.
 *
 * «Тренировка» и «Экзамен» внизу показывают одно главное действие на шаг (#888): выбор
 * темы (setup), поле ответа (answer; wait — пока наставник пишет), «Следующий вопрос»
 * (after). Шаг переключает stage(); «Спросить» и идеи — всегда поле ввода.
 */
(function () {
  "use strict";

  var root = document.getElementById("mentor-chat");
  if (!root) return;

  var mode = root.dataset.mode;
  var api = root.dataset.api;
  var codexHref = root.dataset.codex;
  var hasCodex = root.dataset.hasCodex === "true";
  var quizOpen = root.dataset.quizOpen === "true";
  var log = document.getElementById("mentor-log");
  var hello = document.getElementById("mentor-hello");
  var form = document.getElementById("mentor-form");
  var input = document.getElementById("mentor-text");
  var send = document.getElementById("mentor-send");
  var resetButton = document.getElementById("mentor-reset");
  var topic = document.getElementById("quiz-topic");
  var topicText = document.getElementById("quiz-text");
  var quizNew = document.getElementById("quiz-new");
  var quizNext = document.getElementById("quiz-next");
  var quizChange = document.getElementById("quiz-change");
  var score = document.getElementById("quiz-score");
  var setupBox = document.getElementById(mode + "-setup");
  var afterBox = document.getElementById(mode + "-after");
  var exam = {};  /* ответ и проверка экзамена — задаёт static/mentor_exam.js */
  var idea = {};  /* черновик карточки идеи — задаёт static/ideas.js */
  /* Свободный чат: писать можно всегда, без открытого вопроса. */
  var free = mode === "ask" || mode === "idea" || mode === "interview";

  var busy = false;
  var writing = null;  // AbortController ответа, который сейчас пишется
  var epoch = 0;       // «Начать заново» — ответ, пришедший после, уже не к этой ленте

  var GRADE = /^\s*\**Оценка:?\**\s*\**(частично верно|неверно|верно)\**\.?[ \t]*\n*/i;
  var GRADE_CLASS = { "верно": "ok", "частично верно": "part", "неверно": "bad" };
  var GRADE_SCORE = { "верно": "correct", "частично верно": "partial", "неверно": "wrong" };
  var INLINE = /\*\*(.+?)\*\*|\[Статья (\d+(?:-\d+)?)\]/g;
  var NO_QUESTION = ["ERR-MENTOR-003", "ERR-MENTOR-004"];  /* «Тренировка», экзамен */

  /* ---------- разметка ответа ---------- */

  function inline(parent, line) {
    var last = 0;
    var match;
    INLINE.lastIndex = 0;
    while ((match = INLINE.exec(line))) {
      if (match.index > last) parent.appendChild(document.createTextNode(line.slice(last, match.index)));
      if (match[1] !== undefined) {
        var bold = document.createElement("b");
        bold.textContent = match[1];
        parent.appendChild(bold);
      } else if (!codexHref) {
        parent.appendChild(document.createTextNode(match[0]));  /* «Предложить идею»: кодекса нет */
      } else {
        var link = document.createElement("a");
        link.href = codexHref + "#st-" + match[2];
        link.className = "mentor-ref";
        link.textContent = "Статья " + match[2];
        parent.appendChild(link);
      }
      last = INLINE.lastIndex;
    }
    if (last < line.length) parent.appendChild(document.createTextNode(line.slice(last)));
  }

  function paragraphs(body, content) {
    body.textContent = "";
    content.trim().split(/\n{2,}/).forEach(function (block) {
      var p = document.createElement("p");
      block.split("\n").forEach(function (line, index) {
        if (index) p.appendChild(document.createElement("br"));
        var heading = /^#{1,4}\s+(.+)$/.exec(line);
        if (heading) {
          var bold = document.createElement("b");
          bold.textContent = heading[1];
          p.appendChild(bold);
          return;
        }
        inline(p, line.replace(/^[-*•] /, "• "));
      });
      body.appendChild(p);
    });
  }

  /* Ответ наставника: в «Тренировке» и экзамене первая строка «Оценка: …» — цветной меткой. */
  function fill(msg, content, sources) {
    var body = msg.querySelector(".mentor-text");
    var old = msg.querySelector(".mentor-grade");
    if (old) old.remove();
    var match = mode === "quiz" || mode === "exam" ? GRADE.exec(content) : null;
    if (match) {
      var grade = match[1].toLowerCase();
      var badge = document.createElement("div");
      badge.className = "mentor-grade mentor-grade-" + GRADE_CLASS[grade];
      badge.textContent = grade.charAt(0).toUpperCase() + grade.slice(1);
      msg.insertBefore(badge, body);
      content = content.slice(match[0].length);
    }
    paragraphs(body, content);
    if (sources && sources.length) {
      var line = document.createElement("div");
      line.className = "mentor-sources";
      sources.forEach(function (source) {
        var link = document.createElement("a");
        link.className = "mentor-source";
        link.href = codexHref + "#" + source.anchor;
        var number = document.createElement("b");
        number.textContent = "Статья " + source.number;
        link.appendChild(number);
        link.appendChild(document.createTextNode(" " + source.title));
        line.appendChild(link);
      });
      msg.appendChild(line);
    }
  }

  /* Личный счёт «Тренировки»: проверка с разобранной оценкой — плюс один ответ. */
  function countGrade(answer) {
    var match = score ? GRADE.exec(answer || "") : null;
    if (!match) return;
    [GRADE_SCORE[match[1].toLowerCase()], "total"].forEach(function (key) {
      var cell = score.querySelector('[data-score="' + key + '"]');
      cell.textContent = String(Number(cell.textContent) + 1);
    });
    score.hidden = false;
  }

  /* Текст, который ещё пишется: без метки и источников, с курсором в конце. */
  function fillPartial(msg, content) {
    var stick = nearBottom();
    var body = msg.querySelector(".mentor-text");
    paragraphs(body, content);
    var caret = document.createElement("span");
    caret.className = "mentor-caret";
    (body.lastElementChild || body).appendChild(caret);
    if (stick) scrollDown();
  }

  /* Ответ оборвался: написанное остаётся, под ним — красная строка. */
  function cut(msg, content, note) {
    if (content) fill(msg, content, null);
    else msg.querySelector(".mentor-text").textContent = "";
    var line = document.createElement("p");
    line.className = "mentor-cut";
    line.textContent = note;
    msg.appendChild(line);
    scrollDown();
  }

  /* ---------- лента ---------- */

  function nearBottom() {
    var doc = document.documentElement;
    return doc.scrollHeight - window.scrollY - window.innerHeight < 120;
  }

  function scrollDown() {
    window.scrollTo(0, document.documentElement.scrollHeight);
  }

  function append(msg) {
    hello.hidden = true;
    if (resetButton) resetButton.hidden = false;
    log.appendChild(msg);
    scrollDown();
    return msg;
  }

  function addUser(content) {
    var msg = document.createElement("div");
    msg.className = "mentor-msg mentor-msg-user";
    var body = document.createElement("div");
    body.className = "mentor-text";
    body.textContent = content;
    msg.appendChild(body);
    return append(msg);
  }

  function addBot(label) {
    var msg = document.createElement("div");
    msg.className = "mentor-msg mentor-msg-bot";
    if (label) {
      var head = document.createElement("div");
      head.className = "mentor-label";
      head.textContent = label;
      msg.appendChild(head);
    }
    var body = document.createElement("div");
    body.className = "mentor-text";
    var dots = document.createElement("span");
    dots.className = "mentor-dots";
    dots.setAttribute("aria-label", "Думаю");
    dots.appendChild(document.createElement("i"));
    dots.appendChild(document.createElement("i"));
    dots.appendChild(document.createElement("i"));
    body.appendChild(dots);
    msg.appendChild(body);
    return append(msg);
  }

  /* ---------- поток ---------- */

  async function readEvents(response, onEvent) {
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";
    for (;;) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      var end;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        var frame = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        var kind = "message";
        var data = "";
        frame.split("\n").forEach(function (line) {
          if (line.indexOf("event: ") === 0) kind = line.slice(7);
          else if (line.indexOf("data: ") === 0) data += line.slice(6);
        });
        if (data) onEvent(kind, JSON.parse(data));
      }
    }
  }

  async function call(path, body, label) {
    var msg = addBot(label);
    var stop = new AbortController();
    writing = stop;
    setBusy(true);
    var text = "";
    var result = null;
    var frame = 0;
    try {
      var response = await fetch(api + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: stop.signal
      });
      if (response.status === 401) {
        window.location.reload();  /* сессия истекла: страница уведёт на вход */
        return null;
      }
      if ((response.headers.get("Content-Type") || "").indexOf("text/event-stream") !== 0) {
        result = { type: "error", code: String(response.status), message: root.dataset.unavailable };
      } else {
        await readEvents(response, function (kind, data) {
          if (kind === "delta") {
            text += data.text;
            /* Перерисовка не чаще кадра экрана: куски приходят быстрее, чем их видно. */
            frame = frame || requestAnimationFrame(function () {
              frame = 0;
              if (!result) fillPartial(msg, text);
            });
          } else {
            result = { type: kind, code: data.code, message: data.message,
              answer: data.answer, sources: data.sources, exam: data.exam, draft: data.draft };
          }
        });
      }
      if (!result) throw new Error("поток оборвался без конца");
      if (result.type === "error") {
        cut(msg, text, result.message);
        return result;
      }
      fill(msg, result.answer, result.sources);
      scrollDown();
      return result;
    } catch (error) {
      if (stop.signal.aborted) return null;  /* «Начать заново»: сообщение уже убрано */
      cut(msg, text, root.dataset.cut);
      return null;
    } finally {
      cancelAnimationFrame(frame);
      if (writing === stop) writing = null;
      setBusy(false);
    }
  }

  /* ---------- режимы ---------- */

  function setBusy(value) {
    busy = value;
    send.disabled = value || !hasCodex;
    root.querySelectorAll(".mentor-setup .btn, .mentor-after .btn").forEach(function (button) {
      button.disabled = value || !hasCodex;
    });
  }

  function updateInput() {
    if (free) return;
    input.disabled = !quizOpen;
  }

  /* Шаг низа экрана: setup — тема, answer и wait — поле ответа, after — «Следующий вопрос». */
  function stage(name) {
    if (free) return;
    if (setupBox) setupBox.hidden = name !== "setup";
    if (afterBox) afterBox.hidden = name !== "after";
    form.hidden = name !== "answer" && name !== "wait";
    if (name === "wait") input.disabled = true;
    else updateInput();
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  }

  async function submit(message) {
    if (!message || busy) return;
    if (!free && !quizOpen) return;
    if (idea.sending) idea.sending();
    addUser(message);
    input.value = "";
    autosize();
    if (mode === "ask") {
      await call("/ask", { message: message });
      return;
    }
    if (free) {
      var turn = await call("/turn", { mode: mode, message: message });
      if (turn && turn.type === "done" && idea.done) idea.done(turn);
      return;
    }
    var mine = epoch;
    quizOpen = false;
    stage("wait");
    var result = mode === "exam" ? await exam.answer(message) :
      await call("/quiz/answer", { message: message });
    if (mine !== epoch) return;
    if (result && result.type === "done") {
      if (mode === "exam") {
        exam.checked(result);
      } else {
        countGrade(result.answer);
        stage("after");
      }
    } else if (!result || NO_QUESTION.indexOf(result.code) < 0) {
      quizOpen = true;  /* проверка не дошла — вопрос остаётся, можно ответить ещё раз */
      stage("answer");
    } else {
      stage(mode === "exam" ? "after" : "setup");
    }
  }

  async function newQuestion() {
    if (busy) return;
    var chapter = topic && topic.value ? Number(topic.value) : null;
    var own = topicText && topicText.value.trim() ? topicText.value.trim() : null;
    var mine = epoch;
    stage("wait");
    var result = await call("/quiz/new", { chapter_number: chapter, text: own }, root.dataset.label);
    if (mine !== epoch) return;
    quizOpen = !!(result && result.type === "done");
    stage(quizOpen ? "answer" : "setup");
    if (quizOpen && window.matchMedia("(hover: hover)").matches) input.focus();
  }

  /* «Начать заново» — работает и пока наставник пишет: экран очищается сразу,
   * запоздавший ответ в новую ленту не попадёт, а сервер забывает диалог режима. */
  if (resetButton) resetButton.addEventListener("click", function () {
    epoch += 1;
    if (writing) writing.abort();
    log.querySelectorAll(".mentor-msg").forEach(function (msg) { msg.remove(); });
    hello.hidden = false;
    resetButton.hidden = true;
    quizOpen = false;
    stage("setup");
    if (idea.reset) idea.reset();
    fetch(api + "/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: mode })
    }).catch(function () { /* нет связи: экран очищен, диалог сотрётся при следующем нажатии */ });
  });

  if (quizNew) quizNew.addEventListener("click", newQuestion);
  if (quizNext) quizNext.addEventListener("click", newQuestion);
  if (quizChange) quizChange.addEventListener("click", function () { stage("setup"); });
  root.querySelectorAll(".mentor-suggestion").forEach(function (button) {
    button.addEventListener("click", function () { submit(button.textContent.trim()); });
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    submit(input.value.trim());
  });

  input.addEventListener("input", autosize);
  input.addEventListener("keydown", function (event) {
    /* Enter отправляет на компьютере; на телефоне Enter — перенос строки. */
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing &&
        window.matchMedia("(hover: hover)").matches) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  /* История с сервера — тот же разбор, что у потока. */
  log.querySelectorAll(".mentor-msg-bot").forEach(function (msg) {
    var body = msg.querySelector(".mentor-text");
    var sources = msg.querySelector(".mentor-sources");
    if (sources) sources.remove();
    fill(msg, body.textContent, null);
    if (sources) msg.appendChild(sources);
  });
  var lastMsg = log.lastElementChild;
  if (quizOpen) stage("answer");
  else if (mode === "quiz" && lastMsg && lastMsg.querySelector(".mentor-grade")) stage("after");
  else stage("setup");
  if (log.querySelector(".mentor-msg")) scrollDown();

  window.MentorChat = {
    exam: exam,
    idea: idea,
    root: root,
    log: log,
    hello: hello,
    input: input,
    call: call,
    stage: stage,
    scrollDown: scrollDown,
    isBusy: function () { return busy; },
    isOpen: function () { return quizOpen; },
    setOpen: function (value) { quizOpen = value; updateInput(); },
    setBusy: setBusy
  };
})();
