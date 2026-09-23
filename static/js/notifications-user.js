(function () {
  'use strict';

  var page = document.querySelector('[data-notifications-page]');
  if (!page) return;

  function setBusy(button, label) {
    if (!button || button.disabled) return;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.dataset.originalLabel = button.textContent.trim();
    button.textContent = label;
  }

  function initInbox() {
    var items = Array.prototype.slice.call(page.querySelectorAll('[data-notification-item]'));
    var search = page.querySelector('[data-notification-search]');
    var unreadOnly = page.querySelector('[data-only-unread]');
    var empty = page.querySelector('[data-filter-empty]');
    var storageKey = 'notif_only_unread_v1';

    function applyFilters() {
      var term = search ? String(search.value || '').trim().toLocaleLowerCase('ar') : '';
      var onlyUnread = !!(unreadOnly && unreadOnly.checked);
      var visible = 0;

      items.forEach(function (item) {
        var matchesState = !onlyUnread || item.dataset.read !== '1';
        var matchesTerm = !term || String(item.textContent || '').toLocaleLowerCase('ar').indexOf(term) !== -1;
        var matches = matchesState && matchesTerm;
        item.hidden = !matches;
        if (matches) visible += 1;
      });

      if (empty) empty.hidden = !(items.length > 0 && visible === 0);
    }

    if (unreadOnly) {
      try {
        unreadOnly.checked = window.localStorage.getItem(storageKey) === '1';
      } catch (error) {
        unreadOnly.checked = false;
      }
      unreadOnly.addEventListener('change', function () {
        try {
          window.localStorage.setItem(storageKey, unreadOnly.checked ? '1' : '0');
        } catch (error) {
          // Storage is optional; filtering still works for this page view.
        }
        applyFilters();
      });
    }

    if (search) search.addEventListener('input', applyFilters);

    var markAll = page.querySelector('[data-mark-all-button]');
    if (markAll && markAll.form) {
      markAll.form.addEventListener('submit', function () {
        setBusy(markAll, 'جارٍ تحديد الكل…');
      });
    }

    page.querySelectorAll('[data-mark-one-button]').forEach(function (button) {
      if (!button.form) return;
      button.form.addEventListener('submit', function () {
        setBusy(button, 'جارٍ التحديث…');
      });
    });

    applyFilters();
  }

  function initSignature() {
    var form = page.querySelector('.notification-signature-form');
    var acknowledgement = document.getElementById('sig_ack');
    var submit = document.getElementById('sigSubmit');
    if (!form || !acknowledgement || !submit || !window.HandwrittenSignature) return;

    var signature = window.HandwrittenSignature.init(form, updateState);

    function updateState() {
      var busy = submit.dataset.busy === '1';
      submit.disabled = busy || !acknowledgement.checked || !signature || !signature.valid();
    }

    acknowledgement.addEventListener('change', updateState);
    form.addEventListener('submit', async function (event) {
      updateState();
      if (submit.disabled) {
        event.preventDefault();
        return;
      }

      if (form.dataset.signatureConfirmed !== '1') {
        event.preventDefault();
        var confirmed = await window.rcConfirm({
          message: 'سيتم تسجيل إقرارك ووقت التوقيع رسميًا. هل تريد اعتماد التوقيع الآن؟',
          type: 'warning',
          title: 'اعتماد التوقيع',
          okText: 'اعتماد التوقيع'
        });
        if (!confirmed || !signature.prepare()) return;
        form.dataset.signatureConfirmed = '1';
        form.requestSubmit(submit);
        return;
      }

      submit.dataset.busy = '1';
      setBusy(submit, 'جارٍ اعتماد التوقيع…');
    });

    updateState();
  }

  if (page.dataset.notificationsPage === 'inbox') initInbox();
  if (page.dataset.notificationsPage === 'detail') initSignature();
})();
