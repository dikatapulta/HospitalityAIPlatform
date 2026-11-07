/* «3D-витрина» — действия менеджера (spec 0069 §2.2, §5 «Кабинет»):
 * «Загрузить сцену (JSON)» — текст файла одним POST по CSRF-контракту (Content-Type:
 * application/json + Origin от fetch); принято — страница перезагружается с новой
 * сценой, отказ — «Сцена не принята: {путь} — {причина}» из конверта ошибки.
 * «Открыть страницу посетителя ↗» — POST за cookie доступа, затем новая вкладка.
 * Только textContent.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-showroom-admin]");
  if (!root) return;
  var file = root.querySelector("[data-showroom-file]");
  var open = root.querySelector("[data-showroom-open]");
  var status = root.querySelector("[data-showroom-status]");
  var failed = root.querySelector("[data-showroom-error]");

  function show(element, text) {
    element.textContent = text;
    element.hidden = !text;
  }

  async function post(url, body) {
    var response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    var data = null;
    try { data = await response.json(); } catch (error) { /* 204 или не JSON */ }
    return { ok: response.ok, status: response.status, data: data };
  }

  file.addEventListener("change", async function () {
    var chosen = file.files && file.files[0];
    if (!chosen) return;
    show(status, "");
    show(failed, "");
    try {
      var result = await post(root.dataset.upload, { text: await chosen.text() });
      if (result.ok && result.data) {
        show(status, result.data.message);
        window.setTimeout(function () { window.location.reload(); }, 800);
      } else {
        var reason = (result.data && result.data.error && result.data.error.message) || String(result.status);
        show(failed, root.dataset.rejected.replace("{reason}", reason));
      }
    } catch (error) {
      show(failed, root.dataset.rejected.replace("{reason}", String(error)));
    }
    file.value = "";
  });

  if (open) {
    open.addEventListener("click", async function () {
      var tab = window.open("", "_blank");
      var result = await post(root.dataset.cookie);
      if (result.ok && tab) {
        tab.opener = null;
        tab.location.href = root.dataset.visitor;
      } else if (tab) {
        tab.close();
      }
    });
  }
})();
