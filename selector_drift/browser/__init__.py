"""The browser driver package. `driver.open(target)` is the only way the product touches a page;
see `driver.py`."""

from selector_drift.browser import allowlist, driver, ratelimit, registry
from selector_drift.browser.allowlist import OffScopeNavigation

__all__ = ["OffScopeNavigation", "allowlist", "driver", "ratelimit", "registry"]
