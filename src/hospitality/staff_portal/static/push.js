/* Уведомления на телефон — подписка на веб-пуш (spec 0050 §3–§4, issue #477).
 *
 * Только телефоны: на компьютере зовёт вкладка и «Уведомления на компьютере»
 * (signal.js, spec 0039) — пуш там дал бы второе уведомление на то же событие.
 * Признак «телефон» — тот же, что `isMobile` в signal.js: правятся вместе.
 *
 * Что делает на каждой странице отеля:
 * - разрешение ещё не спрашивали — показывает кнопку; спросить можно только по
 *   нажатию (iOS иначе откажет);
 * - разрешение дано — молча сверяет подписку: нет её или ключ сервера сменился —
 *   подписывает заново, и в любом случае записывает на сервер. Так подписка,
 *   удалённая сервером, возвращается при первом открытии кабинета;
 * - iPhone в Safari — подсказка добавить кабинет на экран «Домой»: веб-пуш на
 *   iOS работает только там;
 * - «Выйти» сначала отписывает телефон (не дольше 2 с), потом выходит.
 *
 * Разметки JS не рисует: все состояния — в layout.html, здесь только `hidden`
 * (канон signal.js). Ванильный JS без сборки (ADR-014).
 */
(function () {
  "use strict";

  var RELEASE_TIMEOUT_MS = 2000;
  var IOS_HINT_KEY = "staff-push:ios-hint-dismissed";

  var root = document.getElementById("push");
  if (!root) return;
  var serverKey = keyBytes(root.dataset.key);
  var subscribeUrl = root.dataset.subscribeUrl;
  var deleteUrl = root.dataset.deleteUrl;
  var testUrl = root.dataset.testUrl;

  var states = {};
  root.querySelectorAll("[data-push-state]").forEach(function (element) {
    states[element.dataset.pushState] = element;
  });
  var testResults = {};
  root.querySelectorAll("[data-push-test-result]").forEach(function (element) {
    testResults[element.dataset.pushTestResult] = element;
  });

  function show() {
    var visible = Array.prototype.slice.call(arguments);
    Object.keys(states).forEach(function (name) {
      states[name].hidden = visible.indexOf(name) === -1;
    });
  }

  function showTestResult(name) {
    Object.keys(testResults).forEach(function (key) {
      testResults[key].hidden = key !== name;
    });
  }

  function keyBytes(value) {
    var base64 = (value || "").replace(/-/g, "+").replace(/_/g, "/");
    while (base64.length % 4) base64 += "=";
    var raw = window.atob(base64);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  function isMobile() {
    var data = navigator.userAgentData;
    if (data && typeof data.mobile === "boolean") return data.mobile;
    return /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
  }

  var isIos = /iPhone|iPad|iPod/.test(navigator.userAgent);
  var standalone = window.matchMedia("(display-mode: standalone)").matches ||
    navigator.standalone === true;
  var supported = "serviceWorker" in navigator && "PushManager" in window &&
    "Notification" in window;

  /* Браузер без ключа в подписке (старый) — считаем ключ тем же: иначе он
   * переподписывался бы на каждой странице. */
  function sameKey(subscription) {
    var current = subscription.options && subscription.options.applicationServerKey;
    if (!current) return true;
    var bytes = new Uint8Array(current);
    if (bytes.length !== serverKey.length) return false;
    for (var i = 0; i < bytes.length; i += 1) {
      if (bytes[i] !== serverKey[i]) return false;
    }
    return true;
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body)
    });
  }

  function registration() {
    return navigator.serviceWorker.register("/staff/sw.js", { scope: "/staff/" }).then(function () {
      return navigator.serviceWorker.ready;
    });
  }

  /* Подписка браузера на ключ сервера и запись её на сервер. Отказ сервера
   * подписке (браузер вне белого списка служб пуша) — "unsupported". */
  async function ensureSubscription() {
    var ready = await registration();
    var subscription = await ready.pushManager.getSubscription();
    if (subscription && !sameKey(subscription)) {
      await subscription.unsubscribe();
      subscription = null;
    }
    if (!subscription) {
      subscription = await ready.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: serverKey
      });
    }
    var json = subscription.toJSON();
    var response = await postJson(subscribeUrl, { endpoint: json.endpoint, keys: json.keys });
    if (response.status === 422) throw new Error("unsupported");
    if (!response.ok) throw new Error("failed");
    return subscription;
  }

  function failureState(error) {
    return error && error.message === "unsupported" ? ["unsupported"] : ["offer", "failed"];
  }

  /* ---------- Состояние при открытии страницы ---------- */

  if (!isMobile()) return;

  if (isIos && !standalone) {
    var dismissed = false;
    try {
      dismissed = window.localStorage.getItem(IOS_HINT_KEY) === "1";
    } catch (error) {
      /* Нет хранилища — подсказка показывается каждый раз. */
    }
    show(dismissed ? null : "ios-hint");
    states["ios-hint"].querySelector("[data-push-ios-ok]").addEventListener("click", function () {
      try {
        window.localStorage.setItem(IOS_HINT_KEY, "1");
      } catch (error) {
        /* Скрыть хотя бы до следующей страницы. */
      }
      show();
    });
    return;
  }
  if (!supported) {
    show(isIos ? "old-ios" : "unsupported");
    return;
  }

  if (Notification.permission === "denied") {
    show("denied");
  } else if (Notification.permission === "granted") {
    ensureSubscription().then(function () { show(); }, function (error) {
      show.apply(null, failureState(error));
    });
  } else {
    show("offer");
  }

  var currentEndpoint = null;

  states.offer.querySelector("[data-push-on]").addEventListener("click", function () {
    /* Разрешение — первым делом в обработчике нажатия: iOS спрашивает только так. */
    Notification.requestPermission().then(function (permission) {
      if (permission === "denied") {
        show("denied");
        return null;
      }
      if (permission !== "granted") return null;
      return ensureSubscription().then(function (subscription) {
        currentEndpoint = subscription.endpoint;
        showTestResult(null);
        show("ready");
      });
    }).catch(function (error) {
      show.apply(null, failureState(error));
    });
  });

  states.ready.querySelector("[data-push-test]").addEventListener("click", function () {
    showTestResult(null);
    postJson(testUrl, { endpoint: currentEndpoint }).then(function (response) {
      return response.ok ? response.json() : { outcome: "failed" };
    }).then(function (result) {
      showTestResult(result.outcome === "delivered" ? "sent" : "not-sent");
    }, function () {
      showTestResult("not-sent");
    });
  });

  /* ---------- «Выйти»: сначала отписать телефон ---------- */

  async function release() {
    var ready = await navigator.serviceWorker.getRegistration("/staff/");
    var subscription = ready ? await ready.pushManager.getSubscription() : null;
    if (!subscription) return;
    /* Оба шага сразу, а не по очереди: висящая сеть не должна оставить телефон
     * подписанным — браузер отпишется и без сервера, а строку на сервере
     * удалит первый же ответ 410 службы пуша. */
    await Promise.allSettled([
      postJson(deleteUrl, { endpoint: subscription.endpoint }),
      subscription.unsubscribe()
    ]);
  }

  document.querySelectorAll('form[action="/staff/logout"]').forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var submitted = false;
      function leave() {
        if (submitted) return;
        submitted = true;
        /* form.submit() не зовёт этот обработчик снова. */
        form.submit();
      }
      setTimeout(leave, RELEASE_TIMEOUT_MS);
      release().then(leave, leave);
    });
  });
})();
