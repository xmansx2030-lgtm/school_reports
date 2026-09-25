(function () {
  "use strict";

  var DB_NAME = "tawtheeq-mobile-drafts";
  var STORE_NAME = "drafts";
  var DB_VERSION = 1;
  var MAX_DRAFT_BYTES = 48 * 1024 * 1024;
  var MAX_DRAFT_AGE_MS = 14 * 24 * 60 * 60 * 1000;

  function openDb() {
    return new Promise(function (resolve, reject) {
      if (!window.indexedDB) return reject(new Error("indexeddb-unavailable"));
      var request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = function () {
        var db = request.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) db.createObjectStore(STORE_NAME, { keyPath: "key" });
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function transact(mode, callback) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE_NAME, mode);
        var result = callback(tx.objectStore(STORE_NAME));
        tx.oncomplete = function () { db.close(); resolve(result); };
        tx.onerror = function () { db.close(); reject(tx.error); };
      });
    });
  }

  function readDraft(key) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE_NAME, "readonly");
        var request = tx.objectStore(STORE_NAME).get(key);
        request.onsuccess = function () { db.close(); resolve(request.result || null); };
        request.onerror = function () { db.close(); reject(request.error); };
      });
    });
  }

  function deleteDraft(key) {
    return transact("readwrite", function (store) { store.delete(key); }).catch(function () {});
  }

  function cleanDrafts(predicate) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE_NAME, "readwrite");
        var request = tx.objectStore(STORE_NAME).openCursor();
        request.onsuccess = function () {
          var cursor = request.result;
          if (!cursor) return;
          if (predicate(cursor.value)) cursor.delete();
          cursor.continue();
        };
        tx.oncomplete = function () { db.close(); resolve(); };
        tx.onerror = function () { db.close(); reject(tx.error); };
      });
    }).catch(function () {});
  }

  function fieldValue(field) {
    if (field.multiple) return { value: Array.prototype.map.call(field.selectedOptions, function (option) { return option.value; }) };
    return { value: field.value };
  }

  function collect(form, key) {
    var fields = {};
    var files = {};
    var totalBytes = 0;
    Array.prototype.forEach.call(form.elements, function (field) {
      if (!field.name || field.disabled || field.name === "csrfmiddlewaretoken") return;
      if (/^(submit|button|reset|password)$/i.test(field.type)) return;
      if (field.type === "file") {
        var selected = Array.prototype.slice.call(field.files || []);
        if (selected.length) {
          files[field.name] = selected.map(function (file) {
            totalBytes += file.size || 0;
            return { blob: file, name: file.name, type: file.type, lastModified: file.lastModified };
          });
        }
        return;
      }
      if (field.type === "checkbox" || field.type === "radio") {
        fields[field.name] = fields[field.name] || { choices: {} };
        fields[field.name].choices[field.value || "on"] = field.checked;
        return;
      }
      fields[field.name] = fieldValue(field);
    });
    return { key: key, savedAt: Date.now(), path: location.pathname, fields: fields, files: files, totalBytes: totalBytes };
  }

  function restoreField(field, stored) {
    if (!field || !stored) return;
    if (field.type === "checkbox" || field.type === "radio") {
      field.checked = Boolean(stored.choices && stored.choices[field.value || "on"]);
    }
    else if (field.multiple && Array.isArray(stored.value)) {
      Array.prototype.forEach.call(field.options, function (option) { option.selected = stored.value.indexOf(option.value) !== -1; });
    } else field.value = stored.value == null ? "" : stored.value;
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function restoreFiles(field, records) {
    if (!field || !records || !records.length || typeof DataTransfer === "undefined") return;
    var transfer = new DataTransfer();
    records.forEach(function (record) {
      var file = record.blob instanceof File
        ? record.blob
        : new File([record.blob], record.name || "image", { type: record.type || "application/octet-stream", lastModified: record.lastModified || Date.now() });
      transfer.items.add(file);
    });
    field.files = transfer.files;
    field.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function formatTime(timestamp) {
    try { return new Date(timestamp).toLocaleString("ar-SA", { dateStyle: "short", timeStyle: "short" }); }
    catch (error) { return ""; }
  }

  function showRestoreNotice(form, draft, restore, discard) {
    var existing = document.querySelector('[data-pwa-draft-notice="' + CSS.escape(draft.key) + '"]');
    if (existing) return;
    var notice = document.createElement("section");
    notice.className = "pwa-draft-notice";
    notice.setAttribute("data-pwa-draft-notice", draft.key);
    notice.setAttribute("role", "status");
    notice.innerHTML = '<i class="fa-solid fa-cloud-arrow-down" aria-hidden="true"></i>' +
      '<div><strong>لديك مسودة محفوظة على هذا الجهاز</strong><span>آخر حفظ: ' + formatTime(draft.savedAt) + '</span></div>' +
      '<div class="pwa-draft-notice__actions"><button type="button" data-restore>استعادة</button><button type="button" data-discard>تجاهل</button></div>';
    form.parentNode.insertBefore(notice, form);
    notice.querySelector("[data-restore]").addEventListener("click", function () { restore(); notice.remove(); });
    notice.querySelector("[data-discard]").addEventListener("click", function () { discard(); notice.remove(); });
  }

  function announceSaved(form) {
    var tag = document.getElementById("draftSaved");
    if (tag) {
      tag.hidden = false;
      tag.style.display = "flex";
      window.clearTimeout(tag._draftSavedTimer);
      tag._draftSavedTimer = window.setTimeout(function () { tag.hidden = true; tag.style.display = "none"; }, 1800);
    } else {
      tag = document.createElement("div");
      tag.className = "pwa-draft-saved-indicator";
      tag.setAttribute("role", "status");
      tag.innerHTML = '<i class="fa-solid fa-cloud-arrow-up" aria-hidden="true"></i><span>حُفظت المسودة على هذا الجهاز</span>';
      document.body.appendChild(tag);
      window.setTimeout(function () { tag.remove(); }, 1800);
    }
    form.dataset.pwaDraftSaved = "true";
    window.setTimeout(function () { delete form.dataset.pwaDraftSaved; }, 1800);
  }

  function bindForm(form) {
    var key = form.getAttribute("data-pwa-draft-key");
    if (!key) return;
    var timer = null;
    var saving = false;

    function save() {
      if (saving) return;
      var draft = collect(form, key);
      if (draft.totalBytes > MAX_DRAFT_BYTES) {
        form.dispatchEvent(new CustomEvent("tawtheeq:draft-too-large", { bubbles: true, detail: { bytes: draft.totalBytes } }));
        return;
      }
      saving = true;
      transact("readwrite", function (store) { store.put(draft); })
        .then(function () { announceSaved(form); })
        .catch(function () {})
        .finally(function () { saving = false; });
    }

    function schedule() {
      window.clearTimeout(timer);
      timer = window.setTimeout(save, 650);
    }

    form.addEventListener("input", schedule);
    form.addEventListener("change", schedule);
    form.addEventListener("tawtheeq:submit-success", function () { deleteDraft(key); });
    window.addEventListener("tawtheeq:submit-success", function (event) {
      if (!event.detail || !event.detail.form || event.detail.form === form) deleteDraft(key);
    });

    readDraft(key).then(function (draft) {
      if (!draft) return;
      showRestoreNotice(form, draft, function () {
        Object.keys(draft.fields || {}).forEach(function (name) {
          var fields = form.querySelectorAll('[name="' + CSS.escape(name) + '"]');
          if (!fields.length && form.elements.namedItem(name)) fields = [form.elements.namedItem(name)];
          Array.prototype.forEach.call(fields, function (field) { restoreField(field, draft.fields[name]); });
        });
        Object.keys(draft.files || {}).forEach(function (name) { restoreFiles(form.elements.namedItem(name), draft.files[name]); });
      }, function () { deleteDraft(key); });
    }).catch(function () {});
  }

  var params = new URLSearchParams(location.search);
  var completedKey = params.get("draft_saved");
  if (completedKey) {
    deleteDraft(completedKey);
    window.setTimeout(function () {
      window.dispatchEvent(new CustomEvent("tawtheeq:task-complete", { detail: { kind: "saved-form" } }));
    }, 0);
    params.delete("draft_saved");
    history.replaceState({}, "", location.pathname + (params.toString() ? "?" + params.toString() : "") + location.hash);
  }

  document.querySelectorAll("form[data-pwa-draft-key]").forEach(bindForm);
  cleanDrafts(function (draft) { return !draft.savedAt || Date.now() - draft.savedAt > MAX_DRAFT_AGE_MS; });

  document.addEventListener("click", function (event) {
    var logout = event.target.closest && event.target.closest('a[href*="/logout"]');
    if (!logout) return;
    var userId = document.body.getAttribute("data-pwa-user-id");
    if (!userId) return;
    event.preventDefault();
    var href = logout.href;
    cleanDrafts(function (draft) {
      return String(draft.key || "").indexOf("-u" + userId) !== -1;
    }).finally(function () { location.href = href; });
  }, true);
  if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(function () {});
}());
