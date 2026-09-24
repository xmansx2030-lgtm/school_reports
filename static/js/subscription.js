(() => {
  function initSubscriptionPage() {
  const pageRoot = document.getElementById('archiveOrder');
  if (!pageRoot || pageRoot.dataset.subscriptionReady === '1') return;
  pageRoot.dataset.subscriptionReady = '1';
  const billingRoot = pageRoot.closest('.subx');

  /* ── 1. الطلب الموحّد: المفاتيح والإجمالي الحيّ ── */
  const checkout = window.SubscriptionCheckout?.init(pageRoot);
  const form = checkout?.form || document.getElementById('paymentForm');
  const submitBtn = checkout?.receiptSubmit || document.getElementById('submitBtn');
  const tamaraSubmit = checkout?.tamaraSubmit || document.getElementById('tamaraSubmit');
  const moyasarSubmit = checkout?.moyasarSubmit || document.getElementById('moyasarSubmit');
  const isOn = checkout?.isOn || ((name) => {
    const control = document.querySelector('input[name="' + name + '"]');
    return !!(control && control.checked && !control.disabled);
  });

  /* ── 1.ب كود الخصم: تحقق فوري من الخادم وتحديث الملخص ── */
  const discountInput = document.getElementById('discountCodeInput');
  const discountApplyBtn = document.getElementById('discountApplyBtn');
  const discountClearBtn = document.getElementById('discountClearBtn');
  const discountStatus = document.getElementById('discountStatus');

  function setDiscountStatus(message, state) {
    if (!discountStatus) return;
    discountStatus.hidden = !message;
    discountStatus.textContent = message || '';
    if (state) discountStatus.dataset.state = state; else delete discountStatus.dataset.state;
  }

  function clearDiscount() {
    checkout?.setDiscount?.(null);
    if (discountClearBtn) discountClearBtn.hidden = true;
    setDiscountStatus('', null);
  }

  if (discountInput && discountApplyBtn && checkout) {
    discountApplyBtn.addEventListener('click', () => {
      const code = (discountInput.value || '').trim().toUpperCase();
      if (!code) {
        setDiscountStatus('أدخل كود الخصم أولاً.', 'error');
        return;
      }
      if (!checkout.isOn('include_subscription')) {
        setDiscountStatus('كود الخصم يسري على بند الاشتراك — فعّل بند التجديد أولاً.', 'error');
        return;
      }
      const body = new URLSearchParams();
      body.set('discount_code', code);
      const planInput = form?.querySelector('input[name="plan_id"]:checked');
      if (planInput?.value) body.set('plan_id', planInput.value);
      const capacityInput = form?.querySelector('input[name="teacher_capacity"]');
      if (capacityInput?.value) body.set('teacher_capacity', capacityInput.value);
      const onBehalf = form?.querySelector('input[name="on_behalf_school"]');
      if (onBehalf?.value) body.set('on_behalf_school', onBehalf.value);
      const csrf = form?.querySelector('input[name="csrfmiddlewaretoken"]')?.value || '';

      discountApplyBtn.disabled = true;
      fetch(billingRoot?.dataset.discountUrl || '', {
        method: 'POST',
        headers: {
          'X-CSRFToken': csrf,
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: body.toString(),
        credentials: 'same-origin',
      })
        .then((response) => response.json())
        .then((data) => {
          if (!data || !data.ok) {
            clearDiscount();
            const middlewareMessages = {
              subscription_expired: 'يمكنك استخدام كود الخصم أثناء تجديد الاشتراك. أعد تحميل الصفحة وحاول مرة أخرى.',
              password_change_required: 'غيّر كلمة المرور المؤقتة ثم أعد محاولة تطبيق كود الخصم.',
            };
            const errorMessage = data && (
              data.message || middlewareMessages[data.detail]
            );
            setDiscountStatus(errorMessage || 'تعذّر التحقق من الكود.', 'error');
            return;
          }
          discountInput.value = data.code;
          checkout.setDiscount({
            code: data.code,
            type: data.discount_type,
            value: parseFloat(data.value) || 0,
          });
          if (discountClearBtn) discountClearBtn.hidden = false;
          let message = data.message || 'تم تطبيق كود الخصم.';
          if (checkout.currentTotal?.() === 0) {
            message += ' الطلب أصبح مجانياً بالكامل — أرسل الطلب وسيُفعّل فوراً بلا إيصال.';
          }
          setDiscountStatus(message, 'ok');
        })
        .catch(() => {
          setDiscountStatus('تعذّر الاتصال بالخادم. حاول مرة أخرى.', 'error');
        })
        .finally(() => {
          discountApplyBtn.disabled = false;
        });
    });

    if (discountClearBtn) {
      discountClearBtn.addEventListener('click', () => {
        discountInput.value = '';
        clearDiscount();
      });
    }

    // تعديل الكود بعد قبوله يلغي القبول القديم حتى لا يُعرض خصم كودٍ آخر.
    discountInput.addEventListener('input', () => {
      const applied = checkout.getDiscount?.();
      if (applied && discountInput.value.trim().toUpperCase() !== applied.code) {
        clearDiscount();
      }
    });
  }

  /* ── 2. حلقة الأيام المتبقية ──
     اللون يأتي من `data-tone` لا من قيمة مكتوبة هنا، وإلا احتاج
     الوضع الليلي إصلاحاً ثانياً لنفس الحلقة. */
  const days = Number.parseInt(billingRoot?.dataset.daysRemaining || '0', 10) || 0;
  const totalDays = Math.max(
    Number.parseInt(billingRoot?.dataset.durationDays || '365', 10) || 365,
    1,
  );
  const circle = document.querySelector('.ring-circle-fg');
  if (circle) {
    const r   = circle.r.baseVal.value;
    const circ = 2 * Math.PI * r;
    circle.setAttribute('stroke-dasharray', `${circ} ${circ}`);
    circle.setAttribute('stroke-dashoffset', String(circ));
    circle.dataset.tone = days < 10 ? 'danger' : (days < 30 ? 'warn' : 'ok');

    const percent = Math.min(Math.max(days / totalDays, 0), 1);
    const offset  = circ - percent * circ;
    setTimeout(() => { circle.setAttribute('stroke-dashoffset', String(offset)); }, 400);
  }

  /* ── نسخ رقم الآيبان (مستمع حدث متوافق مع CSP بدل onclick) ── */
  document.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const text = btn.getAttribute('data-copy') || '';
      const done = () => {
        const orig = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-check"></i>';
        setTimeout(() => { btn.innerHTML = orig; }, 2000);
        if (window._showToast) window._showToast('تم نسخ رقم الآيبان بنجاح ✓', 'success');
      };
      const fail = () => { if (window._showToast) window._showToast('تعذّر النسخ، يرجى النسخ يدوياً', 'error'); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(fail);
      } else {
        try {
          const ta = document.createElement('textarea');
          ta.value = text;
          ta.className = 'subscription-copy-fallback';
          document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); done();
        } catch (e) { fail(); }
      }
    });
  });

  /* ── 3. رفع الإيصال والمعاينة ── */
  const fileInput   = document.getElementById('receipt');
  const fileLabel   = document.getElementById('fileLabel');
  const dropArea    = document.getElementById('dropArea');
  const preview     = document.getElementById('uploadPreview');
  const previewThumb= document.getElementById('previewThumb');
  const previewName = document.getElementById('previewName');
  const previewSize = document.getElementById('previewSize');
  const removeBtn   = document.getElementById('removeFile');
  const uploadIcon  = dropArea ? dropArea.querySelector('.upload-icon') : null;

  function preventDefaults(e) { e.preventDefault(); e.stopPropagation(); }
  if (dropArea && fileInput) {
    ['dragenter','dragover','dragleave','drop'].forEach(ev => dropArea.addEventListener(ev, preventDefaults));
    ['dragenter','dragover'].forEach(ev => dropArea.addEventListener(ev, () => dropArea.classList.add('drag-active')));
    ['dragleave','drop'].forEach(ev => dropArea.addEventListener(ev, () => dropArea.classList.remove('drag-active')));

    dropArea.addEventListener('drop', e => {
      const files = e.dataTransfer.files;
      if (files.length) { fileInput.files = files; showPreview(files[0]); }
    });

    fileInput.addEventListener('change', function () {
      if (this.files && this.files[0]) showPreview(this.files[0]);
    });
  }

  function showPreview(file) {
    if (file.size > 10 * 1024 * 1024) {
      showToast('حجم الملف يتجاوز الحد المسموح (10MB)', 'error');
      fileInput.value = '';
      return;
    }
    const reader = new FileReader();
    reader.onload = e => {
      previewThumb.src = e.target.result;
      previewName.textContent = file.name;
      previewSize.textContent = (file.size / 1024).toFixed(1) + ' KB';
      preview.classList.add('visible');
      dropArea.classList.add('is-hidden');
      if (uploadIcon) uploadIcon.className = 'fa-solid fa-circle-check upload-icon';
      fileLabel.textContent = file.name;
    };
    reader.readAsDataURL(file);
  }

  if (removeBtn) {
    removeBtn.addEventListener('click', () => {
      fileInput.value = '';
      preview.classList.remove('visible');
      dropArea.classList.remove('is-hidden');
      if (uploadIcon) uploadIcon.className = 'fa-solid fa-cloud-arrow-up upload-icon';
      fileLabel.textContent = 'يدعم JPG, PNG, WEBP — الحد الأقصى 10MB';
    });
  }

  /* ── 4. التحقق قبل الإرسال ── */
  if (form) {
    form.addEventListener('submit', function (e) {
      const paymentMethod = e.submitter?.dataset.paymentMethod || 'bank_transfer';
      const isElectronic = paymentMethod === 'tamara' || paymentMethod === 'moyasar';
      const anySelected = isOn('include_subscription') || isOn('include_archive_addon') || isOn('include_archive_storage') || isOn('include_archive_space');
      if (!anySelected) {
        e.preventDefault();
        const items = document.querySelector('.purchase-flow');
        if (items) items.scrollIntoView({ behavior: 'smooth', block: 'center' });
        showToast('اختر بندًا واحدًا على الأقل للدفع', 'error');
        return;
      }
      if (isOn('include_subscription') && !document.querySelector('input[name="plan_id"]:checked')) {
        e.preventDefault();
        const plans = document.getElementById('schoolSubscriptionPlans');
        if (plans) plans.scrollIntoView({ behavior: 'smooth', block: 'center' });
        showToast('اختر باقة لتجديد اشتراك المدرسة', 'error');
        return;
      }
      const orderIsFree = (checkout?.currentTotal?.() ?? 1) === 0 && !!checkout?.getDiscount?.();
      if (!isElectronic && !orderIsFree && fileInput && !fileInput.value) {
        e.preventDefault();
        if (dropArea) {
          dropArea.classList.add('drag-active');
          dropArea.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        showToast('يرجى إرفاق صورة الإيصال قبل الإرسال', 'error');
        return;
      }
      const activeSubmit = paymentMethod === 'tamara'
        ? tamaraSubmit
        : paymentMethod === 'moyasar'
          ? moyasarSubmit
          : submitBtn;
      if (activeSubmit) {
        activeSubmit.classList.add('loading');
        const lbl = activeSubmit.querySelector('span');
        const previousLabel = lbl ? lbl.textContent : '';
        if (lbl) lbl.textContent = 'جارٍ الإرسال...';
        activeSubmit.disabled = true;

        // مغادرة الصفحة إلى بوابة مستضافة تستبدلها، فلا تُرى هذه الحالة ثانية.
        // وإن بقينا هنا بعد ثوانٍ فالانتقال لم يحدث — تحويلٌ محجوب أو طلبٌ
        // ساقط — وزرٌّ ميت للأبد يُخفي ذلك عن المستخدم.
        window.setTimeout(function () {
          if (!activeSubmit.classList.contains('loading')) return;
          activeSubmit.classList.remove('loading');
          activeSubmit.disabled = false;
          if (lbl) lbl.textContent = previousLabel;
          showToast('تعذّر فتح صفحة الدفع. حاول مرة أخرى أو اختر وسيلة دفع أخرى.', 'error');
        }, 8000);
      }
    });
  }

  // العودة بزرّ الرجوع تستعيد الصفحة من الكاش كما تُركت — بزرٍّ معطّل
  // ما يزال «يرسل».
  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    [submitBtn, tamaraSubmit, moyasarSubmit].forEach(function (button) {
      if (button) button.classList.remove('loading');
    });
    checkout?.recompute?.();
  });

  /* ── 5. التنبيهات المنبثقة ── */
  function showToast(msg, type = 'success') {
    if (window.showAppToast) window.showAppToast(msg, type);
  }
  window._showToast = showToast; // expose globally

  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initSubscriptionPage, { once: true });
  } else {
    initSubscriptionPage();
  }
})();
