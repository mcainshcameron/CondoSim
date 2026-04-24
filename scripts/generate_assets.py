"""Generate image assets for CondoSim via FAL.ai.

Reads FAL_KEY from env (or .env). Writes PNGs to imageAssets/ and a
descriptions.md alongside them. Re-running skips files that already exist
unless --force is passed. Pass --only <name1,name2> to run a subset.

Usage:
    python scripts/generate_assets.py                # all jobs
    python scripts/generate_assets.py --only conti   # just one
    python scripts/generate_assets.py --only cover --force
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "imageAssets"
DESC_FILE = OUT_DIR / "descriptions.md"

# ---- load .env so FAL_KEY is picked up without a shell export ------------
def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

_load_dotenv()

FAL_KEY = os.environ.get("FAL_KEY")
if not FAL_KEY:
    print("ERROR: FAL_KEY not set in env or .env", file=sys.stderr)
    sys.exit(1)

# ---- job specs ------------------------------------------------------------
# Square WhatsApp-style portraits for residents; a landscape GTA-style cover.
PORTRAIT_STYLE = (
    "photorealistic portrait, natural lighting, shot on 50mm lens, slight "
    "depth of field, neutral warm color grade, looking toward camera, "
    "tight head-and-shoulders framing suitable for a messaging-app avatar"
)

GTA_STYLE = (
    "GTA V loading-screen cover art style, bold vector illustration with "
    "thick black outlines, cel-shaded flat color fills, high contrast, "
    "saturated warm palette, slight halftone/grunge texture, dramatic rim "
    "light, satirical heroic pose, full figure or three-quarter figure on "
    "a flat solid-color background panel, Rockstar Games promotional art "
    "aesthetic, no text anywhere in the image"
)

JOBS: list[dict] = [
    {
        "name": "conti",
        "model": "fal-ai/flux/dev",
        "size": {"width": 1024, "height": 1024},
        "description": (
            "Sig.ra Maria Conti (2B) — 72-year-old Italian widow, retired "
            "schoolteacher. Stern, traditional, 40+ years in the building."
        ),
        "prompt": (
            f"{PORTRAIT_STYLE}. A 72-year-old Italian woman, widow, retired "
            "primary school teacher. Silver-white hair pulled back in a neat "
            "low bun, a few loose strands. Lined face, sharp observant eyes, "
            "thin pressed lips — skeptical, unimpressed expression, as if "
            "she's just heard something foolish. Simple dark blouse with a "
            "small gold cross at the neck, a modest cardigan over her "
            "shoulders. Soft diffused window light from the left. Background: "
            "the slightly out-of-focus interior of a 1970s Italian apartment "
            "with lace curtains. Muted palette, dignified, not glamorous."
        ),
    },
    {
        "name": "ferrari",
        "model": "fal-ai/flux/dev",
        "size": {"width": 1024, "height": 1024},
        "description": (
            "Marco Ferrari (5A) — 31-year-old Italian management consultant. "
            "Sharp, reserved, faintly condescending, commutes to Milan."
        ),
        "prompt": (
            f"{PORTRAIT_STYLE}. A 31-year-old Italian man, management "
            "consultant. Short dark hair styled neatly, clean shave or very "
            "light stubble, sharp jawline, subtle cold smirk — the kind of "
            "smile that doesn't reach the eyes. Crisp navy blazer over a "
            "white open-collar shirt, no tie. Looks like he's half-checking "
            "his phone off-camera. Background: slightly blurred glass-and-"
            "steel lobby of a Milan office tower at dusk, warm artificial "
            "lighting. Confident, aloof, faintly arrogant vibe."
        ),
    },
    {
        "name": "greco",
        "model": "fal-ai/flux/dev",
        "size": {"width": 1024, "height": 1024},
        "description": (
            "Valentina Greco (7A) — 38-year-old 'real-estate consultant', "
            "new penthouse owner. Composed, calculating, charming on demand."
        ),
        "prompt": (
            f"{PORTRAIT_STYLE}. A 38-year-old Italian woman, elegant and "
            "composed. Dark auburn hair cut in a sleek shoulder-length bob, "
            "subtle bronzy makeup, a small knowing smile that barely moves "
            "her eyes. Tailored cream silk blouse, delicate gold chain, one "
            "small hoop earring catching the light. Expression: attentive, "
            "measured, impossible to read. Background: out-of-focus modern "
            "penthouse interior at golden hour, floor-to-ceiling window with "
            "a soft view of Italian rooftops. Expensive, understated, "
            "deliberately charming."
        ),
    },
    {
        "name": "marchetti",
        "model": "fal-ai/flux/dev",
        "size": {"width": 1024, "height": 1024},
        "description": (
            "Davide Marchetti (3B) — 54-year-old part-time worker, full-time "
            "carer for his elderly mother. Anxious, tired, resentful."
        ),
        "prompt": (
            f"{PORTRAIT_STYLE}. A 54-year-old Italian man, working-class, "
            "visibly tired. Thinning salt-and-pepper hair, unshaven three-"
            "day stubble, dark circles under the eyes, slightly slumped "
            "posture. Cheap gray half-zip sweater over a faded t-shirt. "
            "Expression: anxious, guarded, a hint of bitterness around the "
            "mouth, distant gaze — as if he just thought of something that "
            "worries him. Background: a dim, cluttered 1980s Italian "
            "apartment kitchen with yellowing walls, a clock just visible, "
            "soft overhead fluorescent light. Dignified but worn-down."
        ),
    },
    {
        "name": "romano",
        "model": "fal-ai/flux/dev",
        "size": {"width": 1024, "height": 1024},
        "description": (
            "Giulia Romano (4C) — 34-year-old Italian designer, first-time "
            "buyer. Extroverted, stylish, ambitious, very online."
        ),
        "prompt": (
            f"{PORTRAIT_STYLE}. A 34-year-old Italian woman, graphic/interior "
            "designer. Dark wavy shoulder-length hair with caramel "
            "highlights, slightly tousled in a deliberate way, warm olive "
            "skin, natural makeup with a bold red lip, wide open smile "
            "showing she just laughed. Mustard-yellow oversized knit "
            "sweater, a few delicate layered gold necklaces, small stud "
            "earrings. Holding an espresso cup just barely in frame. "
            "Background: a bright, plant-filled design studio with a "
            "mood-board pinned to a cork wall, out of focus. Warm, vibrant, "
            "photogenic energy."
        ),
    },
    # ---- GTA-style mugshots (one per resident) --------------------------
    # Tall portrait orientation, flat solid-color background so they can be
    # composed as side-by-side panels in the landing page with CSS.
    {
        "name": "gta_conti",
        "model": "fal-ai/flux/dev",
        "size": {"width": 768, "height": 1152},
        "description": (
            "GTA-style mugshot panel: Sig.ra Conti. Tall portrait, flat "
            "dusty-red background, for landing-page character grid."
        ),
        "prompt": (
            f"{GTA_STYLE}. A 72-year-old Italian widow, retired "
            "schoolteacher. Silver-white hair in a tight low bun, deep "
            "wrinkles, pressed thin lips, arms crossed, giving a "
            "disapproving sideways glare. Plain dark charcoal blouse with "
            "a small gold cross at the neck, modest cardigan. Stands rigid "
            "and judgmental. Flat solid dusty-red / terracotta background, "
            "slight halftone noise. No text, no logo, no letters."
        ),
    },
    {
        "name": "gta_ferrari",
        "model": "fal-ai/flux/dev",
        "size": {"width": 768, "height": 1152},
        "description": (
            "GTA-style mugshot panel: Marco Ferrari. Tall portrait, flat "
            "cold-blue background."
        ),
        "prompt": (
            f"{GTA_STYLE}. A 31-year-old Italian management consultant. "
            "Short neat dark hair, light stubble, sharp jaw, smug half-"
            "smirk. Crisp navy blazer over a white open-collar shirt, no "
            "tie. One hand casually in pocket, the other holding a "
            "smartphone he's half-glancing at. Confident, aloof, faintly "
            "condescending pose. Flat solid steel-blue background, slight "
            "halftone noise. No text, no logo, no letters."
        ),
    },
    {
        "name": "gta_greco",
        "model": "fal-ai/flux/dev",
        "size": {"width": 768, "height": 1152},
        "description": (
            "GTA-style mugshot panel: Valentina Greco. Tall portrait, flat "
            "champagne / warm-gold background."
        ),
        "prompt": (
            f"{GTA_STYLE}. A 38-year-old elegant Italian woman, real-estate "
            "consultant. Sleek shoulder-length auburn bob, bronzy makeup, "
            "small knowing smile. Tailored cream silk blouse, tailored black "
            "trousers, delicate gold chain. Standing three-quarter view, "
            "anatomically correct with exactly two arms and two hands. Her "
            "right hand rests on her hip; her left arm hangs relaxed at her "
            "side with her left hand lightly holding a small glass of red "
            "wine near her thigh. Arms NOT crossed. No extra limbs, no "
            "duplicated arms. Confident, composed, quietly dangerous pose. "
            "Flat solid champagne-gold background, slight halftone noise. "
            "No text, no logo, no letters."
        ),
    },
    {
        "name": "gta_marchetti",
        "model": "fal-ai/flux/dev",
        "size": {"width": 768, "height": 1152},
        "description": (
            "GTA-style mugshot panel: Davide Marchetti. Tall portrait, flat "
            "muddy olive / mustard background."
        ),
        "prompt": (
            f"{GTA_STYLE}. A 54-year-old tired working-class Italian man. "
            "Thinning salt-and-pepper hair, three-day stubble, heavy dark "
            "circles under the eyes, slightly slumped shoulders. Cheap "
            "gray half-zip sweater over a faded t-shirt. One hand pinching "
            "the bridge of his nose in exhaustion, the other holding a "
            "phone down by his hip. Worn-out, resentful, anxious pose. "
            "Flat solid muddy-olive / mustard background, slight halftone "
            "noise. No text, no logo, no letters."
        ),
    },
    {
        "name": "gta_romano",
        "model": "fal-ai/flux/dev",
        "size": {"width": 768, "height": 1152},
        "description": (
            "GTA-style mugshot panel: Giulia Romano. Tall portrait, flat "
            "hot-pink / coral background."
        ),
        "prompt": (
            f"{GTA_STYLE}. A 34-year-old Italian woman, stylish designer. "
            "Shoulder-length dark wavy hair with caramel highlights, warm "
            "olive skin, bold red lips, wide laughing smile. Mustard-yellow "
            "oversized knit sweater, layered gold necklaces. One arm "
            "extended holding up a smartphone taking a selfie toward the "
            "viewer, other hand on her hip, tilted confident pose. "
            "Vibrant, extroverted, main-character energy. Flat solid "
            "hot-coral / pink background, slight halftone noise. No text, "
            "no logo, no letters."
        ),
    },
    # ---- title-less skyline backdrop ------------------------------------
    {
        "name": "gta_skyline_backdrop",
        "model": "fal-ai/flux/dev",
        "size": {"width": 1536, "height": 1024},
        "description": (
            "Title-less GTA-style landscape backdrop: the Italian condo "
            "facade at sunset. Designed to sit behind a web-rendered "
            "'CondoSim' logo + character mugshots on the landing page."
        ),
        "prompt": (
            "GTA V cover art illustration style, bold vector look, thick "
            "outlines, cel-shaded flat colors, dramatic composition. Wide "
            "landscape view of a 1970s Italian urban condominium: a 6- to "
            "7-story residential building with balconies, laundry hanging, "
            "a small pharmacy (green cross) and tobacconist sign on the "
            "ground floor, scooters parked outside, a narrow street. "
            "Warm orange-and-magenta sunset sky with long dramatic "
            "shadows, a faint haze, the city rooftops trailing into the "
            "distance. Slight halftone / print-grain texture. Centered "
            "wide composition with a large clean sky area at the top "
            "reserved for a title to be overlaid later. Absolutely no "
            "text, no letters, no numbers, no logos anywhere in the "
            "image."
        ),
    },
    {
        "name": "cover_gta_style",
        "model": "fal-ai/flux/dev",
        "size": {"width": 1536, "height": 1024},
        "description": (
            "Landing-page hero in GTA V cover-art style: multi-panel grid "
            "illustration of the 5 residents + the Italian condo building, "
            "with 'CondoSim' title treatment. Landscape 3:2."
        ),
        "prompt": (
            "GTA V style cover art illustration, bold vector-like character "
            "painting with thick outlines, saturated color blocking, high "
            "contrast cel-shading. A landscape poster divided into a "
            "grid of panels, each panel framing one Italian condominium "
            "resident in an exaggerated heroic/satirical pose: "
            "(1) a stern 72-year-old Italian grandmother in a dark blouse, "
            "arms crossed, judging; "
            "(2) a smug 31-year-old man in a navy blazer checking his phone; "
            "(3) an elegant 38-year-old woman in a cream silk blouse, "
            "holding a wine glass, knowing smile; "
            "(4) a tired 54-year-old man in a gray sweater, rubbing his "
            "forehead, phone in the other hand; "
            "(5) a 34-year-old stylish woman in a mustard sweater, laughing, "
            "taking a selfie. "
            "A central panel shows an ornate 1970s Italian condominium "
            "facade (6-story, balconies with laundry, pharmacy cross and "
            "a tobacconist on the ground floor) at warm sunset. Large bold "
            "stylized title text 'CondoSim' across the top in a heavy "
            "industrial sans-serif, slight grunge texture. Subtitle in "
            "smaller text: 'Condominio Via Garibaldi'. The whole composition "
            "has the iconic Rockstar Games cover art look — dramatic, "
            "satirical, pop-art, desaturated highlights with bold color pops."
        ),
    },
]

# ---- FAL client -----------------------------------------------------------
def _generate(job: dict) -> str:
    url = f"https://fal.run/{job['model']}"
    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": job["prompt"],
        "image_size": job["size"],
        "num_images": 1,
        "enable_safety_checker": False,
    }
    with httpx.Client(timeout=180.0) as client:
        r = client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    images = data.get("images") or []
    if not images:
        raise RuntimeError(f"no images in response: {data}")
    return images[0]["url"]


# Jobs whose backgrounds we strip for overlay compositions. Cutouts land at
# imageAssets/<name>_cutout.png.
CUTOUT_TARGETS = [
    "gta_conti", "gta_ferrari", "gta_greco", "gta_marchetti", "gta_romano",
]

# PNG -> WebP conversion + resize. Source paths are relative to imageAssets/;
# destination paths are relative to frontend/public/. Smaller files = faster
# page loads; WebP typically beats PNG 5-10x at visually-identical quality.
WEBP_CONVERSIONS = [
    # Photoreal avatars — 1024² source, display ~30-42px so 512 is plenty
    # at 2x retina.
    {"src": "conti.png",     "dst": "avatars/conti.webp",     "max_side": 512,  "quality": 85},
    {"src": "ferrari.png",   "dst": "avatars/ferrari.webp",   "max_side": 512,  "quality": 85},
    {"src": "greco.png",     "dst": "avatars/greco.webp",     "max_side": 512,  "quality": 85},
    {"src": "marchetti.png", "dst": "avatars/marchetti.webp", "max_side": 512,  "quality": 85},
    {"src": "romano.png",    "dst": "avatars/romano.webp",    "max_side": 512,  "quality": 85},
    # Landing-page hero.
    {"src": "gta_skyline_backdrop.png", "dst": "hero/skyline.webp", "max_side": 1920, "quality": 85},
    # Cutouts — keep alpha channel.
    {"src": "gta_conti_cutout.png",     "dst": "hero/conti_cutout.webp",     "max_side": 900, "quality": 85, "alpha": True},
    {"src": "gta_ferrari_cutout.png",   "dst": "hero/ferrari_cutout.webp",   "max_side": 900, "quality": 85, "alpha": True},
    {"src": "gta_greco_cutout.png",     "dst": "hero/greco_cutout.webp",     "max_side": 900, "quality": 85, "alpha": True},
    {"src": "gta_marchetti_cutout.png", "dst": "hero/marchetti_cutout.webp", "max_side": 900, "quality": 85, "alpha": True},
    {"src": "gta_romano_cutout.png",    "dst": "hero/romano_cutout.webp",    "max_side": 900, "quality": 85, "alpha": True},
]

PUBLIC_DIR = ROOT / "frontend" / "public"


def _convert_webp(spec: dict) -> None:
    from PIL import Image  # lazy import; only needed in --webp mode
    src = OUT_DIR / spec["src"]
    dst = PUBLIC_DIR / spec["dst"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as img:
        # Preserve alpha for cutouts; flatten to RGB otherwise so quality
        # setting behaves predictably and file size stays tight.
        if spec.get("alpha"):
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")
        img.thumbnail((spec["max_side"], spec["max_side"]), Image.LANCZOS)
        img.save(dst, "WEBP", quality=spec["quality"], method=6)
    src_kb = src.stat().st_size // 1024
    dst_kb = dst.stat().st_size // 1024
    print(f"[webp] {spec['src']} ({src_kb}KB) -> {spec['dst']} ({dst_kb}KB, "
          f"{img.width}x{img.height})")


def _remove_bg(src: Path) -> str:
    """Send a local PNG to FAL birefnet, return the cutout URL."""
    data_url = "data:image/png;base64," + base64.b64encode(
        src.read_bytes()
    ).decode("ascii")
    url = "https://fal.run/fal-ai/birefnet"
    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=180.0) as client:
        r = client.post(url, headers=headers, json={"image_url": data_url})
        r.raise_for_status()
        data = r.json()
    img = data.get("image") or {}
    if not img.get("url"):
        raise RuntimeError(f"no image url in birefnet response: {data}")
    return img["url"]


def _download(url: str, dest: Path) -> None:
    with httpx.Client(timeout=180.0, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)


def _write_descriptions() -> None:
    lines = ["# CondoSim image assets", ""]
    for job in JOBS:
        lines.append(f"## `{job['name']}.png`")
        lines.append("")
        lines.append(job["description"])
        lines.append("")
        lines.append(f"- model: `{job['model']}`")
        lines.append(f"- size: {job['size']['width']}x{job['size']['height']}")
        lines.append("")
    DESC_FILE.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated job names to run")
    ap.add_argument("--force", action="store_true",
                    help="regenerate even if file exists")
    ap.add_argument("--cutouts", action="store_true",
                    help="run background removal over CUTOUT_TARGETS "
                         "instead of running generation jobs")
    ap.add_argument("--webp", action="store_true",
                    help="convert source PNGs to resized WebP into "
                         "frontend/public/ (no FAL calls)")
    args = ap.parse_args()

    OUT_DIR.mkdir(exist_ok=True)

    if args.webp:
        for spec in WEBP_CONVERSIONS:
            try:
                _convert_webp(spec)
            except Exception as e:
                print(f"[fail] {spec['src']}: {e}", file=sys.stderr)
                return 1
        return 0

    if args.cutouts:
        wanted = set((args.only or "").split(",")) if args.only else None
        for name in CUTOUT_TARGETS:
            if wanted and name not in wanted:
                continue
            src = OUT_DIR / f"{name}.png"
            dest = OUT_DIR / f"{name}_cutout.png"
            if not src.exists():
                print(f"[skip] {src.name} missing — generate it first")
                continue
            if dest.exists() and not args.force:
                print(f"[skip] {dest.name} exists")
                continue
            print(f"[rmbg] {src.name} -> {dest.name} ...", flush=True)
            try:
                cutout_url = _remove_bg(src)
                _download(cutout_url, dest)
                print(f"[ok  ] {dest.name} ({dest.stat().st_size // 1024} KB)")
            except Exception as e:
                print(f"[fail] {dest.name}: {e}", file=sys.stderr)
                return 1
        return 0

    wanted = set((args.only or "").split(",")) if args.only else None

    for job in JOBS:
        if wanted and job["name"] not in wanted:
            continue
        dest = OUT_DIR / f"{job['name']}.png"
        if dest.exists() and not args.force:
            print(f"[skip] {dest.name} exists")
            continue
        print(f"[gen ] {dest.name} via {job['model']} ...", flush=True)
        try:
            img_url = _generate(job)
            _download(img_url, dest)
            print(f"[ok  ] {dest.name} ({dest.stat().st_size // 1024} KB)")
        except Exception as e:
            print(f"[fail] {dest.name}: {e}", file=sys.stderr)
            return 1

    _write_descriptions()
    print(f"\nwrote {DESC_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
