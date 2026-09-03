/* static/js/hijri-date.js
   ─────────────────────────────────────────────────────────────────────────
   عرض التاريخ الهجري في المتصفّح — مصدرٌ واحد.

   **لماذا ملفٌ مستقل.** كان كل قالبٍ يحتاج صدى هجرياً يبني مُنسّقه بيده،
   فتكرّر نفس السطر في أربعة قوالب، وفي ثلاثةٍ منها لحقه الخطأ نفسه:

       hijriFmt.format(dt) + " هـ"   →   «٢١ ربيع الأول ١٤٤٨ هـ هـ»

   لأن ‎Intl‎ يُخرج علامة الحقبة ‎(era)‎ ضمن النتيجة أصلاً، فإلحاقها يدوياً
   يضاعفها. ولمّا كان الخطأ واحداً في ثلاثة أماكن، فالعلّة ليست في السطر بل
   في تكراره؛ فالإصلاح أن يكون التنسيق في مكانٍ واحد لا في كل قالب.

   **ولماذا ‎nu-latn‎.** الخادم يعرض ‎«1448/03/21»‎ بأرقامٍ لاتينية عبر
   ``hijri_utils``. فلو أخرج المتصفّح ‎«١٤٤٨»‎ بالأرقام العربية لرأى المدير
   نظامَي ترقيمٍ مختلفين لنفس التاريخ في الشاشة الواحدة.

   الاستعمال:
       TawtheeqHijri.format(dateOrString)   → "21 ربيع الأول 1448 هـ"
       TawtheeqHijri.echo(dateOrString)     → "📅 الموافق 21 ربيع الأول 1448 هـ"
       TawtheeqHijri.supported              → false إن عجز المتصفّح
   ───────────────────────────────────────────────────────────────────────── */
(function (global) {
  'use strict';

  var formatter = null;
  try {
    // أم القرى هو التقويم الرسمي في المملكة، و‎nu-latn‎ يوحّد الأرقام مع الخادم.
    formatter = new Intl.DateTimeFormat('ar-SA-u-ca-islamic-umalqura-nu-latn', {
      day: '2-digit',
      month: 'long',
      year: 'numeric'
    });
  } catch (error) {
    formatter = null;
  }

  /** يقبل Date أو "YYYY-MM-DD" أو أي نصٍّ يفهمه Date، ويعيد Date صالحاً أو null. */
  function toDate(value) {
    if (!value) return null;
    if (value instanceof Date) return isNaN(value.getTime()) ? null : value;
    var text = String(value).trim();
    if (!text) return null;

    // "YYYY-MM-DD" يُبنى بالتوقيت المحلي؛ لو مرّرناه لـ Date مباشرةً لقُرئ UTC
    // فانزاح يوماً كاملاً غربَ غرينتش — وهو ما يجعل التاريخ يسبق المكتوب بيوم.
    var parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
    var parsed = parts
      ? new Date(Number(parts[1]), Number(parts[2]) - 1, Number(parts[3]))
      : new Date(text);

    return isNaN(parsed.getTime()) ? null : parsed;
  }

  /**
   * يعيد التاريخ الهجري نصاً: "21 ربيع الأول 1448 هـ".
   * علامة «هـ» جزءٌ من مخرجات Intl — لا تُلحق يدوياً.
   */
  function format(value) {
    var date = toDate(value);
    if (!date || !formatter) return '';
    try {
      return formatter.format(date);
    } catch (error) {
      return '';
    }
  }

  /** صدى جاهز للعرض تحت حقل تاريخٍ ميلادي. يعيد '' إن تعذّر التحويل. */
  function echo(value, prefix) {
    var text = format(value);
    if (!text) return '';
    return (prefix === undefined ? '📅 الموافق ' : prefix) + text;
  }

  /* ───────────────────────────────────────────────────────────────────────
     التركيب الذاتي.

     المدير يرى «‎1448/03/21 هـ‎» في كل جدول، ثم يُطلب منه أن يُصفّي بتاريخٍ
     ميلادي بلا أي جسر. وصفحة «إضافة تقرير» كانت تبني هذا الجسر بيدها، فبقي
     الجسر حيث كُتب ولم يصل إلى الفلاتر.

     فبدل وصل كل حقلٍ على حدة: كل ‎<input type="date">‎ يحمل ‎data-hijri-echo‎
     يُركَّب له صداه هنا، ويتحدّث مع كل تغيير. والحقل الذي يُضاف غداً يرثه
     بسمةٍ واحدة، لا بنسخِ عشرين سطراً.
     ─────────────────────────────────────────────────────────────────────── */
  var ECHO_CLASS = 'hijri-echo';

  function wire(input) {
    if (!input || input.dataset.hijriWired === '1') return;
    input.dataset.hijriWired = '1';

    var target = null;
    var explicit = input.getAttribute('data-hijri-echo');
    if (explicit) target = document.getElementById(explicit);

    if (!target) {
      target = document.createElement('small');
      target.className = ECHO_CLASS;
      // ‎polite‎ لا ‎assertive‎: التاريخ صدىً مساعد، لا تنبيهٌ يقطع القراءة.
      target.setAttribute('aria-live', 'polite');
      input.insertAdjacentElement('afterend', target);
    }

    var paint = function () {
      var text = format(input.value);
      target.textContent = text ? 'الموافق ' + text : '';
      target.hidden = !text;
    };

    input.addEventListener('input', paint);
    input.addEventListener('change', paint);
    paint();
  }

  function wireAll(root) {
    (root || document)
      .querySelectorAll('input[type="date"][data-hijri-echo]')
      .forEach(wire);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { wireAll(); });
  } else {
    wireAll();
  }

  global.TawtheeqHijri = {
    supported: formatter !== null,
    toDate: toDate,
    format: format,
    echo: echo,
    wire: wire,
    wireAll: wireAll
  };
})(window);
