// Фото к заявке (spec 0081 §4): вклеивается в скрипт чата (маркер в chat.js) только там, где фото
// включены, — вес страницы ограничен (#746). Картинка и отправка — файлом photo.js с версией в
// адресе, по первой надобности, как оценка. Не загрузился — фото в ленте без картинки, снимок
// не уходит, гость видит «не доставлено».
const photoBtn = $("photo-btn"), photoFile = $("photo-file");
let photoLoad = null;
function photoScript() {
  photoLoad = photoLoad || new Promise((done, fail) => {
    const script = el("script");
    script.src = "__PHOTO_SRC__"; script.onload = done;
    script.onerror = () => { photoLoad = null; fail(); };
    document.head.appendChild(script);
  });
  return photoLoad;
}
function photo(bubble, url) { photoScript().then(() => photoFill(bubble, url), () => {}); }
photoBtn.addEventListener("click", () => photoFile.click());
photoFile.addEventListener("change", () => {
  const picked = photoFile.files[0];
  photoFile.value = "";
  if (picked && !photoBtn.disabled) photoScript().then(() => photoSend(picked, photoBtn), () => {
    const status = $("chat-pause"); status.textContent = t("not_delivered"); status.hidden = false;
  });
});
