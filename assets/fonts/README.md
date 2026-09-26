# Bundled caption fonts (OFL-licensed, free to redistribute)

- `NotoSans-Variable.ttf` — Noto Sans variable font (wght axis), SIL Open
  Font License 1.1. Source: https://github.com/google/fonts (ofl/notosans).
  Family name: "Noto Sans" — matches the caption styles in core/edit/captions.py.
- `DejaVuSansMono.ttf` — DejaVu Sans Mono (Bitstream Vera license), for the
  "typewriter" caption style. Source: system DejaVu package.

These ship inside the installer (PyInstaller `datas`) so captions render
identically on machines without the fonts installed. ffmpeg's `ass` filter
is pointed at this directory via `fontsdir` (see `ass_filter()`).
