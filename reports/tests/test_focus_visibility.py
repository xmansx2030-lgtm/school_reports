# -*- coding: utf-8 -*-
"""مَن يتنقّل بلوحة المفاتيح يجب أن يرى أين هو.

المنصة يقودها موظفون إداريون ووكلاء ومديرو مدارس ساعاتٍ يومياً، وكثيرٌ من
عملهم إدخالُ نماذج وتنقّلٌ بين حقول. وكان ``:focus-visible`` معرَّفاً في عشرين
موضعاً فقط عبر 489KB من CSS — أي أن معظم الشاشات بلا حلقة تركيز، ومَن يعتمد
``Tab`` يعمل أعمى.

والحلقة ليست تفصيلاً تجميلياً: هي الشرط 2.4.7 في WCAG، ومن دونها لا يمكن
استعمال النظام بلوحة المفاتيح أصلاً.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


def contrast_ratio(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class FocusVisibilityTests(SimpleTestCase):
    @property
    def root(self) -> Path:
        return Path(settings.BASE_DIR)

    def _css(self, name: str) -> str:
        return (self.root / "static" / "css" / name).read_text(encoding="utf-8")

    def _theme_token(self, css: str, selector: str, token: str) -> str:
        block = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\n\}}", css, re.DOTALL)
        self.assertIsNotNone(block, f"تعذر العثور على كتلة الثيم: {selector}")
        value = re.search(
            rf"{re.escape(token)}:\s*(#[0-9a-fA-F]{{6}})",
            block.group("body"),
        )
        self.assertIsNotNone(value, f"{token} يجب أن يملك قيمة لون مركزية في {selector}")
        return value.group(1)

    def test_a_global_focus_ring_covers_every_interactive_element(self):
        css = self._css("utilities.css")

        self.assertIn(":focus-visible", css)
        for element in ("a", "button", "input", "select", "textarea", "summary", "[tabindex]"):
            self.assertIn(element, css)
        # ``outline`` لا ``box-shadow``: الأولى وحدها تصمد في وضع التباين العالي.
        self.assertRegex(css, r":focus-visible\s*\{[^}]*outline:\s*3px solid")

    def test_the_ring_is_suppressed_for_pointer_focus_only(self):
        """نقرةُ الفأرة لا ترسم حلقة، ولوحةُ المفاتيح ترسمها.

        الخلط بينهما هو ما دفع كثيرين تاريخياً إلى ``outline: none`` المطلق —
        فأتلفوا الوصولية كلها هرباً من حلقةٍ تظهر عند كل نقرة.
        """
        css = self._css("utilities.css")

        self.assertIn(":focus:not(:focus-visible)", css)

        # كل قاعدة تُطفئ الحلقة يجب أن تكون مشروطة بأن التركيز جاء من الفأرة.
        # الإطفاء غير المشروط هو العطل التاريخي الذي أتلف وصولية مواقع كثيرة.
        for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
            if re.search(r"outline:\s*none", body):
                self.assertIn(
                    ":focus:not(:focus-visible)",
                    selector,
                    f"إطفاءٌ غير مشروط لحلقة التركيز في: {selector.strip()[:80]}",
                )

    def test_the_ring_colour_is_a_token_in_both_themes(self):
        """لونٌ ثابت في الوضعين يعني حلقةً تختفي في أحدهما."""
        alias = "--focus-ring: var(--twq-focus)"
        self.assertIn(alias, self._css("tokens.css"))
        self.assertIn(alias, self._css("dark-mode.css"))

    def test_the_ring_is_visible_against_both_backgrounds(self):
        """3:1 هو حدّ WCAG لمكوّنات الواجهة غير النصّية."""
        tokens = self._css("tokens.css")
        light_focus = self._theme_token(tokens, ":root", "--twq-focus")
        light_background = self._theme_token(tokens, ":root", "--twq-bg")
        dark_focus = self._theme_token(tokens, 'html[data-theme="dark"]', "--twq-focus")
        dark_background = self._theme_token(tokens, 'html[data-theme="dark"]', "--twq-bg")

        self.assertGreaterEqual(contrast_ratio(light_focus, light_background), 3.0)
        self.assertGreaterEqual(contrast_ratio(dark_focus, dark_background), 3.0)
