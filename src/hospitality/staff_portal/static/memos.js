/* Служебные записки: черновик и кнопки карточки (spec 0077 §3, §9; templates/memo_draft.html,
 * memo_card.html, memos_actions_api.py). Действие под именем — window.NecturnMemoConfirm.run
 * (static/memo_confirm.js): окно подтверждения ключом или паролем.
 *   Черновик: «Сохранить черновик» → POST drafts (новый) или {id}/draft; «Отправить…» —
 *     сохранить, затем подтвердить действие send → карточка.
 *   Карточка: «Подписать» / «Есть замечание» / решение руководителя — через окно
 *     подтверждения; «Отправить руководителю», «Отозвать», «на основе» — POST без него.
 *   Печать (memo_print.html): «Печать / сохранить в PDF» — window.print().
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
