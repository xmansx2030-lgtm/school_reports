/* Tawtheeq approval presentation helpers. Backend authorization stays authoritative. */
(function () {
  "use strict";

  var form = document.querySelector("[data-approval-decision-form]");
  if (!form) return;

  var note = form.querySelector("#approvalNote");
  var error = form.querySelector("#approvalNoteError");
  var noteRequiredActions = new Set(["request_info", "return"]);

  function clearNoteError() {
    if (!note || !error) return;
    note.removeAttribute("aria-invalid");
    error.hidden = true;
    error.textContent = "";
  }

  if (note) note.addEventListener("input", clearNoteError);

  form.addEventListener("submit", function (event) {
    var submitter = event.submitter;
    var action = submitter ? submitter.value : "";
    if (!note || !noteRequiredActions.has(action) || note.value.trim()) {
      clearNoteError();
      return;
    }

    event.preventDefault();
    note.setAttribute("aria-invalid", "true");
    if (error) {
      error.textContent = "اكتب سبب الإعادة أو طلب الاستكمال قبل تنفيذ الإجراء.";
      error.hidden = false;
    }
    note.focus();
  });
})();
