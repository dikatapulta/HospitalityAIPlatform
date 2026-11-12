"use strict";
// Фото к заявке (spec 0081 §4): картинка в пузыре гостя и отправка с камеры. Отдельный файл
// (`photos/widget.py`): его грузит скрипт чата (`photoScript` в chat.js) по первой надобности —
// фото в ленте или выбранный снимок — и он пользуется его `T`, `t`, `el`, `append`, `show`,
// `shown`, `base`, `lang`, `empty`, `typing`, `toGate`, `atEnd`, `scrollDown`, `newMark`,
// `markSeen`, `poll`. Надписи и стили едут здесь же: вес страницы чата ограничен (#746).
// Отказы — статусом над строкой ввода, в переписку не пишутся (§8).
Object.assign(T, __PHOTO_TEXTS__);
document.head.appendChild(Object.assign(document.createElement("style"), {textContent: __PHOTO_CSS__}));
// Снимок больше 5 МБ страница уменьшает до 2400 px JPEG, а не получает отказ (канон documents.js).
const PHOTO_MAX = 5 * 1024 * 1024, PHOTO_SIDE = 2400;
// Гость прислал не то — «другой снимок»; сбой у нас (005, 006, 5xx, сеть) — «ещё раз».
const PHOTO_REJECTED = ["ERR-FILES-001", "ERR-FILES-002"];

// Картинка — в пузырь, который скрипт чата уже поставил в ленту.
function photoFill(bubble, url) {
  const end = atEnd();
  const img = el("img");
  img.alt = t("photo_alt");
  img.addEventListener("load", () => { if (end) scrollDown(); });
  img.src = url;
  bubble.classList.add("photo");
  bubble.insertBefore(img, bubble.firstChild);
}

function photoStatus(key) {
  const status = $("chat-pause");
  status.hidden = !key;
  if (key) status.textContent = t(key);
}

async function shrinkPhoto(file) {
  try {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, PHOTO_SIDE / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    for (const quality of [0.85, 0.7]) {
      const blob = await new Promise((done) => canvas.toBlob(done, "image/jpeg", quality));
      if (blob && blob.size <= PHOTO_MAX) return new File([blob], "photo.jpg", {type: "image/jpeg"});
    }
  } catch (e) { /* браузер не смог — отказ размером от сервера */ }
  return file;
}

// Снимок гостя — пузырём сразу, на сервер — формой; ответ отеля — обычной репликой.
async function photoSend(picked, button) {
  empty(false);
  const file = picked.size > PHOTO_MAX ? await shrinkPhoto(picked) : picked;
  const key = crypto.randomUUID();
  shown.add(key);
  const preview = URL.createObjectURL(file);
  const mine = append("inbound", "");
  photoFill(mine, preview);
  button.disabled = true; typing(true); photoStatus(null);
  const form = new FormData();
  form.append("client_key", key);
  form.append("language", lang);
  form.append("file", file, file.name || "photo.jpg");
  let response = null;
  let data = null;
  try {
    response = await fetch(base + "/photos", {method: "POST", body: form});
    try { data = await response.json(); } catch (e) { /* не-JSON — ниже */ }
  } catch (e) {
    /* сети нет — ниже */
  } finally {
    button.disabled = false; typing(false);
  }
  if (response && response.status === 401) { toGate(data && data.error && data.error.message); return; }
  if (!response || !response.ok || !data) {
    mine.remove(); URL.revokeObjectURL(preview);
    const code = data && data.error && data.error.code;
    const limit = response && response.status === 429;
    photoStatus(limit ? "photo_limit" : PHOTO_REJECTED.includes(code) ? "photo_rejected" : "photo_failed");
    scrollDown();
    return;
  }
  shown.add(data.photo.id);
  data.replies.forEach((text, i) => show({id: data.reply_ids[i], direction: "outbound", text}));
  scrollDown();
  const mark = newMark();
  if (mark) mark.remove();
  markSeen();
  // Повтор ключа (`duplicate`) ответа не несёт — его, как и всё новое, приносит опрос.
  poll();
}
