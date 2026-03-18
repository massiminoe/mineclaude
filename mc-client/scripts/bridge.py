"""Minescript entry point — invoke as \\bridge in game."""
import sys
import os

# Bridge package is copied to /headlessmc/bridge/
sys.path.insert(0, "/headlessmc")

from bridge.server import main
main()
