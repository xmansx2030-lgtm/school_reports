from pathlib import Path


APP = Path("/app")


def insert_after(path: Path, anchor: str, addition: str, guard: str) -> None:
    text = path.read_text(encoding="utf-8")
    if guard in text:
        return
    if anchor not in text:
        raise RuntimeError(f"Overlay anchor was not found in {path}")
    path.write_text(text.replace(anchor, anchor + addition, 1), encoding="utf-8")


def insert_before(path: Path, anchor: str, addition: str, guard: str) -> None:
    text = path.read_text(encoding="utf-8")
    if guard in text:
        return
    if anchor not in text:
        raise RuntimeError(f"Overlay anchor was not found in {path}")
    path.write_text(text.replace(anchor, addition + anchor, 1), encoding="utf-8")


settings_block = """

# ----------------- Mansour public AI assistant -----------------
# The secret is server-side only. The widget remains visible without it and
# reports a safe temporary-unavailable message until production is configured.
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
MANSOUR_ASSISTANT_ENABLED = _env_bool(
    "MANSOUR_ASSISTANT_ENABLED",
    bool(OPENAI_API_KEY),
)
MANSOUR_ASSISTANT_MODEL = (
    os.getenv("MANSOUR_ASSISTANT_MODEL") or "gpt-5.6-luna"
).strip()

try:
    MANSOUR_ASSISTANT_MAX_OUTPUT_TOKENS = max(
        100,
        min(900, int(os.getenv("MANSOUR_ASSISTANT_MAX_OUTPUT_TOKENS", "700"))),
    )
except (TypeError, ValueError):
    MANSOUR_ASSISTANT_MAX_OUTPUT_TOKENS = 700

try:
    MANSOUR_ASSISTANT_TIMEOUT_SECONDS = max(
        5.0,
        min(30.0, float(os.getenv("MANSOUR_ASSISTANT_TIMEOUT_SECONDS", "20"))),
    )
except (TypeError, ValueError):
    MANSOUR_ASSISTANT_TIMEOUT_SECONDS = 20.0
"""

insert_after(
    APP / "config/settings.py",
    'SECURITY_CONTACT_EMAIL = (\n'
    '    os.getenv("SECURITY_CONTACT_EMAIL") or "support@tawtheeq-ksa.com"\n'
    ").strip()\n",
    settings_block,
    "MANSOUR_ASSISTANT_ENABLED =",
)
insert_after(
    APP / "reports/urls.py",
    '    path("", views.platform_landing, name="landing"),\n',
    '    path("assistant/mansour/", views.mansour_assistant_reply, name="mansour_assistant_reply"),\n',
    'name="mansour_assistant_reply"',
)
insert_after(
    APP / "reports/views/__init__.py",
    "from .api import *               # noqa: F401,F403\n",
    "from .mansour import *           # noqa: F401,F403\n",
    "from .mansour import *",
)
insert_after(
    APP / "reports/templates/reports/landing.html",
    "  <link rel=\"stylesheet\" href=\"{% static 'css/landing.css' %}\">\n",
    "  <link rel=\"stylesheet\" href=\"{% static 'css/mansour-assistant.css' %}?v=20260731.2\">\n",
    "css/mansour-assistant.css",
)
insert_before(
    APP / "reports/templates/reports/landing.html",
    '  <div class="sbc-verify-seal"',
    '  {% include "reports/partials/mansour_assistant.html" %}\n\n',
    'partials/mansour_assistant.html',
)
insert_after(
    APP / "reports/templates/reports/landing.html",
    "  <script src=\"{% static 'js/landing.js' %}\" defer></script>\n",
    "  <script src=\"{% static 'js/mansour-assistant.js' %}?v=20260731.2\" defer></script>\n",
    "js/mansour-assistant.js",
)
