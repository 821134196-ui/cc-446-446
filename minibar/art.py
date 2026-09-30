"""生成匿名演示用商品占位图（SVG，无外部依赖），输出到 web/static/img。"""
from pathlib import Path

from . import config

# (底色, 强调色, 图形)  —— 与藏青/香槟金主题协调
ART = {
    "water":   ("#DCEAF5", "#5B8DB8", "bottle"),
    "cola":    ("#F3D9D2", "#A84A33", "can"),
    "spark":   ("#E4EFE7", "#6E9C7D", "bottle"),
    "juice":   ("#F8E9C9", "#C98A2B", "carton"),
    "beer":    ("#F2E2B8", "#B0872E", "bottle"),
    "nuts":    ("#EFE0CC", "#8A5A2B", "pouch"),
    "choco":   ("#E7D3C2", "#5E3B24", "bar"),
    "chips":   ("#F5D9A8", "#C77B2A", "bag"),
}

TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120">
<rect width="120" height="120" rx="10" fill="{bg}"/>
<g fill="{fg}">{shape}</g>
<text x="60" y="108" text-anchor="middle" font-family="sans-serif" font-size="9"
 fill="#1F2A44" opacity="0.65">MINIBAR</text>
</svg>
"""

SHAPES = {
    "bottle": '<rect x="54" y="20" width="12" height="14" rx="3"/>'
              '<path d="M50 38 h20 v8 l6 12 v36 a6 6 0 0 1 -6 6 h-20 a6 6 0 0 1 -6 -6 v-36 l6 -12 z"/>',
    "can":    '<rect x="40" y="28" width="40" height="66" rx="6"/>'
              '<rect x="40" y="34" width="40" height="6" opacity="0.35"/>'
              '<rect x="52" y="52" width="16" height="18" rx="2" opacity="0.35"/>',
    "carton": '<path d="M42 34 h36 l6 10 v46 a4 4 0 0 1 -4 4 h-40 a4 4 0 0 1 -4 -4 v-46 z"/>'
              '<rect x="52" y="58" width="16" height="20" rx="2" opacity="0.35"/>',
    "pouch":  '<path d="M44 30 l8 8 h16 l8 -8 v62 a6 6 0 0 1 -6 6 h-20 a6 6 0 0 1 -6 -6 z"/>'
              '<rect x="50" y="56" width="20" height="5" rx="2" opacity="0.35"/>',
    "bar":    '<rect x="36" y="44" width="48" height="34" rx="4"/>'
              '<rect x="42" y="50" width="12" height="10" rx="2" opacity="0.35"/>'
              '<rect x="58" y="50" width="20" height="10" rx="2" opacity="0.35"/>',
    "bag":    '<path d="M40 36 q20 -10 40 0 l4 52 a8 8 0 0 1 -8 8 h-32 a8 8 0 0 1 -8 -8 z"/>'
              '<rect x="52" y="54" width="16" height="14" rx="2" opacity="0.35"/>',
}


def generate(target_dir=None):
    out = Path(target_dir or config.BASE_DIR / "web" / "static" / "img")
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for key, (bg, fg, shape) in ART.items():
        p = out / f"{key}.svg"
        p.write_text(TEMPLATE.format(bg=bg, fg=fg, shape=SHAPES[shape]), encoding="utf-8")
        written.append(p.name)
    return written
