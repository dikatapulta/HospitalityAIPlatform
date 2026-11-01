// Демо «Забытые вещи» (issue #604): поиск, форма «Найдена вещь» и выдача — без
// единого запроса к серверу. Карточки рисует сервер; здесь только атрибут hidden
// по фильтрам, data-status карточки (какую кнопку показать — решает CSS) и
// новая карточка клоном <template>, поля которой заполняются textContent —
// введённое сотрудником никогда не становится разметкой. Фото — blob-адрес в
// этом браузере: на сервер не уходит, перезагрузка страницы его забывает.
//
// «Сегодня» и граница «за 7 дней» — день отеля из data-today (сервер считает
// его по часовому поясу отеля), а не часы компьютера.
"use strict";

(() => {
  const root = document.querySelector("[data-lf]");
  if (!root) return;
  const list = root.querySelector("[data-lf-list]");
  const today = root.dataset.today;
  const safe = root.dataset.safe;
  const count = root.querySelector("[data-lf-count]");
  const empty = root.querySelector("[data-lf-empty]");
  const emptyDates = root.querySelector("[data-lf-empty-dates]");
  const emptyAny = root.querySelector("[data-lf-empty-any]");
  const qRoom = root.querySelector("[data-lf-q-room]");
  const qDate = root.querySelector("[data-lf-q-date]");
  const qText = root.querySelector("[data-lf-q-text]");
  const ranges = Array.from(root.querySelectorAll("[data-lf-range]"));
  let range = "all"; // all | week | day

  const MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
  ];
  const parts = (iso) => iso.split("-").map(Number);
  const dayLabel = (iso) => {
    const [, month, day] = parts(iso);
    return `${day} ${MONTHS[month - 1]}`;
  };
  const shortLabel = (iso) => {
    const [, month, day] = parts(iso);
    return `${String(day).padStart(2, "0")}.${String(month).padStart(2, "0")}`;
  };
  const shiftDays = (iso, days) => {
    const [year, month, day] = parts(iso);
    return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
  };
  const weekStart = shiftDays(today, -6);
  const norm = (text) => text.trim().toLowerCase().replaceAll("ё", "е");

  // ---- Поиск ----
  function setRange(next) {
    range = next;
    for (const button of ranges) {
      const on = button.dataset.lfRange === next;
      button.classList.toggle("chip-active", on);
      button.setAttribute("aria-pressed", String(on));
    }
    if (next !== "day") qDate.value = "";
  }

  function matches(card) {
    const room = norm(qRoom.value);
    const text = norm(qText.value);
    const checkout = card.dataset.checkout;
    if (room && !card.dataset.room.toLowerCase().includes(room)) return false;
    if (text && !text.split(/\s+/).every((word) => card.dataset.text.includes(word))) return false;
    if (range === "week" && (checkout < weekStart || checkout > today)) return false;
    if (range === "day" && checkout !== qDate.value) return false;
    return true;
  }

  function applyFilters() {
    let shown = 0;
    for (const card of list.querySelectorAll("[data-lf-card]")) {
      card.hidden = !matches(card);
      if (!card.hidden) shown += 1;
    }
    count.textContent = `Найдено: ${shown}`;
    empty.hidden = shown > 0;
    emptyDates.hidden = range === "all";
    emptyAny.hidden = range !== "all";
  }

  for (const button of ranges) {
    button.addEventListener("click", () => {
      setRange(button.dataset.lfRange);
      applyFilters();
    });
  }
  qDate.addEventListener("change", () => {
    setRange(qDate.value ? "day" : "all");
    applyFilters();
  });
  qRoom.addEventListener("input", applyFilters);
  qText.addEventListener("input", applyFilters);
  root.querySelector("[data-lf-reset]").addEventListener("click", () => {
    qRoom.value = "";
    qText.value = "";
    setRange("all");
    applyFilters();
  });

  // ---- Выдача: «Гость нашёлся» → «Выдано гостю» → «Кто выдал» ----
  list.addEventListener("click", (event) => {
    const card = event.target.closest("[data-lf-card]");
    if (!card) return;
    const form = card.querySelector("[data-lf-return-form]");
    const input = card.querySelector("[data-lf-returned-input]");
    const status = card.querySelector("[data-lf-status]");
    if (event.target.closest("[data-lf-claim]")) {
      card.dataset.status = "claimed";
      status.textContent = "Гость нашёлся — ждёт выдачи";
    } else if (event.target.closest("[data-lf-return]")) {
      card.dataset.status = "returning";
      form.hidden = false;
      input.focus();
    } else if (event.target.closest("[data-lf-return-cancel]")) {
      card.dataset.status = "claimed";
      form.hidden = true;
    } else if (event.target.closest("[data-lf-return-confirm]")) {
      const who = input.value.trim();
      if (!who) {
        input.focus();
        return;
      }
      card.dataset.status = "returned";
      form.hidden = true;
      status.textContent = `Выдано гостю ${shortLabel(today)}`;
      card.querySelector("[data-lf-returned-by]").textContent = who;
      card.querySelector("[data-lf-returned-row]").hidden = false;
    }
  });

  // ---- Форма «Найдена вещь» ----
  const form = root.querySelector("[data-lf-form]");
  const openButton = root.querySelector("[data-lf-new]");
  const docToggle = form.querySelector("[data-lf-doc]");
  const docRule = form.querySelector("[data-lf-doc-rule]");
  const photoBox = form.querySelector("[data-lf-photo]");
  const file = form.querySelector("[data-lf-file]");
  const preview = form.querySelector("[data-lf-photo-preview]");
  const previewImg = form.querySelector("[data-lf-photo-img]");
  const photoLabel = form.querySelector("[data-lf-photo-label]");
  const fRoom = form.querySelector("[data-lf-room]");
  const fDate = form.querySelector("[data-lf-date]");
  const fTitle = form.querySelector("[data-lf-title]");
  const fStorage = form.querySelector("[data-lf-storage]");
  const formError = form.querySelector("[data-lf-form-error]");
  const template = root.querySelector("[data-lf-card-template]");
  let photoUrl = null;

  function clearPhoto() {
    if (photoUrl) URL.revokeObjectURL(photoUrl);
    photoUrl = null;
    file.value = "";
    previewImg.removeAttribute("src");
    preview.hidden = true;
    photoLabel.textContent = "Сфотографировать или выбрать";
  }

  function setDocument(on) {
    docToggle.checked = on;
    docRule.hidden = !on;
    photoBox.hidden = on;
    if (on) {
      clearPhoto();
      fStorage.value = safe;
    }
    fStorage.disabled = on;
  }

  function resetForm() {
    setDocument(false);
    clearPhoto();
    fRoom.value = "";
    fDate.value = today;
    fTitle.value = "";
    fStorage.selectedIndex = 0;
    formError.hidden = true;
  }

  function showForm(open) {
    form.hidden = !open;
    openButton.setAttribute("aria-expanded", String(open));
    if (open) (fRoom.value ? fTitle : fRoom).focus();
  }

  openButton.addEventListener("click", () => showForm(form.hidden));
  form.querySelector("[data-lf-cancel]").addEventListener("click", () => {
    resetForm();
    showForm(false);
  });
  docToggle.addEventListener("change", () => setDocument(docToggle.checked));
  file.addEventListener("change", () => {
    const chosen = file.files && file.files[0];
    if (!chosen) return;
    if (photoUrl) URL.revokeObjectURL(photoUrl);
    photoUrl = URL.createObjectURL(chosen);
    previewImg.src = photoUrl;
    preview.hidden = false;
    photoLabel.textContent = "Другое фото";
  });

  // Кто нашёл — по этажу номера: 1504 → «горничная 15-го этажа».
  function finderFor(room) {
    const digits = /^\d{3,4}$/.test(room) ? Number(room) : NaN;
    return Number.isNaN(digits) ? "хаускипинг" : `горничная ${Math.floor(digits / 100)}-го этажа`;
  }

  function artFor(isDocument) {
    if (isDocument) return root.querySelector("[data-lf-art-lock]").content.firstElementChild.cloneNode(true);
    if (!photoUrl) return root.querySelector("[data-lf-art-item]").content.firstElementChild.cloneNode(true);
    const box = document.createElement("div");
    box.className = "lf-art lf-art-photo";
    const img = document.createElement("img");
    img.src = photoUrl;
    img.alt = "";
    box.append(img);
    photoUrl = null; // адрес теперь у карточки — resetForm его не отзовёт
    return box;
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const room = fRoom.value.trim();
    const title = fTitle.value.trim();
    if (!room || !title) {
      formError.hidden = false;
      (room ? fTitle : fRoom).focus();
      return;
    }
    const isDocument = docToggle.checked;
    const checkout = fDate.value || today;
    const card = template.content.firstElementChild.cloneNode(true);
    card.dataset.room = room;
    card.dataset.checkout = checkout;
    card.dataset.text = norm(title);
    card.querySelector("[data-lf-new-art]").replaceWith(artFor(isDocument));
    card.querySelector("[data-lf-new-title]").textContent = title;
    card.querySelector("[data-lf-new-room]").textContent = room;
    card.querySelector("[data-lf-new-date]").textContent = dayLabel(checkout);
    card.querySelector("[data-lf-new-finder]").textContent = finderFor(room);
    card.querySelector("[data-lf-new-storage]").textContent =
      fStorage.options[fStorage.selectedIndex].text.toLowerCase();
    list.prepend(card);
    resetForm();
    showForm(false);
    // Новая находка обязана быть видна: фильтры сброшены, карточка подсвечена.
    qRoom.value = "";
    qText.value = "";
    setRange("all");
    applyFilters();
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => card.classList.remove("lf-card-new"), 1600);
  });

  if (root.hasAttribute("data-lf-open-form")) showForm(true);
})();
