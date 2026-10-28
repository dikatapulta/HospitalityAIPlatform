/* Service worker кабинета (spec 0050 §3): показывает веб-пуш и по нажатию
 * открывает кабинет — браузер закрыт, экран заблокирован.
 *
 * Отдаётся маршрутом `/staff/sw.js` (router.py): область `/staff/` — весь
 * кабинет. Обработчика `fetch` нет намеренно: кабинет не кэшируется офлайн и
 * работает как без service worker, страницы ходят в сеть как раньше.
 *
 * Сообщение — JSON `{title, body, url, tag}` от `platform/staff_push.py`, без
 * текста гостя: уведомление видно на заблокированном экране.
 */
"use strict";

var FALLBACK = {
  title: "Кабинет персонала",
  body: "",
  url: "/staff/",
  tag: "staff-push"
};

self.addEventListener("install", function () {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

/* Только свои адреса кабинета: нажатие не уводит на чужой сайт. */
function safeUrl(value) {
  try {
    var url = new URL(value, self.location.origin);
    if (url.origin === self.location.origin && url.pathname.indexOf("/staff/") === 0) {
      return url.href;
    }
  } catch (error) {
    /* Негодный адрес — главная кабинета. */
  }
  return new URL(FALLBACK.url, self.location.origin).href;
}

self.addEventListener("push", function (event) {
  var message = FALLBACK;
  try {
    var data = event.data ? event.data.json() : null;
    if (data && typeof data.title === "string") message = data;
  } catch (error) {
    /* Непонятное сообщение — всё равно показать: iOS отзывает подписку у
     * пуша, после которого уведомления не было. */
  }
  event.waitUntil(
    self.registration.showNotification(message.title, {
      body: typeof message.body === "string" ? message.body : "",
      tag: typeof message.tag === "string" ? message.tag : FALLBACK.tag,
      /* Та же метка заменяет прежнее уведомление — и всё равно звучит:
       * вторая «Проверка» иначе пришла бы молча. */
      renotify: true,
      icon: "/staff/static/icon-192.png",
      badge: "/staff/static/badge-96.png",
      data: { url: safeUrl(message.url) }
    })
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = safeUrl(event.notification.data && event.notification.data.url);
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (windows) {
      for (var i = 0; i < windows.length; i += 1) {
        if (windows[i].url === url && "focus" in windows[i]) return windows[i].focus();
      }
      return self.clients.openWindow(url);
    })
  );
});
