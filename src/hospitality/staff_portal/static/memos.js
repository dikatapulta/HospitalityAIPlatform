/* Служебные записки: черновик и кнопки карточки (spec 0077 §3, §6, §9; templates/memo_draft.html,
 * memo_card.html, memo_explanation.html, memo_demand.html, memo_demand_new.html,
 * memos_actions_api.py, memos_demands_api.py). Действие под именем —
 * window.NecturnMemoConfirm.run (static/memo_confirm.js): окно подтверждения ключом или паролем.
 *   Черновик: «Сохранить черновик» → POST drafts (новый) или {id}/draft; «Отправить…» —
 *     сохранить, затем подтвердить действие send → карточка.
 *   Карточка: «Подписать» / «Есть замечание» / решение руководителя — через окно
 *     подтверждения; «Отправить руководителю», «Отозвать», «на основе» — POST без него.
 *   Печать (memo_print.html): «Печать / сохранить в PDF» — window.print().
 *   Объяснительная (PR 5б): черновик, «Дополнить» и «Подать» — как черновик записки;
 *     требование — «Получил(а)» окном подтверждения, «Затребовать объяснение» — фото multipart.
 * Тексты — data-msg-*; всё — textContent, разметки в JS нет.
 */
(function () {
  "use strict";

  async function post(api, path, body) {
    var response = await fetch(api + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
    var data = null;
    try { data = await response.json(); } catch (error) { /* не конверт */ }
    if (!response.ok) {
      var envelope = (data && data.error) || {};
      var failure = new Error(envelope.code || String(response.status));
      failure.code = envelope.code;
      failure.reason = envelope.details && envelope.details[0] ? envelope.details[0].reason : null;
      throw failure;
    }
    return data || {};
  }

  function show(node, text) {
    node.textContent = text || "";
    node.hidden = !text;
  }

  function refusal(msg, error) {
    if (error.code === "ERR-MEMO-002") return msg.msgChanged;
    if (error.reason === "not_a_member" || error.reason === "not_senior_or_manager") {
      return msg.msgMembersChanged;
    }
    return msg.msgFailed;
  }

  /* ---------- Черновик ---------- */

  var draft = document.querySelector("[data-memo-draft]");
  if (draft) {
    var dmsg = draft.dataset;
    var dstatus = draft.querySelector("[data-draft-status]");
    var template = draft.querySelector("[data-template]");
    var body = draft.querySelector("#memo-body");
    var sendButton = draft.querySelector("[data-draft-send]");
    var signers = Array.prototype.slice.call(draft.querySelectorAll("[data-signer]"));
    var maxSigners = Number(dmsg.maxSigners);
    var lastTemplateBody = template.selectedOptions[0] ? template.selectedOptions[0].dataset.body : "";

    var addressee = draft.querySelector("#memo-addressee");
    /* Адресат подписантом быть не может (§3.1): его строка снимается и гаснет. */
    var syncSigners = function () {
      signers.forEach(function (box) { if (box.value === addressee.value) box.checked = false; });
      var picked = signers.filter(function (box) { return box.checked; }).length;
      signers.forEach(function (box) {
        box.disabled = box.value === addressee.value || (!box.checked && picked >= maxSigners);
      });
      sendButton.textContent = picked ? sendButton.dataset.labelSigners : sendButton.dataset.labelAddressee;
    };
    signers.forEach(function (box) { box.addEventListener("change", syncSigners); });
    addressee.addEventListener("change", syncSigners);
    syncSigners();

    template.addEventListener("change", function () {
      var next = template.selectedOptions[0].dataset.body || "";
      var untouched = !body.value.trim() || body.value === lastTemplateBody;
      if (untouched || window.confirm(dmsg.templateConfirm)) {
        body.value = next;
        lastTemplateBody = next;
      }
    });

    var collect = function () {
      return {
        template_key: template.value || null,
        title: draft.querySelector("#memo-title").value.trim(),
        body: body.value.trim(),
        addressee_id: addressee.value,
        signer_ids: signers.filter(function (box) { return box.checked; }).map(function (box) { return box.value; })
      };
    };

    /* Сохранить; вернуть id черновика или null (текст отказа уже показан). */
    var save = async function () {
      var data = collect();
      if (!data.title || !data.body) { show(dstatus, dmsg.msgFillTitleBody); return null; }
      if (!data.addressee_id) { show(dstatus, dmsg.msgPickAddressee); return null; }
      var id = draft.dataset.draftId;
      try {
        var saved = await post(dmsg.api, id ? "/" + id + "/draft" : "/drafts", data);
        if (!id) {
          draft.dataset.draftId = saved.id;
          // Перезагрузка страницы не создаст второй черновик: адрес — уже правка этого.
          window.history.replaceState(null, "", saved.href + "/edit");
        }
        return saved;
      } catch (error) {
        show(dstatus, refusal(dmsg, error));
        return null;
      }
    };

    var saveButton = draft.querySelector("[data-draft-save]");
    saveButton.addEventListener("click", async function () {
      saveButton.disabled = true;
      show(dstatus, "");
      var saved = await save();
      if (saved) show(dstatus, dmsg.msgDraftSaved);
      saveButton.disabled = false;
    });
    sendButton.addEventListener("click", async function () {
      sendButton.disabled = true;
      show(dstatus, "");
      var saved = await save();
      if (saved) {
        var status = await window.NecturnMemoConfirm.run(saved.id, { kind: "send" });
        if (status) { window.location.assign(saved.href); return; }
      }
      sendButton.disabled = false;
    });
  }

  /* ---------- Черновик объяснительной (memo_explanation.html, PR 5б) ----------
   * Первое сохранение: «Дополнить» → POST {id}/supplement, по требованию или адресату →
   * POST explanations; дальше — POST {id}/explanation. «Подать» — сохранить, затем send. */

  var explanation = document.querySelector("[data-explanation-draft]");
  if (explanation) {
    var emsg = explanation.dataset;
    var estatus = explanation.querySelector("[data-draft-status]");
    var field = function (name) { return explanation.querySelector("[name=" + name + "]"); };

    var collectExplanation = function () {
      var data = { title: field("title").value.trim(), body: field("body").value.trim() };
      var addresseeField = field("addressee_id");
      if (addresseeField) data.addressee_id = addresseeField.value;
      return data;
    };

    var saveExplanation = async function () {
      var data = collectExplanation();
      if (!data.title || !data.body) { show(estatus, emsg.msgFillTitleBody); return null; }
      if (data.addressee_id === "") { show(estatus, emsg.msgPickAddressee); return null; }
      var id = emsg.draftId;
      var path = "/" + id + "/explanation";
      if (!id && emsg.supplementOf) path = "/" + emsg.supplementOf + "/supplement";
      else if (!id) {
        path = "/explanations";
        if (emsg.demandId) data.demand_id = emsg.demandId;
      }
      try {
        var saved = await post(emsg.api, path, data);
        if (!id) {
          explanation.dataset.draftId = saved.id;
          // Перезагрузка не создаст вторую запись: адрес — уже правка этой.
          window.history.replaceState(null, "", saved.href + "/edit");
        }
        return saved;
      } catch (error) {
        show(estatus, refusal(emsg, error));
        return null;
      }
    };

    var esave = explanation.querySelector("[data-draft-save]");
    var esend = explanation.querySelector("[data-draft-send]");
    esave.addEventListener("click", async function () {
      esave.disabled = true;
      show(estatus, "");
      if (await saveExplanation()) show(estatus, emsg.msgDraftSaved);
      esave.disabled = false;
    });
    esend.addEventListener("click", async function () {
      esend.disabled = true;
      show(estatus, "");
      var saved = await saveExplanation();
      if (saved) {
        var status = await window.NecturnMemoConfirm.run(saved.id, { kind: "send" });
        if (status) { window.location.assign(saved.href); return; }
      }
      esend.disabled = false;
    });
  }

  /* ---------- Требование объяснения (memo_demand.html): «Получил(а)» ---------- */

  var demand = document.querySelector("[data-memo-demand]");
  var receive = demand && demand.querySelector("[data-demand-receive]");
  if (receive) {
    receive.addEventListener("click", async function () {
      receive.disabled = true;
      var path = "demands/" + demand.dataset.demandId;
      var status = await window.NecturnMemoConfirm.run(path, { kind: "receive" });
      if (status) { window.location.reload(); return; }
      receive.disabled = false;
    });
  }

  /* ---------- «Затребовать объяснение» (memo_demand_new.html): фото — multipart ---------- */

  var demandForm = document.querySelector("[data-demand-new]");
  if (demandForm) {
    var nmsg = demandForm.dataset;
    var nstatus = demandForm.querySelector("[data-demand-status]");
    var demandRefusal = function (code, reason) {
      if (code === "ERR-FILES-001") return nmsg.msgTooLarge;
      if (code === "ERR-FILES-002" || code === "ERR-FILES-003") return nmsg.msgWrongFile;
      if (code === "ERR-FILES-005" || code === "ERR-FILES-006") return nmsg.msgStorage;
      if (reason === "not_your_service") return nmsg.msgNotYourService;
      if (reason === "not_a_member") return nmsg.msgMembersChanged;
      return nmsg.msgFailed;
    };
    demandForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      var employee = demandForm.querySelector("[name=employee_id]").value;
      var photo = demandForm.querySelector("[name=photo]").files[0];
      if (!employee) { show(nstatus, nmsg.msgPickEmployee); return; }
      if (!photo) { show(nstatus, nmsg.msgPickPhoto); return; }
      var button = demandForm.querySelector("button[type=submit]");
      button.disabled = true;
      show(nstatus, "");
      var body = new FormData();
      body.append("employee_id", employee);
      body.append("photo", photo, photo.name);
      try {
        // Без Content-Type: границу multipart ставит сам браузер.
        var response = await fetch(nmsg.api, { method: "POST", body: body });
        var data = null;
        try { data = await response.json(); } catch (error) { /* не конверт */ }
        if (response.ok && data && data.href) { window.location.assign(data.href); return; }
        var envelope = (data && data.error) || {};
        var reason = envelope.details && envelope.details[0] ? envelope.details[0].reason : null;
        show(nstatus, demandRefusal(envelope.code, reason));
      } catch (error) {
        show(nstatus, nmsg.msgFailed);
      }
      button.disabled = false;
    });
  }

  /* ---------- Печать (memo_print.html): лист делает @media print в memos.css ---------- */

  document.querySelectorAll("[data-memo-print]").forEach(function (button) {
    button.addEventListener("click", function () { window.print(); });
  });

  /* ---------- Карточка ---------- */

  var card = document.querySelector("[data-memo-card]");
  if (!card) return;
  var cmsg = card.dataset;
  var memoId = cmsg.memoId;
  var cstatus = card.querySelector("[data-memo-status]");

  async function confirmed(intent) {
    show(cstatus, "");
    var status = await window.NecturnMemoConfirm.run(memoId, intent);
    if (status) window.location.reload();
  }

  card.querySelectorAll("[data-memo-intent]").forEach(function (button) {
    button.addEventListener("click", function () { confirmed({ kind: button.dataset.kind }); });
  });

  var remarkForm = card.querySelector("[data-memo-remark-form]");
  if (remarkForm) {
    remarkForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var remark = remarkForm.querySelector("textarea").value.trim();
      if (remark) confirmed({ kind: "remark", remark: remark });
    });
  }

  var decideForm = card.querySelector("[data-memo-decide-form]");
  if (decideForm) {
    decideForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var decision = event.submitter ? event.submitter.value : "approved";
      var comment = decideForm.querySelector("textarea").value.trim();
      if (decision !== "approved" && !comment) { show(cstatus, cmsg.msgCommentRequired); return; }
      confirmed({ kind: "decide", decision: decision, comment: comment });
    });
  }

  card.querySelectorAll("[data-memo-post]").forEach(function (button) {
    button.addEventListener("click", async function () {
      if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
      button.disabled = true;
      show(cstatus, "");
      try {
        var done = await post(cmsg.api, "/" + memoId + button.dataset.path);
        if (done.href) window.location.assign(done.href);
        else window.location.reload();
      } catch (error) {
        show(cstatus, refusal(cmsg, error));
        button.disabled = false;
      }
    });
  });
})();
