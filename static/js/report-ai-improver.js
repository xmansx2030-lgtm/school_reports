(function () {
  "use strict";

  function csrfToken() {
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return input ? input.value : "";
  }

  function dispatchTextChange(target) {
    target.dispatchEvent(new Event("input", { bubbles: true }));
    target.dispatchEvent(new Event("change", { bubbles: true }));
  }

  Array.prototype.forEach.call(
    document.querySelectorAll("[data-report-ai-improver]"),
    function (root) {
      var target = document.getElementById(root.getAttribute("data-target-id") || "id_idea");
      var endpoint = root.getAttribute("data-endpoint") || "";
      var documentName = root.getAttribute("data-document-name") || "التقرير";
      var trigger = root.querySelector("[data-report-ai-trigger]");
      var undo = root.querySelector("[data-report-ai-undo]");
      var preview = root.querySelector("[data-report-ai-preview]");
      var output = root.querySelector("[data-report-ai-output]");
      var accept = root.querySelector("[data-report-ai-accept]");
      var cancel = root.querySelector("[data-report-ai-cancel]");
      var status = root.querySelector("[data-report-ai-status]");
      var remainingNode = root.querySelector("[data-report-ai-remaining]");
      var quotaDots = root.querySelectorAll("[data-quota-dot]");
      var dailyLimit = parseInt(root.getAttribute("data-daily-limit"), 10);
      if (!Number.isFinite(dailyLimit) || dailyLimit < 0) dailyLimit = 3;
      var recommendedLength = parseInt(root.getAttribute("data-recommended-length"), 10) || 0;
      var maxLength = parseInt(root.getAttribute("data-max-length"), 10) || 6000;
      var previewCount = root.querySelector("[data-report-ai-preview-count]");
      var remaining = parseInt(root.getAttribute("data-remaining"), 10);
      var suggestedText = "";
      var previousText = "";
      var isLoading = false;

      if (!Number.isFinite(remaining)) remaining = dailyLimit;

      if (!target || !endpoint || !trigger || !preview || !output) return;

      function setStatus(message, type) {
        if (!status) return;
        status.textContent = message || "";
        status.classList.toggle("is-error", type === "error");
        status.classList.toggle("is-success", type === "success");
      }

      function renderTrigger() {
        trigger.disabled = isLoading || remaining <= 0;
        if (isLoading) {
          trigger.innerHTML = '<i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i> جارٍ تحسين الصياغة…';
        } else if (remaining <= 0) {
          trigger.innerHTML = '<i class="fa-solid fa-calendar-check" aria-hidden="true"></i> اكتمل رصيد اليوم';
        } else {
          trigger.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles" aria-hidden="true"></i> تحسين الصياغة بالذكاء الاصطناعي';
        }
      }

      function updateQuota(value) {
        remaining = Math.max(0, Math.min(dailyLimit, parseInt(value, 10) || 0));
        root.setAttribute("data-remaining", String(remaining));
        root.classList.toggle("is-exhausted", remaining <= 0);
        if (remainingNode) remainingNode.textContent = String(remaining);
        Array.prototype.forEach.call(quotaDots, function (dot, index) {
          dot.classList.toggle("is-available", index < remaining);
        });
        renderTrigger();
      }

      function setLoading(loading) {
        isLoading = loading;
        renderTrigger();
      }

      function closePreview() {
        preview.hidden = true;
        suggestedText = "";
        output.textContent = "";
        if (previewCount) previewCount.textContent = "0";
      }

      trigger.addEventListener("click", function () {
        var text = String(target.value || "").trim();
        if (isLoading) return;
        if (text.length < 20) {
          setStatus("اكتب نص " + documentName + " أولًا بما لا يقل عن 20 حرفًا.", "error");
          target.focus();
          return;
        }
        if (Array.from(text).length > maxLength) {
          setStatus("اختصر النص إلى " + maxLength + " حرفًا أو أقل ثم حاول مرة أخرى.", "error");
          target.focus();
          return;
        }

        closePreview();
        setStatus("أراجع الصياغة مع الحفاظ على معلومات " + documentName + "…", "");
        setLoading(true);

        var controller = typeof window.AbortController === "function"
          ? new window.AbortController()
          : null;
        var timeout = window.setTimeout(function () {
          if (controller) controller.abort();
        }, 32000);

        fetch(endpoint, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": csrfToken()
          },
          signal: controller ? controller.signal : undefined,
          body: JSON.stringify({ text: text })
        })
          .then(function (response) {
            return response.json().catch(function () {
              return { ok: false, message: "تعذر قراءة رد خدمة التحسين." };
            }).then(function (data) {
              if (!response.ok || !data.ok) {
                var failure = new Error(data.message || "تعذر تحسين الصياغة الآن.");
                failure.remaining = data.remaining;
                throw failure;
              }
              return data;
            });
          })
          .then(function (data) {
            suggestedText = String(data.improved_text || "").trim();
            if (!suggestedText) throw new Error("لم تصل صياغة محسنة. حاول مرة أخرى.");
            var suggestionLength = Array.from(suggestedText).length;
            if (suggestionLength > maxLength) {
              throw new Error("الصياغة المقترحة أطول من الحد النهائي. أعد المحاولة للحصول على نص أكثر اختصارًا.");
            }
            output.textContent = suggestedText;
            if (previewCount) previewCount.textContent = String(suggestionLength);
            preview.hidden = false;
            updateQuota(data.remaining);
            if (recommendedLength && suggestionLength > recommendedLength) {
              setStatus("النص المقترح مسموح، لكنه أطول من النطاق المثالي للطباعة. راجعه أو أعد التحسين لاختصاره.", "");
            } else {
              setStatus("صياغة موجزة ومناسبة للطباعة. راجعها ثم اعتمدها إذا كانت مناسبة.", "success");
            }
            preview.scrollIntoView({ behavior: "smooth", block: "nearest" });
          })
          .catch(function (error) {
            if (typeof error.remaining !== "undefined") updateQuota(error.remaining);
            var message = error && error.name === "AbortError"
              ? "استغرق التحسين وقتًا أطول من المتوقع. حاول مرة أخرى."
              : (error.message || "تعذر تحسين الصياغة الآن.");
            setStatus(message, "error");
          })
          .then(function () {
            window.clearTimeout(timeout);
            setLoading(false);
          });
      });

      updateQuota(remaining);

      if (accept) {
        accept.addEventListener("click", function () {
          if (!suggestedText) return;
          previousText = target.value;
          target.value = suggestedText;
          dispatchTextChange(target);
          closePreview();
          if (undo) undo.hidden = false;
          setStatus("تم اعتماد الصياغة المحسنة. راجعها قبل حفظ " + documentName + ".", "success");
          target.focus();
        });
      }

      if (cancel) {
        cancel.addEventListener("click", function () {
          closePreview();
          setStatus("تم الإبقاء على النص الأصلي.", "");
        });
      }

      if (undo) {
        undo.addEventListener("click", function () {
          target.value = previousText;
          dispatchTextChange(target);
          undo.hidden = true;
          setStatus("تمت استعادة النص السابق.", "success");
          target.focus();
        });
      }
    }
  );
}());
