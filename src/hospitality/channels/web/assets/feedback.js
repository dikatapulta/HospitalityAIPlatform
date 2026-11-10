"use strict";
// Оценка выполненной заявки (spec 0076 §5): под пузырём итога — «Как всё прошло?» и два
// пальца, палец вниз открывает поле «что не так». Отдельный файл (`feedback/widget.py`):
// его грузит скрипт чата (`feedback` в chat.js), когда в ленте впервые есть что оценить, и
// он пользуется его `T`, `t`, `el`, `label`, `api`, `log`, `toGate`, `atEnd`, `scrollDown`.
// Надписи и стили едут здесь же, а не в странице: вес страницы чата ограничен (#746).
// «Передадим» и «передали» — только когда сервер сказал, что дошло (`passed_to_staff`).
Object.assign(T, __FB_TEXTS__);
document.head.appendChild(Object.assign(document.createElement("style"), {textContent: __FB_CSS__}));
const FB_THUMB = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 10h4v11H3zM7 10l4-7a2 2 0 0 1 3 2l-1 5h5.5a2 2 0 0 1 2 2.4l-1.4 7a2 2 0 0 1-2 1.6H7"/></svg>';
const FB_SEND = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"/></svg>';

function fbSay(state) {
  if (!state.rating) return "fb_ask";
  if (state.rating === "up" || !state.passed_to_staff) return "fb_thanks";
  return state.commented ? "fb_sent" : "fb_sorry";
}

// Состояние — с сервера (poll, история, ответ на касание); оценка окончательная (§3):
// после касания остаётся только выбранный палец.
function fbShow(box, state) {
  const end = atEnd();
  box.fb = state;
  for (const button of box.querySelectorAll(".fb-btn")) {
    const mine = button.dataset.rating === state.rating;
    button.hidden = Boolean(state.rating) && !mine;
    button.disabled = Boolean(state.rating);
    button.setAttribute("aria-pressed", String(mine));
  }
  const say = box.querySelector(".fb-say");
  say.dataset.t = fbSay(state); say.textContent = t(say.dataset.t);
  box.querySelector("form").hidden = state.rating !== "down" || state.commented;
  if (end) scrollDown();
}

function fbWidget(bubble, state) {
  const box = el("div", "fb glass");
  box.dataset.request = state.request_id;
  const row = el("div", "fb-row");
  row.appendChild(el("p", "fb-say"));
  for (const rating of ["up", "down"]) {
    const button = el("button", "fb-btn");
    button.type = "button"; button.dataset.rating = rating; button.innerHTML = FB_THUMB;
    label(button, "fb_" + rating);
    row.appendChild(button);
  }
  const form = el("form", "fb-form");
  // Две строки: подсказка видна целиком, а «что не так» — часто целая фраза. Предел —
  // `COMMENT_MAX_CHARS` домена; сервер держит его и сам (422).
  const field = el("textarea");
  field.rows = 2; field.maxLength = __FB_MAX__;
  label(field, "fb_hint");
  const go = el("button", "circle send");
  go.type = "submit"; go.innerHTML = FB_SEND;
  label(go, "send");
  form.append(field, go);
  box.append(row, form);
  // Виджет встаёт позже пузыря (файл грузится): кто был у конца ленты, остаётся у конца.
  const end = atEnd();
  bubble.after(box);
  fbShow(box, state);
  if (end) scrollDown();
}

// Касание и комментарий: сбой — прежняя строка чата «не доставлено» внутри виджета, всё
// можно повторить. Заявку больше нельзя оценить (404) или её оценили в другой вкладке
// (409) — виджет уходит: его состояние устарело, правду покажет следующая загрузка.
async function fbPost(box, path, body) {
  const controls = [...box.querySelectorAll("button, textarea")];
  for (const control of controls) control.disabled = true;
  const r = await api(path, {method: "POST", body: JSON.stringify(Object.assign({request_id: box.dataset.request}, body))});
  for (const control of controls) control.disabled = false;
  const fail = box.querySelector(".fail");
  if (fail) fail.remove();
  if (r.status === 401) { toGate(r.data && r.data.error && r.data.error.message); return; }
  if (r.status === 404 || r.status === 409) { box.remove(); return; }
  if (!r.ok || !r.data) {
    fbShow(box, box.fb);
    box.appendChild(el("p", "fail", t("not_delivered")));
    if (atEnd()) scrollDown();
    return;
  }
  fbShow(box, {request_id: box.dataset.request, rating: r.data.rating, commented: r.data.commented, passed_to_staff: r.data.passed_to_staff});
}

log.addEventListener("click", (e) => {
  const button = e.target.closest(".fb-btn");
  if (button && !button.disabled) fbPost(button.closest(".fb"), "/feedback", {rating: button.dataset.rating});
});
log.addEventListener("submit", (e) => {
  const form = e.target.closest(".fb-form");
  if (!form) return;
  e.preventDefault();
  const comment = form.querySelector("textarea").value.trim();
  if (comment) fbPost(form.closest(".fb"), "/feedback/comment", {comment});
});
