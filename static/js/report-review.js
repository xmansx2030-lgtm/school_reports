/* لوحة فحص جاهزية التقرير.
 *
 * قاعدتان تحكمان هذا الملف:
 *
 * ١. **لا يُبنى نصُّ ملاحظةٍ بـ innerHTML.** الرسائل تأتي من نموذج لغوي، ونصٌّ
 *    مولَّد يُحقن في DOM هو ثغرة مهما بدا بريئاً. كل نصٍّ هنا يمرّ بـ
 *    textContent، والأيقونات وحدها ترميز ثابت مكتوب في هذا الملف.
 *
 * ٢. **لا يُعاد تنفيذ منطق الفحص هنا.** الحكم كلّه في الخادم، والواجهة تعرض ما
 *    يصلها. نسخةٌ ثانية من قواعد الفحص في JS تفترق عن الأولى عند أول تعديل،
 *    فيرى المعلّم ملاحظةً في اللوحة لا يراها المراجع — أو العكس.
 */
(function () {
  "use strict";

  var RING_CIRCUMFERENCE = 163.363;
  var SEVERITY_LABELS = { high: "يوجب المراجعة", medium: "ضعف ظاهر", low: "تحسين اختياري" };
  var LEVEL_CLASSES = { ready: "is-ready", almost: "is-almost", needs_work: "is-needs-work" };

  var TEXT_FIELDS = [
    ["title", "id_title"],
    ["category", "id_category"],
    ["report_date", "id_report_date"],
    ["goal", "id_goal"],
    ["idea", "id_idea"],
    ["implementation_method", "id_implementation_method"],
    ["results", "id_results"],
    ["recommendations", "id_recommendations"],
    ["beneficiaries_count", "id_beneficiaries_count"]
  ];

  var TOGGLES = [
    ["show_goal", "id_show_goal"],
    ["show_details", "id_show_details"],
    ["show_implementation", "id_show_implementation"],
    ["show_results", "id_show_results"],
    ["show_recommendations", "id_show_recommendations"],
    ["show_beneficiaries", "id_show_beneficiaries"]
  ];

  function csrfToken() {
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return input ? input.value : "";
  }

  function value(id) {
    var node = document.getElementById(id);
    return node ? String(node.value || "").trim() : "";
  }

  function checked(id) {
    var node = document.getElementById(id);
    return !!(node && node.checked);
  }

  /* عدد الشواهد المرفقة فعلاً: بطاقةٌ مُعلَّمة للحذف ليست شاهداً، وبطاقةٌ فارغة
     في المصفوفة ليست شاهداً، والصورة المحفوظة مسبقاً شاهدٌ وإن لم يُختر لها ملف
     في هذه الجلسة. */
  function evidenceCount() {
    var cards = document.querySelectorAll("[data-evidence-card]");
    var total = 0;
    Array.prototype.forEach.call(cards, function (card) {
      var remove = card.querySelector('[data-evidence-delete] input[type="checkbox"]');
      if (remove && remove.checked) return;
      var file = card.querySelector('input[type="file"]');
      if (file && file.files && file.files.length > 0) { total += 1; return; }
      var image = card.querySelector("[data-preview-image]");
      if (image && !image.hidden && image.getAttribute("src")) total += 1;
    });
    return total;
  }

  function collectDraft() {
    var draft = { sections: {}, evidence_count: evidenceCount() };
    TEXT_FIELDS.forEach(function (pair) { draft[pair[0]] = value(pair[1]); });
    TOGGLES.forEach(function (pair) { draft.sections[pair[0]] = checked(pair[1]); });
    return draft;
  }

  /* بصمةٌ للمقارنة لا للأمان: تكفي لمعرفة أن المعلّم عدّل شيئاً بعد آخر فحص. */
  function fingerprint(draft) {
    var parts = TEXT_FIELDS.map(function (pair) { return draft[pair[0]]; });
    TOGGLES.forEach(function (pair) { parts.push(draft.sections[pair[0]] ? "1" : "0"); });
    parts.push(String(draft.evidence_count));
    return parts.join("␟");
  }

  function icon(name) {
    var node = document.createElement("i");
    node.className = name;
    node.setAttribute("aria-hidden", "true");
    return node;
  }

  Array.prototype.forEach.call(
    document.querySelectorAll("[data-report-review]"),
    function (root) {
      var endpoint = root.getAttribute("data-endpoint") || "";
      if (!endpoint) return;

      var form = root.closest("form") || document.getElementById("report-form");
      var trigger = root.querySelector("[data-rrv-trigger]");
      var triggerLabel = root.querySelector("[data-rrv-trigger-label]");
      var intro = root.querySelector("[data-rrv-intro]");
      var result = root.querySelector("[data-rrv-result]");
      var ringValue = root.querySelector("[data-rrv-ring-value]");
      var ringWrap = root.querySelector("[data-rrv-ring]");
      var scoreNode = root.querySelector("[data-rrv-score]");
      var headline = root.querySelector("[data-rrv-headline]");
      var summary = root.querySelector("[data-rrv-summary]");
      var staleNote = root.querySelector("[data-rrv-stale]");
      var strengthsList = root.querySelector("[data-rrv-strengths]");
      var issuesList = root.querySelector("[data-rrv-issues]");
      var cleanNote = root.querySelector("[data-rrv-clean]");
      var scrollHint = root.querySelector("[data-rrv-scroll-hint]");
      var degraded = root.querySelector("[data-rrv-degraded]");
      var degradedText = root.querySelector("[data-rrv-degraded-text]");
      var status = root.querySelector("[data-rrv-status]");
      var quota = root.querySelector("[data-rrv-quota]");
      var remainingNode = root.querySelector("[data-rrv-remaining]");

      if (!trigger || !result || !issuesList) return;

      var semanticEnabled = root.getAttribute("data-semantic-enabled") === "1";
      var dailyLimit = parseInt(root.getAttribute("data-daily-limit"), 10);
      var remaining = parseInt(root.getAttribute("data-remaining"), 10);
      if (!Number.isFinite(dailyLimit)) dailyLimit = 5;
      if (!Number.isFinite(remaining)) remaining = dailyLimit;

      var isLoading = false;
      var checkedFingerprint = null;

      function setStatus(message, isError) {
        if (!status) return;
        status.textContent = message || "";
        status.classList.toggle("is-error", !!isError);
      }

      function renderTrigger() {
        trigger.disabled = isLoading;
        if (!triggerLabel) return;
        if (isLoading) {
          triggerLabel.textContent = "أفحص التقرير…";
        } else if (checkedFingerprint === null) {
          triggerLabel.textContent = "افحص جاهزية التقرير";
        } else if (root.classList.contains("is-stale")) {
          triggerLabel.textContent = "أعد الفحص بعد التعديل";
        } else {
          triggerLabel.textContent = "إعادة الفحص";
        }
      }

      /* شريط الجوال يعرض النتيجة نفسها على زرّه، فيعرف المستخدم حاله دون أن
         يمرّر إلى اللوحة. وهو يعرضها فقط — الفحص كلّه هنا. */
      var mobileBar = document.querySelector("[data-report-mobile-actions]");
      var mobileLabel = document.querySelector("[data-mobile-review-label]");

      function reflectOnMobileBar(level, score) {
        if (!mobileBar) return;
        mobileBar.classList.remove("is-ready", "is-almost", "is-needs-work");
        mobileBar.classList.add(LEVEL_CLASSES[level] || "is-needs-work");
        if (mobileLabel) mobileLabel.textContent = "الجاهزية " + score;
      }

      var mobileTrigger = document.querySelector("[data-mobile-review]");
      if (mobileTrigger) {
        mobileTrigger.addEventListener("click", function () {
          var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
          try {
            root.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "center" });
          } catch (error) {
            root.scrollIntoView();
          }
          trigger.click();
        });
      }

      function markStale(stale) {
        root.classList.toggle("is-stale", stale);
        if (staleNote) staleNote.hidden = !stale;
        renderTrigger();
      }

      function updateQuota(next) {
        if (!Number.isFinite(next)) return;
        remaining = Math.max(0, Math.min(dailyLimit, next));
        root.classList.toggle("is-quota-out", semanticEnabled && remaining <= 0);
        if (remainingNode) remainingNode.textContent = String(remaining);
        if (quota) quota.hidden = !semanticEnabled;
      }

      /* الانتقال إلى الحقل المقصود هو أنفع ما في اللوحة: ملاحظةٌ لا تُوصل إلى
         موضعها تترك المعلّم يبحث عنها في نموذج طويل. */
      function focusField(anchor) {
        if (!anchor) return;
        var node = null;
        try { node = document.querySelector(anchor); } catch (error) { node = null; }
        if (!node) return;

        // صفحة الإضافة تلفّ الحقل بـ‎.ar-field‎ وصفحة التعديل بـ‎.row‎.
        var wrapper = node.closest ? (node.closest(".ar-field, .row") || node) : node;
        var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        try {
          node.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "center" });
        } catch (error) {
          node.scrollIntoView();
        }
        wrapper.classList.remove("is-review-target");
        // إعادة تشغيل الحركة تحتاج إطاراً بلا الصنف.
        window.requestAnimationFrame(function () {
          wrapper.classList.add("is-review-target");
          window.setTimeout(function () { wrapper.classList.remove("is-review-target"); }, 2200);
        });
        if (typeof node.focus === "function") {
          try { node.focus({ preventScroll: true }); } catch (error) { node.focus(); }
        }
      }

      function buildIssue(issue) {
        var item = document.createElement("li");
        var button = document.createElement("button");
        button.type = "button";
        button.className = "rrv-issue";
        button.setAttribute("data-severity", issue.severity || "medium");

        var mark = document.createElement("span");
        mark.className = "rrv-issue-mark";
        mark.setAttribute("aria-hidden", "true");
        button.appendChild(mark);

        var body = document.createElement("div");
        body.className = "rrv-issue-body";

        var top = document.createElement("div");
        top.className = "rrv-issue-top";
        var field = document.createElement("span");
        field.className = "rrv-issue-field";
        field.textContent = issue.field_label || "";
        top.appendChild(field);
        var severity = document.createElement("span");
        severity.className = "rrv-issue-severity";
        severity.textContent = SEVERITY_LABELS[issue.severity] || SEVERITY_LABELS.medium;
        top.appendChild(severity);
        body.appendChild(top);

        var message = document.createElement("p");
        message.className = "rrv-issue-message";
        message.textContent = issue.message || "";
        body.appendChild(message);

        if (issue.hint) {
          var hint = document.createElement("p");
          hint.className = "rrv-issue-hint";
          hint.textContent = issue.hint;
          body.appendChild(hint);
        }

        if (issue.anchor) {
          var go = document.createElement("span");
          go.className = "rrv-issue-go";
          go.appendChild(icon("fa-solid fa-arrow-turn-down"));
          var goText = document.createElement("span");
          goText.textContent = "الانتقال إلى " + (issue.field_label || "الحقل");
          go.appendChild(goText);
          body.appendChild(go);
          button.addEventListener("click", function () { focusField(issue.anchor); });
        } else {
          button.disabled = true;
        }

        button.appendChild(body);
        item.appendChild(button);
        return item;
      }

      function renderStrengths(strengths) {
        if (!strengthsList) return;
        strengthsList.textContent = "";
        var list = Array.isArray(strengths) ? strengths : [];
        list.forEach(function (text) {
          var item = document.createElement("li");
          item.textContent = String(text || "");
          strengthsList.appendChild(item);
        });
        strengthsList.hidden = list.length === 0;
      }

      function renderIssues(issues) {
        issuesList.textContent = "";
        var list = Array.isArray(issues) ? issues : [];
        list.forEach(function (issue) {
          if (issue && typeof issue === "object") issuesList.appendChild(buildIssue(issue));
        });
        issuesList.hidden = list.length === 0;
        if (cleanNote) cleanNote.hidden = list.length !== 0;

        /* سقف ارتفاع القائمة يقصّ آخر بطاقة، والقصّ بلا إشعار يبدو عطلاً في
           العرض لا محتوىً وراءه المزيد. القياس بعد الرسم لأن الارتفاع الفعلي
           لا يُعرف قبله. */
        if (scrollHint) {
          window.requestAnimationFrame(function () {
            scrollHint.hidden = issuesList.scrollHeight <= issuesList.clientHeight + 4;
          });
        }
      }

      /* «2 ملاحظات» عربيةٌ مترجَمة لا عربية. المثنّى صيغةٌ قائمة بذاتها، وإهماله
         أول ما يكشف أن الواجهة لم تُكتب بالعربية أصلاً. */
      function countPhrase(count, forms) {
        if (count === 1) return forms.one;
        if (count === 2) return forms.two;
        if (count <= 10) return count + " " + forms.few;
        return count + " " + forms.many;
      }

      function summaryText(payload) {
        var issues = Array.isArray(payload.issues) ? payload.issues : [];
        if (issues.length === 0) return "لا ملاحظات على البنود المفعّلة.";
        var high = issues.filter(function (issue) { return issue.severity === "high"; }).length;
        var rest = issues.length - high;
        var parts = [];
        if (high) {
          parts.push(countPhrase(high, {
            one: "ملاحظة واحدة توجب المراجعة",
            two: "ملاحظتان توجبان المراجعة",
            few: "ملاحظات توجب المراجعة",
            many: "ملاحظة توجب المراجعة"
          }));
        }
        if (rest) {
          parts.push(countPhrase(rest, {
            one: "ملاحظة أخرى",
            two: "ملاحظتان أخريان",
            few: "ملاحظات أخرى",
            many: "ملاحظة أخرى"
          }));
        }
        return parts.join(" و") + ". اضغط أي ملاحظة للانتقال إلى موضعها.";
      }

      function degradedMessage(payload) {
        if (!semanticEnabled) return "";
        if (payload.reason === "structure_first") {
          return "أكمِل الملاحظات الأساسية أعلاه ثم أعد الفحص لقراءة ترابط البنود — ولم تُحتسب لك محاولة.";
        }
        if (payload.reason === "quota_exhausted") {
          return "انتهى رصيد الفحص الذكي اليوم، وهذه نتيجة الفحص البنيوي وحده. يعود الرصيد غدًا.";
        }
        if (payload.semantic === false) {
          return "تعذّر الفحص الذكي الآن، وهذه نتيجة الفحص البنيوي وحده — ولم تُحتسب لك محاولة.";
        }
        return "";
      }

      function render(payload) {
        var score = Math.max(0, Math.min(100, parseInt(payload.score, 10) || 0));
        var level = LEVEL_CLASSES[payload.level] ? payload.level : "needs_work";

        root.classList.remove("is-ready", "is-almost", "is-needs-work");
        root.classList.add(LEVEL_CLASSES[level], "is-checked");

        if (intro) intro.hidden = true;
        result.hidden = false;

        if (scoreNode) scoreNode.textContent = String(score);
        if (ringValue) {
          ringValue.style.strokeDashoffset = String(RING_CIRCUMFERENCE * (1 - score / 100));
        }
        if (ringWrap) ringWrap.setAttribute("aria-label", "درجة الجاهزية " + score + " من 100");
        if (headline) headline.textContent = payload.headline || "";
        if (summary) summary.textContent = summaryText(payload);

        renderStrengths(payload.strengths);
        renderIssues(payload.issues);

        var note = degradedMessage(payload);
        if (degraded && degradedText) {
          degradedText.textContent = note;
          degraded.hidden = !note;
        }

        updateQuota(parseInt(payload.remaining, 10));
        markStale(false);
        reflectOnMobileBar(level, score);

        if (payload.cached) {
          setStatus("نتيجة الفحص نفسه — لم يتغيّر نص التقرير، فلم تُحتسب محاولة.", false);
        } else if (payload.checked_at) {
          setStatus("آخر فحص " + payload.checked_at + ".", false);
        } else {
          setStatus("", false);
        }
      }

      trigger.addEventListener("click", function () {
        if (isLoading) return;
        var draft = collectDraft();

        isLoading = true;
        renderTrigger();
        setStatus("أقرأ التقرير كما يقرؤه المراجع…", false);

        var controller = typeof window.AbortController === "function" ? new window.AbortController() : null;
        var timer = window.setTimeout(function () { if (controller) controller.abort(); }, 32000);

        fetch(endpoint, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
          signal: controller ? controller.signal : undefined,
          body: JSON.stringify(draft)
        })
          .then(function (response) {
            return response.json().catch(function () {
              return { ok: false, message: "تعذر قراءة رد خدمة الفحص." };
            });
          })
          .then(function (payload) {
            if (!payload || payload.ok !== true) {
              setStatus((payload && payload.message) || "تعذر إجراء الفحص الآن.", true);
              return;
            }
            checkedFingerprint = fingerprint(draft);
            render(payload);
          })
          .catch(function () {
            setStatus("تعذر الوصول إلى خدمة الفحص. تحقق من الاتصال ثم أعد المحاولة.", true);
          })
          .then(function () {
            window.clearTimeout(timer);
            isLoading = false;
            renderTrigger();
          });
      });

      /* نتيجةٌ معروضة عن نصٍّ تغيّر بعدها تضلّل أكثر مما تفيد، فتُعلَّم قديمة
         بدل أن تُمحى: الملاحظات تبقى مقروءة والمعلّم يعرف أنها عن نسخةٍ سابقة. */
      if (form) {
        // إضافة شاهدٍ أو إزالته تغيّر المسودة كما يغيّرها الكتابة في حقل.
        document.addEventListener("evidence:changed", function () {
          if (checkedFingerprint === null || isLoading) return;
          markStale(fingerprint(collectDraft()) !== checkedFingerprint);
        });
        var onEdit = function () {
          if (checkedFingerprint === null || isLoading) return;
          markStale(fingerprint(collectDraft()) !== checkedFingerprint);
        };
        form.addEventListener("input", onEdit);
        form.addEventListener("change", onEdit);
      }

      updateQuota(remaining);
      renderTrigger();
    }
  );
})();
