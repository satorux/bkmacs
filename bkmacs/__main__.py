"""Entry point, reachable three ways.

``python3 -m bkmacs`` from the project directory, ``python3 path/to/bkmacs``
from anywhere at all, and an alias to either.  The second one runs this file
with no package around it, which is why the import has to be arranged by hand.
"""

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from bkmacs import main
else:
    from . import main

sys.exit(main())
