"""Kept for compatibility: builds nba.html via assemble.py."""
import sys, assemble
assemble.build("nba", sys.argv[1] if len(sys.argv) > 1 else None)
