"""Draw the simple architecture diagram (docs/images/architecture.png).
    python scripts/dev/draw_architecture.py"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).resolve().parents[2] / "docs" / "images" / "architecture.png"
fig, ax = plt.subplots(figsize=(16, 10.5), dpi=150)
ax.set_xlim(0, 160)
ax.set_ylim(0, 105)
ax.axis("off")

INK = "#1d2330"
BOXES = {}


def box(key, x, y, w, h, title, sub="", fill="#ffffff", text=INK, round_=True, edge="#5b6475"):
    style = "round,pad=0,rounding_size=4.5" if round_ else "round,pad=0,rounding_size=0.8"
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=style, fc=fill, ec=edge, lw=1.6))
    if sub:
        ax.text(x + w / 2, y + h * 0.63, title, ha="center", va="center", fontsize=12.5, color=text,
                fontweight="bold")
        ax.text(x + w / 2, y + h * 0.3, sub, ha="center", va="center", fontsize=9.5, color=text, linespacing=1.3)
    else:
        ax.text(x + w / 2, y + h / 2, title, ha="center", va="center", fontsize=12.5, color=text, fontweight="bold",
                linespacing=1.3)
    BOXES[key] = (x, y, w, h)


def anchor(key, side):
    x, y, w, h = BOXES[key]
    return {"l": (x, y + h / 2), "r": (x + w, y + h / 2), "t": (x + w / 2, y + h), "b": (x + w / 2, y),
            "tl": (x + w * 0.3, y + h), "tr": (x + w * 0.7, y + h), "bl": (x + w * 0.3, y),
            "br": (x + w * 0.7, y)}[side]


def arrow(a, sa, b, sb, label="", rad=0.0, dashed=False, lx=0.0, ly=0.0, both=False):
    p1, p2 = anchor(a, sa), anchor(b, sb)
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle="<|-|>" if both else "-|>", mutation_scale=16, lw=1.5,
                                 color="#3d4a5c", linestyle="--" if dashed else "-",
                                 connectionstyle=f"arc3,rad={rad}", shrinkA=2, shrinkB=2))
    if label:
        mx, my = (p1[0] + p2[0]) / 2 + lx, (p1[1] + p2[1]) / 2 + ly
        ax.text(mx, my, label, ha="center", va="center", fontsize=9.5, color="#3d4a5c",
                bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="none"))


ax.text(80, 101, "Architecture: Motor Insurance Claim Triage", ha="center", va="center", fontsize=20,
        fontweight="bold", family="serif", color=INK)

# --- row 1: intake
box("claim", 3, 80, 20, 11, "Claim\n(FNOL)")
box("api", 33, 79, 26, 13, "API", "form checks, trick-message\nblock, rate limit", fill="#fff4c2", round_=False)
box("queue", 70, 80, 20, 11, "Queue\n(Redis)", fill="#7a808a", text="white", round_=False)
box("triage", 101, 78, 28, 15, "Triage Agent", "claim type, key facts\n(dates, delays, estimate)",
    fill="#c8ecc0", round_=False)
box("quarantine", 33, 60, 26, 10, "Rejected &\nset aside")

# --- row 2: research
box("policy", 3, 36, 22, 14, "Policy\nDocuments", "wording + brochure", fill="#2e6b2e", text="white")
box("retriever", 34, 37, 24, 12, "Retriever", "searches 359\npolicy sections", fill="#cfe2ff", round_=False)
box("coverage", 70, 35, 30, 16, "Coverage Agent", "chooses its own searches,\nfinds rules + exceptions",
    fill="#e6c8f5", round_=False)
box("gateway", 116, 52, 26, 12, "AI Gateway", "time limits, retries,\ncircuit breaker, cost",
    fill="#ffd9b8", round_=False)
box("model", 147 - 12, 30, 22, 11, "AI model\n(Groq / stand-in)", fill="#f4f6f9")

# --- row 3: decision
box("check", 70, 13, 30, 12, "Self-check", "every quote must be\nreally in the policy", fill="#fff4c2",
    round_=False)
box("team", 112, 13, 28, 12, "Choose the team", "fast-track / surveyor /\ninvestigation", fill="#c8ecc0",
    round_=False)
box("human", 34, 13, 24, 12, "Human review", "if unsure or AI down")
box("answer", 112, 0.5, 28, 9, "Recommendation\n+ policy quotes")

# --- arrows
arrow("claim", "r", "api", "l", "submit", ly=2.2)
arrow("api", "b", "quarantine", "t", "bad / trick form", lx=10)
arrow("api", "r", "queue", "l", "valid", ly=2.2)
arrow("queue", "r", "triage", "l", "", ly=2.2)
arrow("triage", "b", "coverage", "tr", "hand-off", lx=-3, ly=4)
arrow("policy", "r", "retriever", "l", "indexed", ly=2.2)
arrow("coverage", "l", "retriever", "r", "search", both=True, ly=2.2)
arrow("coverage", "r", "gateway", "bl", "AI calls", both=True, lx=0, ly=2.5)
arrow("triage", "br", "gateway", "t", "AI call", both=True, lx=7)
arrow("gateway", "b", "model", "t", "", both=True)
arrow("coverage", "b", "check", "t", "draft answer", lx=9)
arrow("check", "r", "team", "l", "passed", ly=2.2)
arrow("check", "l", "human", "r", "still failing", ly=2.2)
arrow("check", "tl", "coverage", "bl", "failed:\nsearch again", rad=-0.6, lx=-12, ly=0)
arrow("team", "b", "answer", "t", "")

# monitoring band
ax.add_patch(FancyBboxPatch((3, 0.5), 52, 9, boxstyle="round,pad=0,rounding_size=2", fc="#f4f6f9",
                            ec="#9aa7bd", lw=1.2, ls="--"))
ax.text(29, 6.4, "Monitoring (watches every service)", ha="center", fontsize=10.5, fontweight="bold", color=INK)
ax.text(29, 3, "Prometheus  ·  Grafana dashboards  ·  Jaeger traces", ha="center", fontsize=9.5, color="#3d4a5c")

OUT.parent.mkdir(parents=True, exist_ok=True)
fig.savefig(OUT, bbox_inches="tight", facecolor="white")
print("wrote", OUT)
