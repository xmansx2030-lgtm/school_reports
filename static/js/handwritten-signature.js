(function () {
  'use strict';

  window.HandwrittenSignature = {
    init: function (form, onChange) {
      const root = form && form.querySelector('[data-handwritten-signature]');
      if (!root) return null;
      const canvas = root.querySelector('[data-signature-canvas]');
      const clear = root.querySelector('[data-signature-clear]');
      const hidden = root.querySelector('[data-signature-data]');
      const message = root.querySelector('[data-signature-message]');
      const ctx = canvas.getContext('2d', { willReadFrequently: false });
      if (!ctx) return null;
      let activePointer = null;
      let last = null;
      let pathLength = 0;
      let minX = canvas.width;
      let minY = canvas.height;
      let maxX = 0;
      let maxY = 0;

      ctx.strokeStyle = '#07432c';
      ctx.lineWidth = 5;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';

      function position(event) {
        const rect = canvas.getBoundingClientRect();
        return {
          x: Math.max(0, Math.min(canvas.width, (event.clientX - rect.left) * canvas.width / rect.width)),
          y: Math.max(0, Math.min(canvas.height, (event.clientY - rect.top) * canvas.height / rect.height))
        };
      }
      function include(point) {
        minX = Math.min(minX, point.x);
        minY = Math.min(minY, point.y);
        maxX = Math.max(maxX, point.x);
        maxY = Math.max(maxY, point.y);
      }
      function valid() {
        return pathLength >= 40 && maxX - minX >= 35 && maxY - minY >= 8;
      }
      function update() {
        root.classList.toggle('hs-field--signed', valid());
        message.textContent = valid()
          ? 'التوقيع جاهز. راجع نص الإقرار ثم اعتمده.'
          : 'ارسم توقيعًا واضحًا في المساحة المخصصة.';
        if (typeof onChange === 'function') onChange();
      }
      canvas.addEventListener('pointerdown', function (event) {
        if (activePointer !== null) return;
        event.preventDefault();
        activePointer = event.pointerId;
        canvas.setPointerCapture(event.pointerId);
        last = position(event);
        include(last);
        root.classList.add('hs-field--drawing');
      });
      canvas.addEventListener('pointermove', function (event) {
        if (event.pointerId !== activePointer || !last) return;
        event.preventDefault();
        const next = position(event);
        ctx.beginPath();
        ctx.moveTo(last.x, last.y);
        ctx.lineTo(next.x, next.y);
        ctx.stroke();
        pathLength += Math.hypot(next.x - last.x, next.y - last.y);
        include(next);
        last = next;
        hidden.value = '';
        update();
      });
      function finish(event) {
        if (event.pointerId !== activePointer) return;
        activePointer = null;
        last = null;
        root.classList.remove('hs-field--drawing');
        update();
      }
      canvas.addEventListener('pointerup', finish);
      canvas.addEventListener('pointercancel', finish);
      clear.addEventListener('click', function () {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        pathLength = 0;
        minX = canvas.width;
        minY = canvas.height;
        maxX = 0;
        maxY = 0;
        hidden.value = '';
        update();
      });
      update();
      return {
        valid: valid,
        prepare: function () {
          if (!valid()) return false;
          hidden.value = canvas.toDataURL('image/png');
          return true;
        }
      };
    }
  };
})();
