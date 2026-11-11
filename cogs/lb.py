"""Bridge so `cogs.lb` works while the real leaderboard file is named "lb (11).py".

Put this file in cogs/ next to "lb (11).py". bot.py loads "cogs.lb" and cogs/h2h.py
does `from .lb import _totals`; both are served from the real file below.
Delete this file if you later rename "lb (11).py" to "lb.py".
"""

import importlib

_real = importlib.import_module(__package__ + ".lb (11)")

# Re-export everything (including helpers such as _totals / _team_rosters and setup()).
globals().update({name: value for name, value in vars(_real).items()
                  if not (name.startswith("__") and name.endswith("__"))})
