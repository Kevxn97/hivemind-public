"""HiveMind: local, source-labelled knowledge for agent continuity."""
import sys

# The installation is code/resources only. All mutable state belongs in the vault.
sys.dont_write_bytecode = True
from .hive_version import VERSION

__version__ = VERSION
