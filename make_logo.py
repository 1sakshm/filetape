"""Draw the Filetape logo: a file winding from a full reel onto an empty one."""

from pathlib import Path

from PIL import Image, ImageDraw

STATIC = Path(__file__).parent / "static"
FIRE, INK, BONE = "#ff4a12", "#120f0d", "#f4ecdf"
SIZE, TAPE_Y, TAPE = 64, 45, 5
BIG = (20, 31, 16, 6)  # centre x, centre y, radius, hub radius
SMALL = (49, 37, 10, 4)
TAIL = 61  # where the tape runs off to


def svg(background):
    plate = f'<rect width="{SIZE}" height="{SIZE}" fill="{background}"/>' if background else ""
    parts = [plate]
    parts.append(f'<rect x="{BIG[0]}" y="{TAPE_Y}" width="{TAIL - BIG[0]}" height="{TAPE}" fill="{INK}"/>')
    for x, y, radius, hub in (BIG, SMALL):
        parts.append(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{INK}"/><circle cx="{x}" cy="{y}" r="{hub}" fill="{BONE}"/>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" role="img" aria-label="Filetape">{"".join(parts)}</svg>\n'


def png(path, size):
    scale = 8
    image = Image.new("RGB", (size * scale, size * scale), FIRE)
    draw = ImageDraw.Draw(image)
    u = size * scale / SIZE
    draw.rectangle((BIG[0] * u, TAPE_Y * u, TAIL * u, (TAPE_Y + TAPE) * u), fill=INK)
    for x, y, radius, hub in (BIG, SMALL):
        draw.ellipse(((x - radius) * u, (y - radius) * u, (x + radius) * u, (y + radius) * u), fill=INK)
        draw.ellipse(((x - hub) * u, (y - hub) * u, (x + hub) * u, (y + hub) * u), fill=BONE)
    image.resize((size, size), Image.LANCZOS).save(path)


(STATIC / "favicon.svg").write_text(svg(FIRE), encoding="utf-8")
(STATIC / "logo.svg").write_text(svg(None), encoding="utf-8")
png(STATIC / "logo-512.png", 512)
png(STATIC / "apple-touch-icon.png", 180)
png(STATIC / "favicon-32.png", 32)
