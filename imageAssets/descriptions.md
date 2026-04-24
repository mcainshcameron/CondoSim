# CondoSim image assets

Generated via `scripts/generate_assets.py` (FAL.ai flux/dev + birefnet).
Organized by intended use below.

## In-app avatars (photoreal, square)

WhatsApp-style profile photos for the 5 residents. 1024x1024 PNG.

| file | resident | vibe |
| --- | --- | --- |
| `conti.png` | Maria Conti, 72, 2B | stern widow, retired teacher, small gold cross |
| `ferrari.png` | Marco Ferrari, 31, 5A | smug consultant, navy blazer, phone in hand |
| `greco.png` | Valentina Greco, 38, 7A | composed "real-estate consultant", penthouse glow |
| `marchetti.png` | Davide Marchetti, 54, 3B | tired carer, three-day stubble, dim kitchen |
| `romano.png` | Giulia Romano, 34, 4C | laughing designer, mustard sweater, plants |

## Landing-page character cutouts (GTA-style, transparent PNG)

Cel-shaded GTA V cover-art style. 768x1152 PNG with alpha. Drop these over
the skyline backdrop, scale per-character, arrange along the facade.

| file | pose |
| --- | --- |
| `gta_conti_cutout.png` | arms crossed, disapproving sideways glare |
| `gta_ferrari_cutout.png` | navy blazer, one hand in pocket, smirking |
| `gta_greco_cutout.png` | hand in pocket + wine glass by her thigh |
| `gta_marchetti_cutout.png` | pinching bridge of nose, phone in other hand |
| `gta_romano_cutout.png` | arm extended taking a selfie toward viewer |

## Landing-page character panels (GTA-style, solid colored bg)

Same mugshots pre-background-removal. 768x1152 PNG. Useful if you want a
GTA-cover panel-grid look instead of overlaying on the skyline — each
resident has a signature flat color (red / steel-blue / gold / olive / coral).

`gta_conti.png`, `gta_ferrari.png`, `gta_greco.png`, `gta_marchetti.png`,
`gta_romano.png`

## Landing-page skyline backdrop

`gta_skyline_backdrop.png` — 1536x1024, GTA-style Italian condo at sunset.
Big clean sky area at the top deliberately left empty for a CSS-overlaid
"CondoSim" title. No baked text.

## Regenerate / extend

```bash
python scripts/generate_assets.py                     # all missing
python scripts/generate_assets.py --only conti        # one job
python scripts/generate_assets.py --only gta_greco --force
python scripts/generate_assets.py --cutouts           # bg-remove all
python scripts/generate_assets.py --cutouts --only gta_greco --force
```

Requires `FAL_KEY` in `.env` (gitignored).
