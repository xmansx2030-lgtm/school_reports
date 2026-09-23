# reports/views/__init__.py
"""
Re-export every public view so that ``from reports import views``
followed by ``views.some_view`` keeps working exactly as before.
"""

from ._helpers import *          # noqa: F401,F403  – shared helpers + user_guide views
from .auth import *              # noqa: F401,F403
from .platform import *          # noqa: F401,F403
from .platform_executives import *  # noqa: F401,F403
from .home import *              # noqa: F401,F403
from .reports import *           # noqa: F401,F403
from .achievements import *      # noqa: F401,F403
from .leadership import *        # noqa: F401,F403
from .teachers import *          # noqa: F401,F403
from .tickets import *           # noqa: F401,F403
from .schools import *           # noqa: F401,F403
from .school_groups import *     # noqa: F401,F403
from .group_notifications import *  # noqa: F401,F403
from .notifications import *     # noqa: F401,F403
from .subscriptions import *     # noqa: F401,F403
from .reporttypes import *       # noqa: F401,F403
from .exports import *           # noqa: F401,F403
from .api import *               # noqa: F401,F403
from .mansour import *           # noqa: F401,F403
from .onboarding import *       # noqa: F401,F403
from .school_additions import * # noqa: F401,F403
from .legal import *            # noqa: F401,F403
from .customer_care import *    # noqa: F401,F403
from .activity import *        # noqa: F401,F403
from .lab import *            # noqa: F401,F403
from .staff_dashboard import * # noqa: F401,F403
from .staff_roles import *     # noqa: F401,F403
from .approvals import *       # noqa: F401,F403
from .assignments import *     # noqa: F401,F403
from .group_assignments import *  # noqa: F401,F403
from .meetings import *        # noqa: F401,F403
from .group_meetings import *  # noqa: F401,F403
from .plans import *           # noqa: F401,F403
from .documents import *       # noqa: F401,F403
from .circular_drafts import * # noqa: F401,F403
from .group_oversight import * # noqa: F401,F403
from .api_keys import *       # noqa: F401,F403
from .totp import *           # noqa: F401,F403
from .data_rights import *    # noqa: F401,F403
from .search import *         # noqa: F401,F403
from .web_push import *       # noqa: F401,F403
from .operations import *     # noqa: F401,F403
from .platform_email import * # noqa: F401,F403
from .conversions import *    # noqa: F401,F403
