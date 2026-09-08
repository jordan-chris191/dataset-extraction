"""
config.py
---------
Compatibility shim. All pipeline parameters now live in gee_config.py; this
module re-exports them so existing imports (`import config`) keep working.
New code should `import gee_config` / `from gee_config import ...` directly.
"""

from gee_config import *  # noqa: F401,F403  (re-exports every config constant)
from gee_config import ensure_ready, validate