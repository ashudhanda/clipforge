"""Single source of truth for the ClipForge version.

Bumped by hand on each release. The PyInstaller spec, the Inno Setup
installer (via /DAppVersion from CI), and the dashboard all read this —
never hardcode the version anywhere else.
"""

__version__ = "0.1.6"
