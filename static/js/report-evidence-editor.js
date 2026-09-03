/* محرر الشواهد المصورة — اسم ملف مستقل لكسر أي نسخة مخبأة من المحرر القديم. */
(function () {
  "use strict";

  var MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
  var WARNING_BYTES = 5 * 1024 * 1024;

  function gcd(a, b) {
    while (b) { var next = a % b; a = b; b = next; }
    return a || 1;
  }

  function allCards(editor) {
    return Array.prototype.slice.call(editor.querySelectorAll("[data-evidence-card]"));
  }

  function liveCards(editor) {
    return allCards(editor).filter(function (card) {
      var deletion = card.querySelector('input[name$="-DELETE"]');
      return !(deletion && deletion.checked);
    });
  }

  function updateOrder(editor) {
    liveCards(editor).forEach(function (card, index) {
      var order = card.querySelector('input[name$="-order"]');
      var number = card.querySelector("[data-evidence-number]");
      if (order) order.value = index + 1;
      if (number) number.textContent = String(index + 1).padStart(2, "0");
    });
  }

  function maxForms(editor) {
    var value = parseInt(editor.getAttribute("data-max-forms"), 10);
    return Number.isFinite(value) && value > 0 ? value : 8;
  }

  /* الترقيم وحالة زر الإضافة والعدّاد — ثلاثتها تتبع عدد البطاقات الحيّة،
     فتُحدَّث معاً بعد كل إضافة أو إزالة أو اختيار ملف. */
  function refresh(editor) {
    updateOrder(editor);
    var live = liveCards(editor).length;
    var limit = maxForms(editor);
    var addButton = editor.querySelector("[data-evidence-add]");
    var counter = editor.querySelector("[data-evidence-count]");

    if (addButton) {
      addButton.disabled = live >= limit;
      addButton.hidden = live >= limit;
    }
    if (counter) {
      if (live >= limit) counter.textContent = "بلغت الحد الأقصى: " + limit + " شواهد.";
      else if (live === 0) counter.textContent = "لا شواهد بعد.";
      else counter.textContent = live + " من " + limit;
    }
    // اللوحة تقرأ عدد الشواهد من الصفحة، فتُنبَّه ليبقى الفحص على الحقيقة.
    editor.dispatchEvent(new CustomEvent("evidence:changed", { bubbles: true }));
  }

  /* بطاقةٌ جديدة من قالبٍ يحمل ``__prefix__``. الفهرس يؤخذ من
     ``TOTAL_FORMS`` لا من عدد العقد: البطاقة المحذوفة تبقى في الصفحة، فعدّها
     يُنتج فهرساً مكرّراً يدوس على نموذجٍ قائم. */
  function addCard(editor) {
    var template = editor.querySelector("[data-evidence-template]");
    var list = editor.querySelector("[data-evidence-list]");
    var prefix = editor.getAttribute("data-prefix") || "evidence";
    var total = document.getElementById("id_" + prefix + "-TOTAL_FORMS");
    if (!template || !list || !total) return;

    var index = parseInt(total.value, 10);
    if (!Number.isFinite(index)) index = allCards(editor).length;
    if (liveCards(editor).length >= maxForms(editor)) return;

    var markup = template.innerHTML.replace(/__prefix__/g, String(index));
    var holder = document.createElement("div");
    holder.innerHTML = markup;
    var card = holder.querySelector("[data-evidence-card]");
    if (!card) return;

    var addTile = list.querySelector("[data-evidence-add]");
    if (addTile) list.insertBefore(card, addTile);
    else list.appendChild(card);
    total.value = String(index + 1);
    bindCard(editor, card);
    refresh(editor);

    var firstControl = card.querySelector("[data-image-source]");
    if (firstControl) firstControl.focus();
    return card;
  }

  function bindCard(editor, card) {
    var input = card.querySelector('input[type="file"]');
    var preview = card.querySelector("[data-preview-image]");
    var placeholder = card.querySelector("[data-preview-placeholder]");
    var ratio = card.querySelector("[data-image-ratio]");
    var note = card.querySelector("[data-file-note]");
    var fit = card.querySelector('select[name$="-fit_mode"]');
    var deletion = card.querySelector('input[name$="-DELETE"]');
    var remove = card.querySelector("[data-evidence-remove]");

    card.querySelectorAll("[data-image-source]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (!input) return;
        if (button.getAttribute("data-image-source") === "camera") {
          input.setAttribute("capture", "environment");
          input.click();
        } else {
          input.removeAttribute("capture");
          // الـ label يفتح المنتقي الأصلي عبر for؛ لا نكرر click برمجياً.
        }
      });
    });

    function syncFit() {
      if (preview && fit) preview.style.objectFit = fit.value === "cover" ? "cover" : "contain";
    }
    if (fit) fit.addEventListener("change", syncFit);
    syncFit();

    /* البطاقةُ لا تُنتزع من الصفحة أبداً — تُعلَّم محذوفةً وتُخفى.
       فهرسةُ نماذج Django متّصلة (‎0..N-1‎)، ونزعُ عقدةٍ من الوسط يفرض إعادة
       ترقيم كل ما بعدها؛ وخطأٌ واحد في ذلك يرسل صورةً إلى حقل صورةٍ أخرى.
       والخادم يتجاهل النموذج المعلَّم بالحذف أصلاً. */
    if (deletion) {
      var syncDeleted = function () {
        var isDeleted = deletion.checked;
        card.classList.toggle("is-deleted", isDeleted);
        card.hidden = isDeleted;
        refresh(editor);
      };
      deletion.addEventListener("change", syncDeleted);
      if (remove) {
        remove.addEventListener("click", function () {
          deletion.checked = true;
          syncDeleted();
        });
      }
      syncDeleted();
    }

    if (input) input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      input.removeAttribute("capture");
      if (!file) return;
      if (note) note.className = "ree-file-note";
      var extension = (file.name.split(".").pop() || "").toLowerCase();
      if (["jpg", "jpeg", "png", "webp"].indexOf(extension) === -1) {
        if (note) {
          note.textContent = "صيغة الصورة غير مدعومة. اختر JPG أو PNG أو WebP.";
          note.classList.add("is-error");
        }
        input.value = "";
        return;
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        if (note) {
          note.textContent = "حجم الملف أكبر من 10MB ولن يقبله النظام.";
          note.classList.add("is-error");
        }
      } else if (file.size > WARNING_BYTES) {
        if (note) {
          note.textContent = "تم اختيار " + file.name + " — الصورة كبيرة وسيحسنها النظام قبل الحفظ.";
          note.classList.add("is-warning");
        }
      } else {
        if (note) note.textContent = "تم اختيار " + file.name + " · " + (file.size / 1024 / 1024).toFixed(1) + "MB";
      }

      if (!preview) return;
      preview.onload = function () {
        var width = preview.naturalWidth;
        var height = preview.naturalHeight;
        var divisor = gcd(width, height);
        if (ratio) ratio.textContent = width + " × " + height + " · " + (width / divisor) + ":" + (height / divisor);
      };
      preview.onerror = function () {
        if (note) {
          note.textContent = "تعذرت معاينة الصورة. اختر ملف JPG أو PNG أو WebP صالحًا.";
          note.classList.add("is-error");
        }
      };
      var reader = new FileReader();
      reader.onload = function () {
        preview.src = reader.result;
        preview.hidden = false;
        if (placeholder) placeholder.hidden = true;
        if (deletion) { deletion.checked = false; card.classList.remove("is-deleted"); }
        syncFit();
      };
      reader.onerror = preview.onerror;
      reader.readAsDataURL(file);
      refresh(editor);
    });

    card.querySelectorAll("[data-move]").forEach(function (button) {
      button.addEventListener("click", function () {
        var direction = button.getAttribute("data-move");
        var sibling = direction === "up" ? card.previousElementSibling : card.nextElementSibling;
        if (!sibling) return;
        if (direction === "up") card.parentNode.insertBefore(card, sibling);
        else card.parentNode.insertBefore(sibling, card);
        updateOrder(editor);
      });
    });
  }

  document.querySelectorAll("[data-report-evidence-editor]").forEach(function (editor) {
    allCards(editor).forEach(function (card) { bindCard(editor, card); });
    var addButton = editor.querySelector("[data-evidence-add]");
    if (addButton) {
      addButton.addEventListener("click", function () { addCard(editor); });
    }
    refresh(editor);
  });
})();
