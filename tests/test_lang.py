from sly import Lexer, Parser
import os
import shutil
import subprocess
import ast
import sys
try:
    import winreg
except Exception:
    winreg = None
import uuid
import threading
import time
import urllib.request
import json
import re
import atexit

# Terminal color setup: set a "midnight" background for the session and
# a neutral foreground. Then print ASCII art in navy blue. We use 24-bit
# ANSI color sequences; terminals without support will ignore them.
# Midnight background: RGB(0,25,51). Navy ASCII: RGB(0,0,128). Default
# foreground used after art: light gray RGB(211,215,219).
_BG_SEQ = '\033[48;2;0;25;51m'
_FG_DEFAULT = '\033[38;2;211;215;219m'
_FG_NAVY = '\033[38;2;0;0;128m'
