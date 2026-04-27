"""Minescript entry point — invoke as \\bridge in game."""
# A1 framing: import minescript_runtime FIRST so it can redirect sys.stdout
# to sys.stderr (dedicating the framed pipe to RPC) before any other module
# loads and potentially prints. If anything reaches stdout before this, the
# bytes corrupt the length-prefixed wire and the next read on the Java side
# tries to interpret printable ASCII as a 32-bit length.
import minescript_runtime  # noqa: F401

import sys
import os

# Bridge package is copied to /headlessmc/bridge/
sys.path.insert(0, "/headlessmc")

from bridge.server import main
main()
