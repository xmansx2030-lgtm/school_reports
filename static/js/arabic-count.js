/* static/js/arabic-count.js
   ─────────────────────────────────────────────────────────────────────────
   تمييز العدد في العربية — نظير ‎reports/templatetags/arabic_tags.py‎.

   **لماذا نسختان.** الخادم يرسم اللوحة أول مرة، ثم يُحدّث ‎JavaScript‎ الأرقام
   عند تغيير الفترة بلا إعادة تحميل. فلو عرف الخادمُ القاعدة وحده لعادت
   «3 عنصر» بمجرّد أول تحديثٍ حيّ — أي أن الإصلاح يصمد حتى أول نقرة.

   القاعدتان مشتقّتان من ‎CLDR‎، والحدود مكتوبة هنا كما هي هناك حرفاً بحرف:

       0            → جمع            لا عناصر
       1            → مفرد           عنصر واحد
       2            → مثنّى           عنصران
       n%100 = 3–10 → جمع            5 عناصر · 103 عناصر
       n%100 = 11–99→ مفرد منصوب     15 عنصراً · 111 عنصراً
       غير ذلك       → مفرد           100 عنصر · 101 عنصر

   الاستعمال:
       TawtheeqArabic.word(3, 'عنصر,عنصران,عناصر,عنصراً')   → "عناصر"
       TawtheeqArabic.count(3, 'عنصر,عنصران,عناصر,عنصراً')  → "3 عناصر"

   الصيغ الأربع بالترتيب: مفرد، مثنّى، جمع، منصوب. وما نقص يُشتقّ بأقربها.
   ───────────────────────────────────────────────────────────────────────── */
(function (global) {
  'use strict';

  function forms(spec) {
    var parts = String(spec || '')
      .split(',')
      .map(function (part) { return part.trim(); })
      .filter(Boolean);

    if (!parts.length) return ['', '', '', ''];
    var singular = parts[0];
    return [
      singular,
      parts.length > 1 ? parts[1] : singular + 'ان',
      parts.length > 2 ? parts[2] : singular,
      parts.length > 3 ? parts[3] : singular
    ];
  }

  function toInt(value) {
    if (value === null || value === undefined) return null;
    // الأرقام قد تصل من الخادم منسّقةً بفواصل ألفية.
    var text = String(value).trim().replace(/[,٬،]/g, '');
    if (!/^-?\d+$/.test(text)) return null;
    return parseInt(text, 10);
  }

  /** صيغة المعدود وحدها، بلا العدد. */
  function word(value, spec) {
    var shapes = forms(spec);
    var singular = shapes[0], dual = shapes[1], plural = shapes[2], accusative = shapes[3];

    var count = toInt(value);
    if (count === null) return plural || singular;

    count = Math.abs(count);
    var remainder = count % 100;

    if (count === 0) return plural || singular;
    if (count === 1) return singular;
    if (count === 2) return dual;
    if (remainder >= 3 && remainder <= 10) return plural;
    if (remainder >= 11 && remainder <= 99) return accusative;
    return singular;
  }

  /** العدد ومعدوده معاً: "لا عناصر" · "عنصر واحد" · "عنصران" · "5 عناصر". */
  function count(value, spec) {
    var shapes = forms(spec);
    var singular = shapes[0], dual = shapes[1], plural = shapes[2];

    var number = toInt(value);
    if (number === null) return String(value) + ' ' + (plural || singular);

    var text = word(number, spec);
    if (number === 0) return 'لا ' + text;
    if (number === 1) return singular + ' واحد';
    if (number === 2) return dual;
    return number + ' ' + text;
  }

  /**
   * يحدّث عنصراً يحمل ‎data-count-forms‎ بصيغةٍ تطابق عدده الجديد.
   * يُبقي القاعدة في مكانٍ واحد بدل نثرها في كل مُحدِّث.
   *
   * ‎data-count-mode="full"‎ يكتب العدد ومعدوده معاً. وهو المطلوب حيثما كان
   * المفرد والمثنّى يحملان عددهما في صيغتهما: «طلبان» لا «2 طلبان» —
   * والعربية لا تُتبع المثنّى برقمه.
   */
  function paint(element, value) {
    if (!element) return;
    var spec = element.getAttribute('data-count-forms');
    if (!spec) return;
    var full = element.getAttribute('data-count-mode') === 'full';
    element.textContent = full ? count(value, spec) : word(value, spec);
  }

  global.TawtheeqArabic = {
    word: word,
    count: count,
    paint: paint
  };
})(window);
