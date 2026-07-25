#!/usr/bin/env python3
"""banner.py — the Wiggum startup splash: a Ralph Wiggum ASCII portrait (density
art, Mr-Burns style) + the title, colored from the Springfield palette matching the
terminal background (Night for dark, Day for light).

Usage:
  banner.py            # auto-detect bg, print colored splash to stdout
  banner.py --plain    # no color (for logs / non-TTY)
  banner.py --bg dark|light   # force theme

Background detection order (mirrors orchestrator.sh _detect_bg):
  WIGGUM_BANNER_BG env  ->  COLORFGBG env  ->  OSC 11 query of the terminal  ->  dark
"""
import os
import re
import sys
import select

# ── Ralph Wiggum portrait (density ASCII generated from the show still) ──────────
RALPH = r"""
                       .   ,,,.......
                  .-:;;:-:=====!;;;;~:~~, ..
               ..:!!!==*+!*!=+N!=N;+*;z!==~~~-.
            .,,=+!!*=!z*!z!=zM*=Nz:zM==N*=z!;!;:-.
          .,.~+*=++=+N!!N+;NNN:zN+;NNN:+M*;N+;+!=!~,.
         ,..=N!=Nz;+N!!NM;+NNz*NMz*NNM!*NM*!N+!N**z;-,.
        ,..!M=!NN:+N+;NNN+zNNz++++zNNNNNNNNNNz++zzzz;..,
       ,. =M!;NM=!NN*+NNNNN!*zMMMNz!*NNNNNN**zNMMz*!z-
      ., ~NN:zNz:zNNNNNNNN!+@$#@@@@$**NNNM=z@@@MN$$+!=.
         :z+*NNz*NNNNNNNNN;#@=,z@$@@M=MNNM=z@@$!;$@N==.
       .;*+**zNNNNNNNNNNNN+!M##$@@$M!zNzzzz!+M#$$#N!+;
       ~M*=+NNNNNNNNNNNNNNNz**zzz+**zMz++**!=****+zzz-
       ,!z*zMNNNNNNNNNNNNNNNNNzzzN**z++NMMMN;!MNNNNNz=,
        .-;=*+NNNNNNNNNNNNNNNNNNN+:=*+*:!+*!;+NNNNNNNz*~.
            -*NNNNNNNNNNNNNNNNMz=!zNMz!=!**+zNNNNNNNNNzz-
             ,=zNNNNNNNNNNN:*N+;zMNN*~=!!!+NNMNNMMNNz+!:.
             .~==+NMNNNNNN*!**~+MNNN+*zNz=~=!***!=;~-.
            .;MN+!=!*zNNNNNMMM;+NNNNNMN**+z*~!+*~
            .*MNNNNz*!!!!*+z**~*NNMNNNNNNNzz;!!~,
          .-::;+NNNNNNNzzz+:=z*!!!*zNNNNN!~;!;!*!:,
        .=zNNNz*!*zNNNNNNz;*MNNNNz+!=*NNN=;zz!:=:!*-
        -NNNNNNNN+*!*+zz!;+MNNNNNNNNz!;*+:*z+=:!!;*;,.
        -zNNNNNNNNNNz+;;*NNNNNNNNNNNNN+-:;===+z++*;~;;.
""".strip("\n").split("\n")

TITLE   = "The Autonomous Ralph Wiggun Loop"
CAPTION = "ME FAIL SPEC?  THAT IS UNPOSSIBLE."   # Ralph-voice nod to the Burns caption

# ── Springfield palette (truecolor) ─────────────────────────────────────────────
# (r,g,b) per role, per theme. Face=Ralph yellow, title=Marge blue, accent=red.
THEMES = {
    "dark":  {"face": (255, 213, 33), "title": (75, 176, 236), "accent": (255, 90, 77)},
    "light": {"face": (217, 148, 0),  "title": (15, 125, 194), "accent": (208, 36, 31)},
}


def _rgb(r, g, b):
    return "\033[38;2;%d;%d;%dm" % (r, g, b)


def detect_bg():
    v = os.environ.get("WIGGUM_BANNER_BG", "").lower()
    if v in ("dark", "light"):
        return v
    fgbg = os.environ.get("COLORFGBG", "")
    if fgbg:
        m = fgbg.split(";")[-1]
        if m.isdigit():
            return "light" if int(m) >= 11 else "dark"
    # OSC 11 query — bounded, never hangs; skipped when no real tty.
    try:
        if sys.stdout.isatty():
            fd = os.open("/dev/tty", os.O_RDWR)
            try:
                import termios, tty
                old = termios.tcgetattr(fd)
                try:
                    tty.setraw(fd)
                    os.write(fd, b"\033]11;?\033\\")
                    resp = b""
                    while select.select([fd], [], [], 0.2)[0]:
                        resp += os.read(fd, 64)
                        if b"\\" in resp or b"\a" in resp:
                            break
                finally:
                    termios.tcsetattr(fd, termios.TCSANOW, old)
                m = re.search(rb"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", resp)
                if m:
                    r, g, b = (int(m.group(i)[:2], 16) for i in (1, 2, 3))
                    lum = (r * 299 + g * 587 + b * 114) // 1000
                    return "light" if lum >= 128 else "dark"
            finally:
                os.close(fd)
    except Exception:
        pass
    return "dark"


def render(plain=False, theme=None):
    if theme is None:
        theme = detect_bg()
    pal = THEMES.get(theme, THEMES["dark"])
    if plain:
        FACE = TITLE_C = ACCENT = RC = B = ""
    else:
        FACE = _rgb(*pal["face"]); TITLE_C = _rgb(*pal["title"])
        ACCENT = _rgb(*pal["accent"]); RC = "\033[0m"; B = "\033[1m"
    out = ["", B + TITLE_C + "  " + TITLE + RC, ""]
    for ln in RALPH:
        out.append(B + FACE + ln + RC)
    out.append("")
    out.append(B + ACCENT + "  " + CAPTION + RC)
    out.append("")
    return "\n".join(out)


def main(argv):
    plain = "--plain" in argv
    theme = None
    if "--bg" in argv:
        i = argv.index("--bg")
        if i + 1 < len(argv):
            theme = argv[i + 1]
    if not plain and not sys.stdout.isatty():
        plain = True
    sys.stdout.write(render(plain=plain, theme=theme) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
