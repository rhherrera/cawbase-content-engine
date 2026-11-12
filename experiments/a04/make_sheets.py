"""Contact sheets for manual render review (before | after side by side per page)."""

import sys
from pathlib import Path

from PIL import Image, ImageDraw


def sheet(files, out, per, scale):
    ims = []
    for f in files:
        im = Image.open(f).convert("L")
        bb = im.point(lambda v: 255 if v < 100 else 0).getbbox()
        top = max((bb[1] if bb else 0) - 20, 0)
        bot = min((bb[3] if bb else im.height) + 20, im.height)
        im = im.crop((0, top, im.width, bot))
        if scale != 1.0:
            im = im.resize((int(im.width * scale), int(im.height * scale)))
        lab = Image.new("L", (im.width, 16), 255)
        ImageDraw.Draw(lab).text((2, 2), f.name, fill=0)
        ims.append((lab, im))
    for k in range(0, len(ims), per):
        grp = ims[k : k + per]
        H = sum(a.height + b.height + 6 for a, b in grp)
        Wd = max(b.width for a, b in grp)
        sh = Image.new("L", (Wd, H), 200)
        y = 0
        for a, b in grp:
            sh.paste(a, (0, y))
            y += a.height
            sh.paste(b, (0, y))
            y += b.height + 6
        sh.save(f"{out}-{k // per + 1}.png")


if __name__ == "__main__":
    pat, out, per, scale = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
    d, g = pat.rsplit("/", 1)
    sheet(sorted(Path(d).glob(g)), out, per, scale)
