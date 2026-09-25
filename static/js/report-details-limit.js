(function () {
  "use strict";

  Array.prototype.forEach.call(
    document.querySelectorAll("[data-report-details-meter]"),
    function (meter) {
      var target = document.getElementById(meter.getAttribute("data-target-id") || "id_idea");
      var recommended = parseInt(meter.getAttribute("data-recommended-length"), 10) || 450;
      var maximum = parseInt(meter.getAttribute("data-max-length"), 10) || 600;
      var count = meter.querySelector("[data-report-details-count]");
      var countValue = count ? count.querySelector("b") : null;
      var fill = meter.querySelector("[data-report-details-fill]");
      var marker = meter.querySelector("[data-report-details-marker]");
      var status = meter.querySelector("[data-report-details-status]");

      if (!target || !countValue || !fill || !status) return;

      var originalText = String(target.value || "");
      var preserveLegacy = meter.getAttribute("data-preserve-legacy") === "1" &&
        Array.from(originalText).length > maximum;
      if (!preserveLegacy) target.setAttribute("maxlength", String(maximum));
      if (marker) marker.style.insetInlineStart = ((recommended / maximum) * 100) + "%";

      function render() {
        var length = Array.from(String(target.value || "")).length;
        var ratio = Math.min(1, length / maximum);
        var state = "is-comfortable";
        var message = "لديك مساحة مريحة لكتابة ما تم تنفيذه وأبرز نتائجه.";

        if (length >= maximum) {
          state = "is-max";
          message = "وصلت إلى الحد النهائي. اختصر الفكرة قبل إضافة تفاصيل أخرى.";
        } else if (length > recommended) {
          state = "is-over-ideal";
          message = "النص مسموح، لكنه تجاوز الطول المثالي وقد يظهر بتنسيق أكثر كثافة في الطباعة.";
        } else if (length >= Math.round(recommended * 0.8)) {
          state = "is-near-ideal";
          message = "اقتربت من الطول المثالي؛ ركّز على التنفيذ والنتيجة وتجنب التكرار.";
        }

        meter.classList.remove("is-comfortable", "is-near-ideal", "is-over-ideal", "is-max");
        meter.classList.add(state);
        countValue.textContent = String(length);
        fill.style.width = (ratio * 100) + "%";
        status.textContent = message;

        if (preserveLegacy && target.value === originalText) {
          status.textContent = "النص القديم محفوظ كما هو؛ إذا عدّلته فاختصره إلى " + maximum + " حرفًا أو أقل.";
          target.setCustomValidity("");
        } else if (length > maximum) {
          target.setCustomValidity("تفاصيل التقرير لا تتجاوز " + maximum + " حرفًا.");
        } else {
          target.setCustomValidity("");
        }
      }

      target.addEventListener("input", render);
      target.addEventListener("change", render);
      render();
    }
  );
}());
