/* ملخّص التقرير الحيّ.
 *
 * يقرأ الحقول المفتوحة أمام المعلّم ولا ينادي الخادم. وكل نصٍّ يمرّ بـ
 * ``textContent`` — العنوان ووصف الشاهد من كتابة المستخدم، وحقنُهما بـ
 * ``innerHTML`` ثغرة.
 *
 * تسميات البنود تُقرأ من بلاطات الاختيار نفسها لا من قائمة مكرّرة هنا: تسميةٌ
 * مكتوبة مرّتين تفترق عند أول تعديل، فيقول الشريط «آلية التنفيذ» بينما تقول
 * البلاطة شيئاً آخر.
 */
(function () {
  "use strict";

  var SECTIONS = [
    { key: "goal", toggle: "id_show_goal", field: "id_goal" },
    { key: "details", toggle: "id_show_details", field: "id_idea" },
    { key: "implementation", toggle: "id_show_implementation", field: "id_implementation_method" },
    { key: "results", toggle: "id_show_results", field: "id_results" },
    { key: "recommendations", toggle: "id_show_recommendations", field: "id_recommendations" },
    { key: "beneficiaries", toggle: "id_show_beneficiaries", field: "id_beneficiaries_count" }
  ];

  var root = document.querySelector("[data-report-summary]");
  if (!root) return;

  var form = root.closest("form") || document.getElementById("report-form");
  var categoryOut = root.querySelector("[data-summary-category]");
  var dateOut = root.querySelector("[data-summary-date]");
  var sectionsOut = root.querySelector("[data-summary-sections]");
  var evidenceOut = root.querySelector("[data-summary-evidence]");

  function labelFor(key) {
    var option = document.querySelector('[data-section-option="' + key + '"]');
    if (!option) return key;
    var title = option.querySelector("strong, .ar-section-option-title, b");
    var text = (title ? title.textContent : option.textContent) || "";
    return text.trim().split("\n")[0].trim();
  }

  function hijriCaption() {
    // صدى التاريخ الهجري تكتبه ‎js/hijri-date.js‎ في هذه العقدة.
    var node = document.getElementById("reportDateHijri");
    return node ? node.textContent.trim() : "";
  }

  /* المثنّى صيغةٌ قائمة بذاتها، و«2 شواهد» ليست عربية. */
  function evidenceText(count) {
    if (count === 0) return "لا شواهد مرفقة.";
    if (count === 1) return "شاهد واحد مرفق.";
    if (count === 2) return "شاهدان مرفقان.";
    if (count <= 10) return count + " شواهد مرفقة.";
    return count + " شاهدًا مرفقًا.";
  }

  function evidenceCount() {
    var total = 0;
    Array.prototype.forEach.call(document.querySelectorAll("[data-evidence-card]"), function (card) {
      var remove = card.querySelector('[data-evidence-delete] input[type="checkbox"]');
      if (remove && remove.checked) return;
      var file = card.querySelector('input[type="file"]');
      if (file && file.files && file.files.length > 0) { total += 1; return; }
      var image = card.querySelector("[data-preview-image]");
      if (image && !image.hidden && image.getAttribute("src")) total += 1;
    });
    return total;
  }

  function render() {
    var category = document.getElementById("id_category");
    if (categoryOut) {
      var chosen = category && category.selectedIndex > -1 ? category.options[category.selectedIndex] : null;
      var label = chosen && chosen.value ? chosen.textContent.trim() : "";
      categoryOut.textContent = label || "—";
      categoryOut.classList.toggle("is-empty", !label);
    }

    if (dateOut) {
      var hijri = hijriCaption();
      var gregorian = document.getElementById("id_report_date");
      var value = hijri || (gregorian ? gregorian.value : "");
      dateOut.textContent = value || "—";
      dateOut.classList.toggle("is-empty", !value);
    }

    if (sectionsOut) {
      sectionsOut.textContent = "";
      var chosenCount = 0;
      SECTIONS.forEach(function (section) {
        var toggle = document.getElementById(section.toggle);
        if (!toggle || !toggle.checked) return;
        chosenCount += 1;
        var field = document.getElementById(section.field);
        var filled = !!(field && String(field.value || "").trim());

        var item = document.createElement("li");
        item.className = filled ? "is-filled" : "is-empty";
        var mark = document.createElement("i");
        mark.className = filled ? "fa-solid fa-circle-check" : "fa-regular fa-circle";
        mark.setAttribute("aria-hidden", "true");
        item.appendChild(mark);
        var text = document.createElement("span");
        text.textContent = labelFor(section.key);
        item.appendChild(text);
        if (!filled) {
          var pending = document.createElement("small");
          // مسافةٌ بادئة لقارئ الشاشة: العنصران متلاصقان في ``textContent``
          // وإن فصلهما التخطيط، فيُقرآن كلمةً واحدة.
          pending.textContent = " بانتظار الكتابة";
          item.appendChild(pending);
        }
        sectionsOut.appendChild(item);
      });

      if (chosenCount === 0) {
        var empty = document.createElement("li");
        empty.className = "is-none";
        empty.textContent = "لم تختر أي بند بعد.";
        sectionsOut.appendChild(empty);
      }
    }

    if (evidenceOut) evidenceOut.textContent = evidenceText(evidenceCount());
  }

  if (form) {
    form.addEventListener("input", render);
    form.addEventListener("change", render);
  }
  document.addEventListener("evidence:changed", render);
  render();
})();
