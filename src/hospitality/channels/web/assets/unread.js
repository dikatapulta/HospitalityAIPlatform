// Непрочитанное (#223). Гость вернулся в чат — по QR или на свёрнутую вкладку:
// над сообщениями отеля, пришедшими без него, черта «Новое»; пока вкладка скрыта —
// счётчик в её заголовке. Последнее увиденное сообщение помнит localStorage;
// хранилища нет (приватный режим) — черты при возврате нет, чат работает как был.
// Вклеивается в скрипт страницы чата (page.py) после `append` и пользуется его
// `base`, `log` и `lastId`.
const TITLE = document.title;
const seenKey = "necturn:seen:" + base;
let unseen = 0;

function seenId() {
  try { return localStorage.getItem(seenKey); } catch (e) { return null; }
}

function markSeen() {
  unseen = 0;
  document.title = TITLE;
  try { if (lastId) localStorage.setItem(seenKey, lastId); } catch (e) { /* без хранилища */ }
}

function newMark() { return log.querySelector(".new-mark"); }

function markNew(before) {
  if (newMark()) return;
  const mark = document.createElement("div");
  mark.className = "new-mark";
  mark.textContent = "Жаңа · Новое · New";
  log.insertBefore(mark, before);
}

// Сообщение из опроса: вкладка скрыта — гость его ещё не видел.
function noteIncoming(div, direction) {
  if (!document.hidden || direction !== "outbound") return;
  markNew(div);
  unseen += 1;
  document.title = "(" + unseen + ") " + TITLE;
}

// Первый показ истории: всё, что отель написал после последнего увиденного.
// Увиденного нет в истории (другой браузер, первый вход) — черты нет.
function markHistory(messages, divs) {
  const from = messages.findIndex((m) => m.id === seenId());
  if (from < 0) return;
  const first = messages.findIndex((m, i) => i > from && m.direction === "outbound");
  if (first < 0) return;
  markNew(divs[first]);
  newMark().scrollIntoView({block: "center"});
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    // Следующая пачка непрочитанного получит свою черту, а не старую.
    const mark = newMark();
    if (mark) mark.remove();
    return;
  }
  const mark = newMark();
  if (unseen && mark) mark.scrollIntoView({block: "center"});
  markSeen();
});
