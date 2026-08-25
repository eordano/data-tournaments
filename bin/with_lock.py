#!/usr/bin/env python3
"""Acquire an exclusive fcntl lock on arg1, then exec arg2+."""
import fcntl, os, sys
f = open(sys.argv[1], "a+")
fcntl.flock(f.fileno(), fcntl.LOCK_EX)
os.execvp(sys.argv[2], sys.argv[2:])
