/* مسجّل الإملاء الصوتي لتفاصيل التقرير.
 *
 * الترتيب المقصود: سجّل ← استمع ← فرّغ ← راجع ← أدرج. ولا خطوة تقفز فوق
 * الأخرى: النص لا يدخل حقل التقرير إلا باختيار المعلّم صراحةً.
 *
 * ثلاثة أمور يسهل نسيانها وقد عولجت هنا:
 *  ١. إيقاف مسارات الميكروفون بعد الانتهاء — بدونها يبقى مؤشّر التسجيل مضاءً
 *     في الجهاز بعد أن ظنّ المستخدم أنه أنهى.
 *  ٢. تحرير عنوان الـ blob السابق قبل إنشاء غيره، وإلا تسرّب في كل محاولة.
 *  ٣. الإيقاف التلقائي عند الحد الأقصى — تسجيلٌ منسيّ يعني رفعاً ضخماً يُرفض
 *     بعد انتظار طويل.
 */
(function () {
  "use strict";

  var MIME_CANDIDATES = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4",
    "audio/mpeg"
  ];

  function csrfToken() {
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return input ? input.value : "";
  }

  function isStandalone() {
    try {
      if (window.navigator && window.navigator.standalone === true) return true;
      return !!(window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
    } catch (error) {
      return false;
    }
  }

  function canRecord() {
    return !!(
      window.MediaRecorder &&
      navigator.mediaDevices &&
      typeof navigator.mediaDevices.getUserMedia === "function"
    );
  }

  function pickMimeType() {
    if (typeof window.MediaRecorder.isTypeSupported !== "function") return "";
    for (var i = 0; i < MIME_CANDIDATES.length; i += 1) {
      if (window.MediaRecorder.isTypeSupported(MIME_CANDIDATES[i])) return MIME_CANDIDATES[i];
    }
    return "";
  }

  function clock(seconds) {
    var total = Math.max(0, Math.floor(seconds));
    var mins = Math.floor(total / 60);
    var secs = total % 60;
    return mins + ":" + (secs < 10 ? "0" : "") + secs;
  }

  function setup(root) {
    var target = document.getElementById(root.getAttribute("data-target-id") || "id_idea");
    var endpoint = root.getAttribute("data-endpoint") || "";
    var documentName = root.getAttribute("data-document-name") || "التقرير";
    var recordingPrompt = root.getAttribute("data-recording-prompt") ||
      "جارٍ التسجيل… تحدّث بوضوح عن تفاصيل التقرير.";
    if (!target || !endpoint) return;

    var dailyLimit = parseInt(root.getAttribute("data-daily-limit"), 10) || 3;
    var remaining = parseInt(root.getAttribute("data-remaining"), 10);
    var maxSeconds = parseInt(root.getAttribute("data-max-seconds"), 10) || 180;
    var maxBytes = parseInt(root.getAttribute("data-max-bytes"), 10) || 10485760;
    var pwaOnly = root.getAttribute("data-pwa-only") === "1";
    if (!Number.isFinite(remaining)) remaining = dailyLimit;

    var el = {
      locked: root.querySelector("[data-voice-locked]"),
      lockedMessage: root.querySelector("[data-voice-locked-message]"),
      stage: root.querySelector("[data-voice-stage]"),
      meter: root.querySelector("[data-voice-meter]"),
      bars: root.querySelectorAll("[data-voice-meter] i"),
      timer: root.querySelector("[data-voice-timer]"),
      elapsed: root.querySelector("[data-voice-elapsed]"),
      countdown: root.querySelector("[data-voice-remaining-time]"),
      record: root.querySelector("[data-voice-record]"),
      recordLabel: root.querySelector("[data-voice-record-label]"),
      retry: root.querySelector("[data-voice-retry]"),
      review: root.querySelector("[data-voice-review]"),
      player: root.querySelector("[data-voice-player]"),
      send: root.querySelector("[data-voice-send]"),
      status: root.querySelector("[data-voice-status]"),
      remaining: root.querySelector("[data-voice-remaining]"),
      quota: root.querySelector("[data-voice-quota]"),
      result: root.querySelector("[data-voice-result]"),
      output: root.querySelector("[data-voice-output]"),
      raw: root.querySelector("[data-voice-raw]"),
      rawOutput: root.querySelector("[data-voice-raw-output]"),
      append: root.querySelector("[data-voice-append]"),
      replace: root.querySelector("[data-voice-replace]"),
      discard: root.querySelector("[data-voice-discard]")
    };

    var recorder = null;
    var stream = null;
    var audioContext = null;
    var analyser = null;
    var meterFrame = null;
    var chunks = [];
    var blob = null;
    var blobUrl = "";
    var startedAt = 0;
    var tick = null;
    var isSending = false;
    var suggestion = "";

    root.hidden = false;

    function status(message, kind) {
      if (!el.status) return;
      el.status.textContent = message || "";
      el.status.classList.toggle("is-error", kind === "error");
      el.status.classList.toggle("is-success", kind === "success");
    }

    function lock(message) {
      if (el.stage) el.stage.hidden = true;
      if (el.locked) el.locked.hidden = false;
      if (message && el.lockedMessage) el.lockedMessage.textContent = message;
      if (el.quota) el.quota.hidden = true;
    }

    /* ── البوابة: التطبيق المثبَّت ودعم المتصفّح ── */
    if (!canRecord()) {
      lock("متصفّح جهازك لا يدعم التسجيل الصوتي. حدّثه أو استخدم الكتابة اليدوية.");
      return;
    }
    if (pwaOnly && !isStandalone()) {
      lock();
      return;
    }
    if (el.stage) el.stage.hidden = false;

    function renderQuota(value) {
      if (typeof value !== "undefined") {
        var parsed = parseInt(value, 10);
        if (Number.isFinite(parsed)) remaining = Math.max(0, Math.min(dailyLimit, parsed));
      }
      root.setAttribute("data-remaining", String(remaining));
      root.classList.toggle("is-exhausted", remaining <= 0);
      if (el.remaining) el.remaining.textContent = String(remaining);
      if (el.send) el.send.disabled = isSending || remaining <= 0 || !blob;
      if (el.record) el.record.disabled = remaining <= 0 && !isRecording();
    }

    function isRecording() {
      return !!recorder && recorder.state === "recording";
    }

    function stopMeter() {
      if (meterFrame) window.cancelAnimationFrame(meterFrame);
      meterFrame = null;
      Array.prototype.forEach.call(el.bars, function (bar) {
        bar.style.height = "6px";
      });
      if (audioContext) {
        try { audioContext.close(); } catch (error) { /* المتصفّح أغلقه سلفاً */ }
      }
      audioContext = null;
      analyser = null;
    }

    function startMeter(sourceStream) {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx || !el.bars.length) return;
      try {
        audioContext = new Ctx();
        var source = audioContext.createMediaStreamSource(sourceStream);
        analyser = audioContext.createAnalyser();
        analyser.fftSize = 64;
        analyser.smoothingTimeConstant = 0.75;
        source.connect(analyser);
        var data = new Uint8Array(analyser.frequencyBinCount);

        var paint = function () {
          if (!analyser) return;
          analyser.getByteFrequencyData(data);
          var count = el.bars.length;
          for (var i = 0; i < count; i += 1) {
            var slot = Math.floor((i / count) * data.length);
            var level = data[slot] / 255;
            el.bars[i].style.height = Math.max(6, Math.round(6 + level * 34)) + "px";
          }
          meterFrame = window.requestAnimationFrame(paint);
        };
        paint();
      } catch (error) {
        stopMeter();
      }
    }

    function releaseStream() {
      if (stream) {
        stream.getTracks().forEach(function (track) { track.stop(); });
      }
      stream = null;
    }

    function clearBlob() {
      if (blobUrl) window.URL.revokeObjectURL(blobUrl);
      blobUrl = "";
      blob = null;
      if (el.player) el.player.removeAttribute("src");
      if (el.review) el.review.hidden = true;
    }

    function stopTimer() {
      if (tick) window.clearInterval(tick);
      tick = null;
      if (el.timer) {
        el.timer.hidden = true;
        el.timer.classList.remove("is-ending");
      }
    }

    function startTimer() {
      startedAt = Date.now();
      if (el.timer) el.timer.hidden = false;
      var paint = function () {
        var seconds = (Date.now() - startedAt) / 1000;
        var left = Math.max(0, maxSeconds - seconds);
        if (el.elapsed) el.elapsed.textContent = clock(seconds);
        if (el.countdown) {
          el.countdown.textContent = left <= 20
            ? "يتوقف تلقائيًا خلال " + clock(left)
            : "الحد الأقصى " + clock(maxSeconds);
        }
        if (el.timer) el.timer.classList.toggle("is-ending", left <= 20);
        if (left <= 0) stopRecording("بلغ التسجيل الحد الأقصى وتوقف تلقائيًا.");
      };
      paint();
      tick = window.setInterval(paint, 250);
    }

    function setRecordingUi(active) {
      root.classList.toggle("is-recording", active);
      if (el.recordLabel) el.recordLabel.textContent = active ? "إيقاف التسجيل" : "ابدأ التسجيل";
      if (el.record) {
        el.record.setAttribute("aria-pressed", active ? "true" : "false");
        var icon = el.record.querySelector("i");
        if (icon) icon.className = active ? "fa-solid fa-stop" : "fa-solid fa-microphone";
      }
    }

    function showRaw(rawText) {
      if (!el.raw || !el.rawOutput) return;
      var differs = rawText && rawText !== suggestion;
      el.rawOutput.textContent = differs ? rawText : "";
      el.raw.open = false;
      el.raw.hidden = !differs;
    }

    function startRecording() {
      if (remaining <= 0) {
        status("اكتمل رصيدك اليوم. يعود تلقائيًا غدًا.", "error");
        return;
      }
      clearBlob();
      if (el.result) el.result.hidden = true;
      if (el.retry) el.retry.hidden = true;
      status("جارٍ طلب إذن الميكروفون…");

      navigator.mediaDevices
        .getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
        })
        .then(function (granted) {
          stream = granted;
          var mimeType = pickMimeType();
          recorder = mimeType
            ? new window.MediaRecorder(granted, { mimeType: mimeType })
            : new window.MediaRecorder(granted);
          chunks = [];

          recorder.addEventListener("dataavailable", function (event) {
            if (event.data && event.data.size > 0) chunks.push(event.data);
          });

          recorder.addEventListener("stop", function () {
            stopTimer();
            stopMeter();
            releaseStream();
            setRecordingUi(false);

            var type = (recorder && recorder.mimeType) || mimeType || "audio/webm";
            blob = new Blob(chunks, { type: type });
            chunks = [];

            if (blob.size < 2000) {
              clearBlob();
              status("التسجيل قصير جدًا. تحدّث بضع ثوانٍ ثم أعد المحاولة.", "error");
              renderQuota();
              return;
            }
            if (blob.size > maxBytes) {
              clearBlob();
              status("التسجيل أكبر من المسموح. سجّل مقطعًا أقصر.", "error");
              renderQuota();
              return;
            }

            blobUrl = window.URL.createObjectURL(blob);
            if (el.player) el.player.src = blobUrl;
            if (el.review) el.review.hidden = false;
            if (el.retry) el.retry.hidden = false;
            status("استمع للتسجيل، ثم حوّله إلى نص.", "success");
            renderQuota();
          });

          recorder.start();
          setRecordingUi(true);
          startTimer();
          startMeter(granted);
          status(recordingPrompt);
        })
        .catch(function (error) {
          releaseStream();
          setRecordingUi(false);
          var denied = error && (error.name === "NotAllowedError" || error.name === "SecurityError");
          status(
            denied
              ? "لم يُسمح باستخدام الميكروفون. فعّل الإذن من إعدادات التطبيق ثم أعد المحاولة."
              : "تعذر بدء التسجيل على هذا الجهاز.",
            "error"
          );
        });
    }

    function stopRecording(message) {
      if (!isRecording()) return;
      try {
        recorder.stop();
      } catch (error) {
        stopTimer();
        stopMeter();
        releaseStream();
        setRecordingUi(false);
      }
      if (message) status(message);
    }

    function send() {
      if (!blob || isSending) return;
      isSending = true;
      renderQuota();
      if (el.send) {
        el.send.innerHTML = '<i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i> جارٍ التفريغ…';
      }
      status("أفرّغ التسجيل وأرتّب النص… قد يستغرق بضع ثوانٍ.");

      var form = new FormData();
      form.append("audio", blob, "report.webm");

      var controller = typeof window.AbortController === "function" ? new window.AbortController() : null;
      var timeout = window.setTimeout(function () {
        if (controller) controller.abort();
      }, 90000);

      fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "X-CSRFToken": csrfToken(),
          /* إشارة السطح: يقرؤها الخادم ليقصر الميزة على التطبيق المثبَّت. */
          "X-Tawtheeq-Surface": isStandalone() ? "standalone" : "browser"
        },
        signal: controller ? controller.signal : undefined,
        body: form
      })
        .then(function (response) {
          return response.json().catch(function () {
            return { ok: false, message: "تعذر قراءة رد خدمة التفريغ." };
          }).then(function (data) {
            if (!response.ok || !data.ok) {
              var failure = new Error(data.message || "تعذر تفريغ التسجيل الآن.");
              failure.remaining = data.remaining;
              throw failure;
            }
            return data;
          });
        })
        .then(function (data) {
          suggestion = String(data.text || "").trim();
          if (!suggestion) throw new Error("لم يصل نص من التسجيل. حاول مرة أخرى.");
          if (el.output) el.output.textContent = suggestion;
          /* التفريغ الحرفي يُعرض حين يختلف عن المحرَّر وحده: تكراره كما هو ضجيج،
             وإخفاؤه عند اختلافه يترك المعلّم بلا وسيلة لكشف تفريغ خاطئ. */
          showRaw(String(data.raw_text || "").trim());
          if (el.result) el.result.hidden = false;
          renderQuota(data.remaining);
          status("راجع النص، ثم أضفه إلى " + documentName + " أو استبدل به النص الحالي.", "success");
          if (el.result) el.result.scrollIntoView({ behavior: "smooth", block: "nearest" });
        })
        .catch(function (error) {
          if (typeof error.remaining !== "undefined") renderQuota(error.remaining);
          status(
            error && error.name === "AbortError"
              ? "استغرق التفريغ وقتًا أطول من المتوقع. حاول مرة أخرى."
              : (error.message || "تعذر تفريغ التسجيل الآن."),
            "error"
          );
        })
        .then(function () {
          window.clearTimeout(timeout);
          isSending = false;
          if (el.send) {
            el.send.innerHTML =
              '<i class="fa-solid fa-wand-magic-sparkles" aria-hidden="true"></i> تحويل التسجيل إلى نص';
          }
          renderQuota();
        });
    }

    function insert(mode) {
      if (!suggestion) return;
      var current = String(target.value || "").trim();
      target.value = mode === "append" && current ? current + "\n\n" + suggestion : suggestion;
      target.dispatchEvent(new Event("input", { bubbles: true }));
      target.dispatchEvent(new Event("change", { bubbles: true }));
      if (el.result) el.result.hidden = true;
      clearBlob();
      if (el.retry) el.retry.hidden = true;
      suggestion = "";
      status("أُدرج النص في " + documentName + ". راجعه قبل الحفظ.", "success");
      target.focus();
    }

    if (el.record) {
      el.record.addEventListener("click", function () {
        if (isRecording()) stopRecording();
        else startRecording();
      });
    }
    if (el.retry) {
      el.retry.addEventListener("click", function () {
        clearBlob();
        if (el.result) el.result.hidden = true;
        el.retry.hidden = true;
        suggestion = "";
        status("");
        startRecording();
      });
    }
    if (el.send) el.send.addEventListener("click", send);
    if (el.append) el.append.addEventListener("click", function () { insert("append"); });
    if (el.replace) el.replace.addEventListener("click", function () { insert("replace"); });
    if (el.discard) {
      el.discard.addEventListener("click", function () {
        suggestion = "";
        if (el.result) el.result.hidden = true;
        status("تم تجاهل النص المفرَّغ.");
      });
    }

    /* مغادرة الصفحة أثناء التسجيل يجب أن تُطفئ الميكروفون. */
    window.addEventListener("pagehide", function () {
      stopRecording();
      stopMeter();
      releaseStream();
      clearBlob();
    });

    renderQuota();
  }

  Array.prototype.forEach.call(document.querySelectorAll("[data-report-voice]"), setup);
}());
