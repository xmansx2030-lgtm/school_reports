(function () {
  "use strict";

  var aliases = {
    "الإدارة": "manager",
    "الادارة": "manager",
    "مدير": "manager",
    "مديرة": "manager",
    "المعلمين": "teachers",
    "المعلمات": "teachers",
    "معلمين": "teachers",
    "معلمات": "teachers",
    "النشاط": "activity",
    "التطوع": "volunteer",
    "الشؤون المدرسية": "affairs",
    "الشؤون الإدارية": "admin",
    "الشؤون الادارية": "admin"
  };

  function normalizeArabicDigits(value) {
    return (value || "").replace(/[٠-٩]/g, function (digit) {
      return String("٠١٢٣٤٥٦٧٨٩".indexOf(digit));
    });
  }

  function transliterateArabic(value) {
    var map = {
      "ا": "a", "أ": "a", "إ": "i", "آ": "a", "ء": "", "ؤ": "w", "ئ": "y",
      "ب": "b", "ت": "t", "ث": "th", "ج": "j", "ح": "h", "خ": "kh",
      "د": "d", "ذ": "dh", "ر": "r", "ز": "z", "س": "s", "ش": "sh",
      "ص": "s", "ض": "d", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh",
      "ف": "f", "ق": "q", "ك": "k", "ل": "l", "م": "m", "ن": "n",
      "ه": "h", "ة": "h", "و": "w", "ي": "y", "ى": "a", "ﻻ": "la", "لا": "la"
    };
    return (value || "").replace(/[ً-ٟ]/g, "").split("").map(function (character) {
      return map[character] !== undefined ? map[character] : character;
    }).join("");
  }

  function slugifyAscii(value) {
    var text = (value || "").trim();
    if (aliases[text]) return aliases[text];
    text = transliterateArabic(normalizeArabicDigits(text));
    try { text = text.normalize("NFKD"); } catch (error) { /* older browser fallback */ }
    return text
      .replace(/[ً-ٟ]/g, "")
      .replace(/[\s_]+/g, "-")
      .replace(/[^a-zA-Z0-9-]/g, "")
      .toLowerCase()
      .replace(/-+/g, "-")
      .replace(/^-+|-+$/g, "");
  }

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("departmentForm");
    if (!form) return;

    var name = document.getElementById("id_name");
    var slug = document.getElementById("id_slug");
    var button = document.getElementById("departmentSubmit");
    var buttonLabel = document.getElementById("departmentSubmitLabel");
    var saveStatus = document.getElementById("departmentSaveStatus");
    var isEdit = form.dataset.editMode === "true";

    function syncSlug() {
      if (!name || !slug) return;
      if (isEdit && (slug.value || "").trim()) return;
      var generated = slugifyAscii(name.value);
      if (generated) slug.value = generated;
    }

    if (name && slug) {
      name.addEventListener("input", syncSlug);
      name.addEventListener("blur", syncSlug);
      syncSlug();
    }

    document.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && String(event.key || "").toLowerCase() === "s") {
        event.preventDefault();
        if (form.requestSubmit) form.requestSubmit();
        else form.submit();
      }
    });

    if (button && buttonLabel) {
      var defaultLabel = buttonLabel.textContent;
      function restoreSubmitButton() {
        button.disabled = false;
        buttonLabel.textContent = defaultLabel;
        form.setAttribute("aria-busy", "false");
        if (saveStatus) saveStatus.textContent = "";
      }

      form.addEventListener("submit", function () {
        button.disabled = true;
        buttonLabel.textContent = "جارٍ الحفظ...";
        form.setAttribute("aria-busy", "true");
        if (saveStatus) saveStatus.textContent = "جارٍ حفظ بيانات القسم.";
      });

      window.addEventListener("pageshow", function (event) {
        if (event.persisted) restoreSubmitButton();
      });
    }
  });
}());
