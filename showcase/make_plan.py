"""Build the plan for the hype cut from the chosen segments, spotter boxes and creator stats."""
import json, sys
from pathlib import Path
D = Path(".context/showcase")
boxes = {(b["key"], b["start"]): b["boxes"] for b in json.loads((D / "boxes.json").read_text())}
meta = {m["key"]: m for m in json.loads((D / "meta.json").read_text())}
src = lambda k: str((D / f"src_{k}.mp4").resolve())
A, B, C, T, S = "tiktok_7340096228866231558", "tiktok_7141192012316871979", "tiktok_7654831376461548813", "youtube_r3ztwAsakOU", "tiktok_7634221398096989471"

def clip(key, caption, *spans):
    m = meta[key]
    return {"handle": m["handle"], "platform": m["platform"], "followers": m.get("followers"), "views": m.get("views"),
            "official": key == S, "caption": caption,
            "shots": [{"src": src(key), "start": s, "end": e, "boxes": boxes.get((key, s), [])} for s, e in spans]}

plan = {
    "tag": "STANLEY QUENCHER H2.0", "product_lines": ["STANLEY", "QUENCHER H2.0"], "cta": "GET YOURS TODAY",
    "intro": [{"src": src(S), "start": 8.3, "word": "THE INTERNET"}, {"src": src(T), "start": 4.8, "word": "IS OBSESSED"}, {"src": src(A), "start": 2.1, "word": "WITH THIS"}],
    "clips": [
        clip(A, "MY STANLEY ERA", (1.8, 4.3)),
        clip(B, "WORTH THE UPGRADE", (5.0, 7.5)),
        clip(C, "GOT THE HYPE", (1.0, 3.5)),
        clip(T, "COLD FOR HOURS", (1.5, 4.0)),
        clip(S, "CUPHOLDER READY", (2.9, 3.9), (8.0, 9.5)),
    ],
    "recap": [{"src": src(B), "start": 2.6}, {"src": src(C), "start": 6.6}, {"src": src(T), "start": 5.3}, {"src": src(S), "start": 10.2}],
    "hero": {"src": src(T), "start": 4.6, "end": 7.8, "boxes": boxes.get((T, 4.6), [])},
}
if len(sys.argv) > 1:  # quick test: fewer clips
    plan["clips"] = plan["clips"][: int(sys.argv[1])]
(D / "plan.json").write_text(json.dumps(plan, indent=1))
print(len(plan["clips"]), "clips")
