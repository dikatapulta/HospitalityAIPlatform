/* Лист «Отчёт» (spec 0083 §2.4): кнопка открывает печать браузера — там же
 * «Сохранить как PDF». Канон — кнопка печати «Кодекса» (mentor_codex.js). */
(function () {
  "use strict";
  var button = document.getElementById("report-print");
  if (button) {
    button.addEventListener("click", function () { window.print(); });
  }
})();
