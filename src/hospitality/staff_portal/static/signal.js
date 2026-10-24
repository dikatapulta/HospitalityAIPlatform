/* Сигнал кабинета (spec 0039 §3, issue #381): звук, счётчик в заголовке вкладки
 * и уведомление на компьютере о невзятых заявках — на каждой странице отеля.
 *
 * У отеля «только кабинет» (staff-бота нет) сигнал — единственный путь, которым
 * заявка зовёт персонал. Поэтому:
 * - опрос идёт и на фоновой вкладке (в отличие от queue.js) — ради фона сигнал
 *   и существует;
 * - собственный отказ сигнал не глотает: 401/403 и четыре неудачных опроса
 *   подряд дают плашку, заголовок вкладки и один нисходящий звук (§3.3).
 *
 * Ванильный JS без сборки (ADR-014), отдельный файл — CSP default-src 'self'.
 * Разметки JS не рисует: плашки и кнопки живут в layout.html, здесь только
 * `hidden` и `document.title`. Звук — Web Audio (генерируется в браузере, без
 * файла и без правки CSP). Данные — `GET …/api/requests/signal` (только `new`,
 * без текста гостя).
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 15000;
  /* Зависший запрос — тоже отказ: без таймаута он держал бы флаг «в полёте»,
   * и сигнал замолчал бы без плашки. */
  var POLL_TIMEOUT_MS = 10000;
  var URGENT_REPEAT_MS = 20000;
  var FAILURES_BEFORE_OFFLINE = 4;

  var root = document.getElementById("signal");
  if (!root) return;
  var signalUrl = root.dataset.url;
  var queueUrl = root.dataset.queueUrl;
  var baseTitle = document.title;

  var banners = {};
  root.querySelectorAll("[data-signal-banner]").forEach(function (element) {
    banners[element.dataset.signalBanner] = element;
  });
  var notifyBlock = root.querySelector("[data-signal-notify]");
  var notifyButton = root.querySelector("[data-signal-notify-on]");
  var notifyDenied = root.querySelector("[data-signal-notify-denied]");
  var soundButton = root.querySelector("[data-signal-sound-on]");

  var FAILURE_TITLES = {
    "logged-out": "⚠ Войдите заново — кабинет не зовёт",
    offline: "⚠ Нет связи — кабинет не зовёт"
  };

  /* ---------- Звук ---------- */

  var AudioContextClass = window.AudioContext || window.webkitAudioContext;
  var audio = null;
  try {
    audio = AudioContextClass ? new AudioContextClass() : null;
  } catch (error) {
    audio = null;
  }

  /* [частота Гц, сдвиг с, длительность с]: новая заявка и просрочка — короткое
   * «динь-дон» вверх; срочная — три частых высоких; отказ сигнала — нисходящий,
   * чтобы его не спутали с заявкой. */
  var SOUNDS = {
    chime: [[880, 0, 0.18], [1318.5, 0.16, 0.3]],
    urgent: [[1046.5, 0, 0.14], [1046.5, 0.2, 0.14], [1046.5, 0.4, 0.14]],
    failure: [[659.3, 0, 0.25], [440, 0.25, 0.5]]
  };

  function soundOn() {
    return audio !== null && audio.state === "running";
  }

  function tone(frequency, offset, duration) {
    var start = audio.currentTime + offset;
    var oscillator = audio.createOscillator();
    var gain = audio.createGain();
    oscillator.type = "triangle";
    oscillator.frequency.setValueAtTime(frequency, start);
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.4, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
    oscillator.connect(gain);
    gain.connect(audio.destination);
    oscillator.start(start);
    oscillator.stop(start + duration + 0.05);
  }

  function play(name) {
    if (!soundOn()) return;
    try {
      SOUNDS[name].forEach(function (note) { tone(note[0], note[1], note[2]); });
    } catch (error) {
      /* Звук не сыграл — счётчик, плашки и уведомления работают без него. */
    }
  }

  /* Плашка — по состоянию звука в браузере, а не по «было ли нажатие на этой
   * странице»: Chromium переносит разрешение на следующие страницы сайта, и
   * там плашки быть не должно (§3.4). Без Web Audio плашка бессмысленна —
   * нажатие ничего не включит. */
  function updateSoundBanner() {
    banners["sound-off"].hidden = audio === null || soundOn();
  }

  function unlockAudio() {
    if (audio === null || soundOn()) return Promise.resolve();
    return audio.resume().then(updateSoundBanner, updateSoundBanner);
  }

  if (audio !== null) {
    audio.addEventListener("statechange", updateSoundBanner);
    /* Звук включает первое нажатие в ЛЮБОМ месте страницы (§3.4). */
    ["click", "keydown", "touchend"].forEach(function (type) {
      document.addEventListener(type, unlockAudio, true);
    });
    unlockAudio();
  }
  updateSoundBanner();
  soundButton.addEventListener("click", function () {
    /* Сотрудник слышит, что звук заработал, — это и проверка колонок. */
    unlockAudio().then(function () { play("chime"); });
  });

  /* ---------- Уведомления на компьютере ---------- */

  /* Android Chrome без service worker и iOS Safari вне установленного
   * веб-приложения уведомлений не показывают (§3.4) — там кнопки нет. */
  function isMobile() {
    var data = navigator.userAgentData;
    if (data && typeof data.mobile === "boolean") return data.mobile;
    return /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
  }
  var notificationsSupported = "Notification" in window && !isMobile();

  function updateNotifyControls() {
    var permission = notificationsSupported ? Notification.permission : "unsupported";
    notifyBlock.hidden = permission !== "default";
    notifyDenied.hidden = permission !== "denied";
  }
  updateNotifyControls();
  notifyButton.addEventListener("click", function () {
    try {
      Notification.requestPermission().then(updateNotifyControls, updateNotifyControls);
    } catch (error) {
      updateNotifyControls();
    }
  });

  /* У строк до дневной нумерации номера нет — уведомление без «#». */
  function numberOf(item) {
    return item.daily_number === null ? "" : " #" + item.daily_number;
  }

  function placeLine(item) {
    return item.room_number ? "Комната " + item.room_number + " · " + item.category_name
      : item.category_name;
  }

  function notify(title, item, kind, extra) {
    if (!notificationsSupported || Notification.permission !== "granted") return;
    /* Кабинет перед глазами — хватает звука и счётчика. */
    if (!document.hidden && document.hasFocus()) return;
    try {
      var notification = new Notification(title, {
        body: placeLine(item) + (extra || ""),
        tag: "request-" + item.id + "-" + kind
      });
      notification.onclick = function () {
        window.focus();
        window.location.href = queueUrl;
        notification.close();
      };
    } catch (error) {
      /* Браузер не умеет уведомления со страницы — остаются звук и счётчик. */
    }
  }

  /* ---------- Что уже прозвучало ---------- */

  /* sessionStorage вкладки, а не память страницы: иначе заявка, пришедшая,
   * пока сотрудник переходил со страницы на страницу, тихо вошла бы в «уже
   * виденное» (§3.3). Хранилище недоступно — работает память страницы.
   * `null` — первое открытие кабинета в этой вкладке. Версия в ключе: записи
   * без службы (до spec 0042) после выкатки прочитались бы как «ничего не
   * звучало», и все невзятые прозвучали бы разом. Список `raised` (третья
   * ступень, spec 0045 §4) добавлен без смены версии: у записи без него уже
   * поднятые заявки молча считаются прозвучавшими. */
  var storageKey = "staff-signal:v2:" + signalUrl;

  function readSeen() {
    try {
      var stored = JSON.parse(window.sessionStorage.getItem(storageKey) || "null");
      if (stored && Array.isArray(stored.announced) && Array.isArray(stored.overdue)) {
        return stored;
      }
    } catch (error) {
      /* Нет хранилища или запись испорчена — как первое открытие. */
    }
    return null;
  }

  function writeSeen(value) {
    try {
      window.sessionStorage.setItem(storageKey, JSON.stringify(value));
    } catch (error) {
      /* Память страницы — `seen` ниже. */
    }
  }

  function asSet(ids) {
    var set = {};
    ids.forEach(function (id) { set[id] = true; });
    return set;
  }

  /* «Прозвучало» — пара «заявка + служба» (spec 0042 §2.4): переданная в другую
   * службу невзятая заявка звучит заново, для новой службы она и есть новая. */
  function announceKey(item) {
    return item.id + "|" + item.category_name;
  }

  var seen = readSeen();

  /* ---------- Срочная: повтор каждые 20 с до «Взять» ---------- */

  var hasUrgent = false;
  var failureState = null;
  var urgentTimer = null;

  function urgentTick() {
    urgentTimer = null;
    if (!hasUrgent || failureState !== null) return;
    play("urgent");
    urgentTimer = setTimeout(urgentTick, URGENT_REPEAT_MS);
  }

  function urgentNow() {
    clearTimeout(urgentTimer);
    urgentTick();
  }

  function keepUrgentRepeating() {
    if (hasUrgent && urgentTimer === null) urgentTimer = setTimeout(urgentTick, URGENT_REPEAT_MS);
  }

  /* ---------- Опрос ---------- */

  function updateTitle(count) {
    if (failureState !== null) {
      document.title = FAILURE_TITLES[failureState];
    } else if (count > 0) {
      document.title = (hasUrgent ? "🚨 " : "") + "(" + count + ") " + baseTitle;
    } else {
      document.title = baseTitle;
    }
  }

  /* Что изменилось с прошлого опроса. Бросает на непонятном ответе — и это
   * отказ опроса, а не тишина (pollOnce). */
  function compare(unclaimed) {
    var first = seen === null;
    var announced = first ? {} : asSet(seen.announced);
    var overdueSeen = first ? {} : asSet(seen.overdue);
    /* null — запись без `raised` (до spec 0045): поднятые не звучат. */
    var raisedSeen = first || !Array.isArray(seen.raised) ? null : asSet(seen.raised);
    var changes = { first: first, fresh: [], newlyOverdue: [], newlyRaised: [] };
    /* Новое «уже прозвучало» — только из текущего списка: взятые и закрытые
     * выпадают сами, и хранилище не растёт. */
    var next = { announced: [], overdue: [], raised: [] };
    unclaimed.forEach(function (item) {
      if (!first && !announced[announceKey(item)]) {
        changes.fresh.push(item);
      } else if (!first && item.is_raised && raisedSeen !== null && !raisedSeen[item.id]) {
        changes.newlyRaised.push(item);
      } else if (!first && item.is_overdue && !overdueSeen[item.id]) {
        changes.newlyOverdue.push(item);
      }
      next.announced.push(announceKey(item));
      if (item.is_overdue) next.overdue.push(item.id);
      if (item.is_raised) next.raised.push(item.id);
    });
    seen = next;
    writeSeen(seen);
    return changes;
  }

  function announce(unclaimed, changes) {
    hasUrgent = unclaimed.some(function (item) { return item.is_urgent; });
    updateTitle(unclaimed.length);

    /* Первое открытие: звучат только срочные — остальное уже видно счётчиком
     * и в очереди (§3.3). */
    var urgentArrived = changes.fresh.some(function (item) { return item.is_urgent; });
    if (urgentArrived || (changes.first && hasUrgent)) {
      urgentNow();
    } else if (
      changes.fresh.length > 0 || changes.newlyOverdue.length > 0 || changes.newlyRaised.length > 0
    ) {
      play("chime");
    }
    keepUrgentRepeating();

    /* Третья ступень (spec 0045 §4): ресепшен и менеджер слышат заявку, которую
     * служба не взяла за два срока. Впервые увиденная поднятая заявка для них
     * не «новая» — её ждут давно. */
    function notifyRaised(item) {
      notify("Никто не взял заявку" + numberOf(item), item, "raised", " · ждёт " + item.age_label);
    }
    changes.fresh.forEach(function (item) {
      if (item.is_raised && !item.is_urgent) {
        notifyRaised(item);
        return;
      }
      var title = item.is_urgent ? "🚨 Срочная заявка" : "Новая заявка";
      notify(title + numberOf(item), item, "new");
    });
    changes.newlyRaised.forEach(notifyRaised);
    changes.newlyOverdue.forEach(function (item) {
      notify("Заявка" + numberOf(item) + " ждёт уже " + item.age_label, item, "overdue");
    });
  }

  function setBanner(state) {
    banners["logged-out"].hidden = state !== "logged-out";
    banners.offline.hidden = state !== "offline";
  }

  /* Отказ звучит один раз на эпизод: повтор превратил бы короткий деплой в
   * сирену. Данные о срочной устарели — её повтор молчит до первого удачного
   * опроса. */
  function enterFailure(state) {
    var wasHealthy = failureState === null;
    if (!wasHealthy && failureState !== "offline") return;
    failureState = state;
    hasUrgent = false;
    clearTimeout(urgentTimer);
    urgentTimer = null;
    setBanner(state);
    updateTitle(0);
    if (wasHealthy) play("failure");
  }

  var failures = 0;

  function countFailure() {
    failures += 1;
    if (failures >= FAILURES_BEFORE_OFFLINE && failureState === null) enterFailure("offline");
  }

  async function pollOnce() {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, POLL_TIMEOUT_MS);
    try {
      var response = await fetch(signalUrl, {
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: controller.signal
      });
      /* Сессию закрыли или истёк её срок; 403 — в этом браузере вошли в другой
       * отель. На вход сигнал НЕ уходит, как queue.js: в фоне это тоже тишина. */
      if (response.status === 401 || response.status === 403) {
        enterFailure("logged-out");
        return;
      }
      if (!response.ok) {
        countFailure();
        return;
      }
      var data = await response.json();
      /* Отказ снимает только разобранный ответ: ошибка разбора тоже дойдёт до
       * плашки, а не замолчит, и не сбросит её на каждом опросе. */
      var changes = compare(data.unclaimed);
      failures = 0;
      failureState = null;
      setBanner(null);
      announce(data.unclaimed, changes);
    } catch (error) {
      countFailure();
    } finally {
      clearTimeout(timer);
    }
  }

  var inFlight = false;
  var again = false;

  async function poll() {
    if (inFlight) {
      again = true;
      return;
    }
    inFlight = true;
    try {
      await pollOnce();
    } finally {
      inFlight = false;
    }
    if (again) {
      again = false;
      poll();
    }
  }

  setInterval(poll, POLL_INTERVAL_MS);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) poll();
  });
  /* «Взять» в очереди (queue.js) — опрос сразу: повтор срочной не должен
   * звучать ещё 15 секунд после нажатия. */
  document.addEventListener("staff-queue-changed", poll);
  poll();
})();
