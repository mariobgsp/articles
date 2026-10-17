#!/usr/bin/env python3
"""Render every Mermaid diagram in diagrams/*.mmd to images/*.png.

Mermaid is loaded from a CDN inside headless Chromium, so there is nothing to
install. Each render is trimmed to its content with ImageMagick and padded, so
one command produces consistently framed diagrams for every article.

    python3 tools/render-diagrams.py            # light + dark, all diagrams
    python3 tools/render-diagrams.py routre     # one diagram (by file stem)
    python3 tools/render-diagrams.py --theme light

Output:
    images/<stem>-architecture.png        light (default, used by the articles)
    images/<stem>-architecture-dark.png   dark (portfolio / site use)
"""

import html
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIAGRAMS = ROOT / "diagrams"
IMAGES = ROOT / "images"

# One brand palette, taken from the portfolio theme (assets/theme.css), so the
# diagrams read as the same family as the site in either rendering.
ACCENT_VIOLET = "#7c5cf5"
ACCENT_BLUE = "#3b6fd4"
ACCENT_EMERALD = "#0f9d6e"

THEMES = {
    "light": {
        "suffix": "",
        "background": "#ffffff",
        "ink": "#14141a",
        "node": "#f6f6f9",
        "line": "#8a8aa0",
        "accent": ACCENT_VIOLET,
        "blue": ACCENT_BLUE,
        "emerald": ACCENT_EMERALD,
    },
    "dark": {
        "suffix": "-dark",
        "background": "#0a0a0b",
        "ink": "#f5f5f7",
        "node": "#16161a",
        "line": "#7aa2ff",
        "accent": "#a78bfa",
        "blue": "#7aa2ff",
        "emerald": "#6ee7b7",
    },
}

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>diagram</title>
<style>
  html, body {{ margin: 0; padding: 0; background: {background}; }}
  #wrap {{ padding: 34px 40px; display: inline-block; }}
  .mermaid svg {{ max-width: none !important; }}
  .mermaid {{ font-family: Inter, "Segoe UI", system-ui, sans-serif; }}
</style>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs";
  mermaid.initialize({{
    startOnLoad: true,
    securityLevel: "loose",
    theme: "base",
    fontFamily: "Inter, Segoe UI, system-ui, sans-serif",
    themeVariables: {{
      darkMode: {dark},
      background: "{background}",
      primaryColor: "{node}",
      primaryTextColor: "{ink}",
      primaryBorderColor: "{accent}",
      secondaryColor: "{node}",
      tertiaryColor: "{background}",
      lineColor: "{line}",
      textColor: "{ink}",
      fontSize: "17px",
      clusterBkg: "{background}",
      clusterBorder: "{accent}",
      edgeLabelBackground: "{background}",
      titleColor: "{ink}",
      nodeBorder: "{accent}",
      mainBkg: "{node}",
      cScale0: "{accent}", cScale1: "{blue}", cScale2: "{emerald}",
      cScale3: "{accent}", cScale4: "{blue}", cScale5: "{emerald}",
      cScale6: "{accent}", cScale7: "{blue}", cScale8: "{emerald}",
      pie1: "{accent}", pie2: "{blue}", pie3: "{emerald}"
    }},
    // useMaxWidth: false is what makes the diagram render at its natural size.
    // With the default (true) mermaid writes width="100%", and inside the
    // auto-width #wrap box that resolves to a collapsed, unreadable SVG.
    flowchart: {{ curve: "basis", htmlLabels: true, nodeSpacing: 46, rankSpacing: 62, padding: 12, useMaxWidth: false }},
    sequence: {{ actorMargin: 62, messageMargin: 34, width: 190, wrap: false, useMaxWidth: false }},
    themeCSS: ".label foreignObject div, .nodeLabel, .edgeLabel span {{ line-height: 1.35; }}"
  }});
</script>
</head>
<body>
<div id="wrap"><pre class="mermaid">{source}</pre></div>
</body></html>
"""


def chromium():
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    sys.exit("no chromium binary found on PATH")


def render(name, source, theme_name, cfg, browser, workdir):
    page = workdir / f"{name}-{theme_name}.html"
    raw = workdir / f"{name}-{theme_name}.png"
    page.write_text(
        PAGE.format(
            source=html.escape(source),
            dark="true" if theme_name == "dark" else "false",
            **cfg,
        )
    )

    # A deliberately oversized viewport: the PNG is trimmed to the diagram
    # afterwards, so the window only has to be bigger than the content.
    subprocess.run(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--hide-scrollbars",
            "--force-device-scale-factor=2",
            "--window-size=4200,3200",
            "--virtual-time-budget=20000",
            f"--screenshot={raw}",
            page.as_uri(),
        ],
        check=True,
        capture_output=True,
    )

    out = IMAGES / f"{name}-architecture{cfg['suffix']}.png"
    # -trim cuts the empty canvas, the border puts the padding back.
    subprocess.run(
        [
            "magick",
            str(raw),
            "-trim",
            "+repage",
            "-bordercolor",
            cfg["background"],
            "-border",
            "40",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    themes = [a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--theme=")]
    if not themes:
        themes = list(THEMES)

    IMAGES.mkdir(exist_ok=True)
    browser = chromium()
    files = sorted(DIAGRAMS.glob("*.mmd"))
    if args:
        files = [f for f in files if f.stem in args]
    if not files:
        sys.exit("no diagrams matched")

    with tempfile.TemporaryDirectory() as tmp:
        workdir = pathlib.Path(tmp)
        for path in files:
            source = path.read_text().strip()
            for theme_name in themes:
                cfg = THEMES[theme_name]
                out = render(path.stem, source, theme_name, cfg, browser, workdir)
                rel = out.relative_to(ROOT)
                print(f"rendered {rel}  ({path.relative_to(ROOT)})")


if __name__ == "__main__":
    main()
