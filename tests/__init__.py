"""The plugin's tests.

Nothing here may reach the real home folder: the plugin keeps its settings,
its record of what is installed in Resolve, and the effects themselves below
it. So before any of the plugin is imported, home is pointed at an empty
folder made for this run. A test that forgets to redirect a path then writes
there, not into someone's DaVinci Resolve.
"""

import atexit
import os
import shutil
import tempfile

REAL_HOME = os.path.expanduser("~")
HOME = tempfile.mkdtemp(prefix="kaitoi-tests-home-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME  # where Windows keeps it
os.environ["APPDATA"] = os.path.join(HOME, "AppData")
# The shared key store can be moved by this variable, wherever home is: a
# shell that sets it would otherwise send a test's writes to the real one.
os.environ["KAITOI_HOME"] = os.path.join(HOME, ".kaitoi")
atexit.register(shutil.rmtree, HOME, True)
