/* «Наставник»: чат «Спросить» и «Тренировки» с потоком ответа (spec 0048 §5, §7; #492).
 *
 * Перенос static/app.js «Наставника». Поток text/event-stream читается fetch'ем
 * (EventSource не умеет POST с телом): события delta {text}…, затем done {answer,
 * sources} или error {code, message}. Текст модели и сотрудника вставляется только
 * как текст: абзацы, **жирный**, [Статья N] → ссылка на кодекс и метка оценки
 * собираются узлами DOM, без innerHTML — ответ модели может нести любой текст.
 * Разметку селекторов задаёт templates/mentor_chat.html.
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

  var busy = false;
  var writing = null;  // AbortController ответа, который сейчас пишется
  var epoch = 0;       // «Начать заново» — ответ, пришедший после, уже не к этой ленте

  var GRADE = /^\s*\**Оценка:?\**\s*\**(частично верно|неверно|верно)\**\.?[ \t]*\n*/i;
  var GRADE_CLASS = { "верно": "ok", "частично верно": "part", "неверно": "bad" };
  var INLINE = /\*\*(.+?)\*\*|\[Статья (\d+(?:-\d+)?)\]/g;
  var NO_QUESTION = "ERR-MENTOR-003";

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

  /* Ответ наставника: в «Тренировке» первая строка «Оценка: …» — цветной меткой. */
  function fill(msg, content, sources) {
    var body = msg.querySelector(".mentor-text");
    var old = msg.querySelector(".mentor-grade");
    if (old) old.remove();
    var match = mode === "quiz" ? GRADE.exec(content) : null;
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
      var line = document.createElement("p");
      line.className = "mentor-sources";
      sources.forEach(function (source, index) {
        if (index) line.appendChild(document.createElement("br"));
        line.appendChild(document.createTextNode("Источник: "));
        var link = document.createElement("a");
        link.href = codexHref + "#" + source.anchor;
        link.textContent = "Статья " + source.number + ". " + source.title;
        line.appendChild(link);
      });
      msg.appendChild(line);
    }
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
    resetButton.hidden = false;
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
              answer: data.answer, sources: data.sources };
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
    if (quizNew) quizNew.disabled = value || !hasCodex;
    log.querySelectorAll(".mentor-next").forEach(function (button) { button.disabled = value; });
  }

  function updateInput() {
    if (mode !== "quiz") return;
    input.disabled = !quizOpen;
    input.placeholder = quizOpen ? "Ваш ответ…" : "Сначала нажмите «Новый вопрос»";
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  }

  async function submit(message) {
    if (!message || busy) return;
    if (mode === "quiz" && !quizOpen) return;
    addUser(message);
    input.value = "";
    autosize();
    if (mode !== "quiz") {
      await call("/ask", { message: message });
      return;
    }
    var mine = epoch;
    quizOpen = false;
    updateInput();
    var result = await call("/quiz/answer", { message: message });
    if (mine !== epoch) return;
    if (result && result.type === "done") {
      addNext();
    } else if (!result || result.code !== NO_QUESTION) {
      quizOpen = true;  /* проверка не дошла — вопрос остаётся, можно ответить ещё раз */
      updateInput();
    }
  }

  async function newQuestion() {
    if (busy) return;
    log.querySelectorAll(".mentor-next").forEach(function (button) { button.remove(); });
    var chapter = topic && topic.value ? Number(topic.value) : null;
    var own = topicText && topicText.value.trim() ? topicText.value.trim() : null;
    var mine = epoch;
    var result = await call("/quiz/new", { chapter_number: chapter, text: own }, root.dataset.label);
    if (mine !== epoch) return;
    quizOpen = !!(result && result.type === "done");
    updateInput();
    if (quizOpen && window.matchMedia("(hover: hover)").matches) input.focus();
  }

  /* Под оценкой — сразу следующий вопрос на ту же тему. */
  function addNext() {
    var last = log.lastElementChild;
    if (!last) return;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-outline mentor-next";
    button.textContent = "Следующий вопрос";
    button.addEventListener("click", newQuestion);
    last.appendChild(button);
    scrollDown();
  }

  /* «Начать заново» — работает и пока наставник пишет: экран очищается сразу,
   * запоздавший ответ в новую ленту не попадёт, а сервер забывает диалог режима. */
  resetButton.addEventListener("click", function () {
    epoch += 1;
    if (writing) writing.abort();
    log.querySelectorAll(".mentor-msg").forEach(function (msg) { msg.remove(); });
    hello.hidden = false;
    resetButton.hidden = true;
    quizOpen = false;
    updateInput();
    fetch(api + "/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: mode })
    }).catch(function () { /* нет связи: экран очищен, диалог сотрётся при следующем нажатии */ });
  });

  if (quizNew) quizNew.addEventListener("click", newQuestion);

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
  if (mode === "quiz" && !quizOpen && lastMsg && lastMsg.classList.contains("mentor-msg-bot") &&
      lastMsg.querySelector(".mentor-grade")) {
    addNext();
  }
  updateInput();
  if (log.querySelector(".mentor-msg")) scrollDown();
})();
