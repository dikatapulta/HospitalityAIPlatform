/* «Наставник»: «Кодекс» и «Где ошибаются» (spec 0048 §2, §6; #492).
 *
 * Кодекс — перенос static/kodeks.js «Наставника»: поиск с подсветкой, фильтр
 * «Для кого», оглавление, ссылка на статью, печать. Менеджеру — загрузка кодекса:
 * файлы читаются в браузере и уходят JSON'ом (канон JSON-действий кабинета, CSRF —
 * Content-Type и Origin). «Где ошибаются» — «Обнулить статистику». Разметку
 * селекторов задают templates/mentor_codex.html и mentor_stats.html.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  async function postJson(url, body) {
    try {
      var response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      var data = null;
      try { data = await response.json(); } catch (error) { /* не JSON */ }
      var error = !response.ok && data && data.error ? data.error : null;
      return { ok: response.ok, status: response.status, data: data, error: error };
    } catch (error) {
      return { ok: false, status: 0, data: null, error: null };
    }
  }

  function articlesWord(count) {
    var tens = count % 100;
    var ones = count % 10;
    if (ones === 1 && tens !== 11) return "статья";
    if (ones >= 2 && ones <= 4 && (tens < 12 || tens > 14)) return "статьи";
    return "статей";
  }

  /* ---------- загрузка кодекса (менеджер) ---------- */

  var upload = $("codex-upload");
  if (upload) {
    var files = $("codex-files");
    var sendButton = $("codex-send");
    var status = $("codex-status");
    var show = function (text, isError) {
      status.textContent = text;
      status.className = isError ? "error" : "hint";
      status.hidden = false;
    };
    sendButton.addEventListener("click", async function () {
      var chosen = Array.prototype.slice.call(files.files || []);
      if (!chosen.length) { show("Выберите файлы кодекса.", true); return; }
      sendButton.disabled = true;
      show("Загружаю и пересчитываю поиск — это несколько секунд…", false);
      var payload = [];
      for (var i = 0; i < chosen.length; i += 1) {
        payload.push({ name: chosen[i].name, text: await chosen[i].text() });
      }
      var result = await postJson(upload.dataset.endpoint, { files: payload });
      sendButton.disabled = false;
      if (result.status === 401) { window.location.reload(); return; }
      if (result.ok) {
        var count = result.data.article_count;
        show("Кодекс загружен: " + count + " " + articlesWord(count) + ".", false);
        window.setTimeout(function () { window.location.reload(); }, 1200);
        return;
      }
      if (result.error && result.error.code === "ERR-MENTOR-001") { show(result.error.message, true); return; }
      if (result.status === 0) { show("Нет связи — попробуйте ещё раз.", true); return; }
      var code = result.error ? result.error.code : String(result.status);
      show("Не получилось загрузить (" + code + "). Попробуйте ещё раз.", true);
    });
  }

  /* ---------- «Обнулить статистику» (менеджер) ---------- */

  var resetStats = $("stats-reset");
  if (resetStats) {
    resetStats.addEventListener("click", async function () {
      if (!window.confirm("Обнулить статистику тренировок? Вернуть её будет нельзя.")) return;
      resetStats.disabled = true;
      var result = await postJson(resetStats.dataset.endpoint, {});
      if (result.ok || result.status === 401) { window.location.reload(); return; }
      resetStats.disabled = false;
      var line = $("stats-status");
      line.textContent = "Не получилось обнулить — попробуйте ещё раз.";
      line.hidden = false;
    });
  }

  /* ---------- кодекс: поиск, «Для кого», оглавление ---------- */

  var q = $("kx-q");
  if (!q) return;
  var pos = $("kx-pos");
  var found = $("kx-found");
  var toc = $("kx-toc");
  var toast = $("kx-toast");
  var articles = Array.prototype.slice.call(document.querySelectorAll(".kx-art"));
  /* Разметка статей — серверная, экранированная: её можно восстанавливать как есть. */
  var original = new Map(articles.map(function (a) { return [a, a.innerHTML]; }));
  var POS_KEY = "mentor-codex-position";
  /* ё и е — одна буква: «объём» находится по «объем». */
  var norm = function (s) { return s.toLowerCase().replace(/ё/g, "е"); };
  var searchText = new Map(articles.map(function (a) { return [a, norm(a.textContent)]; }));

  /* Подсветка: режем текстовые узлы и оборачиваем совпадения в <mark>; запрос
   * вставляется как текст, поэтому не может стать разметкой. */
  function highlight(el, needle) {
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(function (node) {
      var text = node.nodeValue;
      var low = norm(text);
      var i = low.indexOf(needle);
      if (i < 0) return;
      var frag = document.createDocumentFragment();
      var last = 0;
      while (i >= 0) {
        frag.append(text.slice(last, i));
        var mark = document.createElement("mark");
        mark.textContent = text.slice(i, i + needle.length);
        frag.append(mark);
        last = i + needle.length;
        i = low.indexOf(needle, last);
      }
      frag.append(text.slice(last));
      node.replaceWith(frag);
    });
  }

  function apply() {
    var needle = norm(q.value.trim());
    var chosen = pos ? pos.value : "";
    var shown = 0;
    articles.forEach(function (a) {
      a.innerHTML = original.get(a);
      var position = a.closest(".kx-ch").dataset.position;  /* у глав общей части её нет */
      var forMe = !chosen || !position || position === chosen;
      a.hidden = !(forMe && (!needle || searchText.get(a).indexOf(needle) >= 0));
      if (!a.hidden) {
        shown += 1;
        if (needle) highlight(a, needle);
      }
    });
    document.querySelectorAll(".kx-ch, .kx-sec, .kx-part").forEach(function (box) {
      box.hidden = !box.querySelector(".kx-art:not([hidden])");
    });
    toc.querySelectorAll(".toc-ch[data-position]").forEach(function (t) {
      t.hidden = !!chosen && t.dataset.position !== chosen;
    });
    toc.querySelectorAll(".toc-sec").forEach(function (t) {
      var chapters = Array.prototype.slice.call($(t.dataset.section).querySelectorAll(".kx-ch"));
      t.hidden = !!chosen && !chapters.some(function (c) {
        return !c.dataset.position || c.dataset.position === chosen;
      });
    });
    found.hidden = !needle;
    found.textContent = "Найдено статей: " + shown;
    $("kx-empty").hidden = shown > 0;
  }

  /* Ссылка на статью, скрытую фильтром или поиском, всё равно открывается. */
  function revealTarget() {
    var target = window.location.hash && document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
    if (!target) return;
    if (target.offsetParent === null) {
      q.value = "";
      if (pos) pos.value = "";
      apply();
    }
    target.scrollIntoView();
  }

  function showToast(text) {
    toast.textContent = text;
    toast.hidden = false;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(function () { toast.hidden = true; }, 2200);
  }

  q.addEventListener("input", apply);
  if (pos) {
    try { pos.value = window.localStorage.getItem(POS_KEY) || ""; } catch (error) { /* приватный режим */ }
    if (pos.selectedIndex < 0) pos.value = "";
    pos.addEventListener("change", function () {
      try { window.localStorage.setItem(POS_KEY, pos.value); } catch (error) { /* не запоминаем */ }
      apply();
    });
  }
  $("kx-print").addEventListener("click", function () { window.print(); });

  /* Кнопка у статьи: адрес статьи — в строку браузера и в буфер. Обработчик на
   * документе: подсветка поиска пересоздаёт кнопки внутри статей. */
  document.addEventListener("click", async function (event) {
    var button = event.target.closest(".kx-link");
    if (!button) return;
    window.history.replaceState(null, "", "#" + button.dataset.anchor);
    try {
      await navigator.clipboard.writeText(window.location.href);
      showToast("Ссылка на статью скопирована");
    } catch (error) {
      showToast("Ссылка на статью — в адресной строке");
    }
  });

  /* Оглавление: на компьютере открыто всегда, на телефоне закрывается после перехода. */
  var wide = window.matchMedia("(min-width: 1100px)");
  var syncToc = function () { if (wide.matches) toc.open = true; };
  wide.addEventListener("change", syncToc);
  syncToc();
  toc.addEventListener("click", function (event) {
    if (event.target.closest("nav a") && !wide.matches) toc.open = false;
  });
  window.addEventListener("hashchange", revealTarget);

  apply();
  revealTarget();
})();
