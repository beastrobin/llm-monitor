"""Generate the dashboard's own .ico for the desktop shortcut.

The shortcut used to borrow pythonw.exe's icon -- a generic snake that says
"some python thing", not "this is the LLM monitor".  The generated icon
follows the panel's own visual language instead: the dark Nord panel
background, the green status pulse, and the sparkline the model cards draw.

Drawn at 256 and downsampled, because a rounded rectangle and a 2 px line
only look right at 16 px if the big image was clean to begin with.
"""

from PIL import Image, ImageDraw

SIZE = 256

# Nord palette, same family the panel's dark theme is tuned to.
BG_TOP = (59, 66, 82)      # nord2
BG_BOTTOM = (46, 52, 64)   # nord0
BORDER = (76, 86, 106)     # nord3
GREEN = (163, 190, 140)    # nord10 -- the panel's --good
GREEN_DIM = (163, 190, 140, 90)


def base():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Rounded square with a vertical gradient, painted row by row inside a
    # mask so the corners stay antialiased.
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], 52, fill=255)
    grad = Image.new("RGBA", (SIZE, SIZE))
    gd = ImageDraw.Draw(grad)
    for y in range(SIZE):
        t = y / SIZE
        row = tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
        gd.line([(0, y), (SIZE, y)], fill=row + (255,))
    img.paste(grad, (0, 0), mask)
    d.rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], 52, outline=BORDER + (255,), width=3)
    return img


def pulse(d):
    # The heartbeat line: flat, spike, dip, flat -- the "is it alive" glyph
    # the panel's header dot already uses.
    pts = [(36, 140), (84, 140), (104, 96), (128, 176), (150, 118), (172, 140), (220, 140)]
    d.line(pts, fill=GREEN_DIM, width=34, joint="curve")   # glow
    d.line(pts, fill=GREEN + (255,), width=16, joint="curve")
    for x, y in [(36, 140), (220, 140)]:
        d.ellipse([x - 9, y - 9, x + 9, y + 9], fill=GREEN + (255,))


def main():
    img = base()
    pulse(ImageDraw.Draw(img))
    img.save("assets/monitor.ico", sizes=[(16, 16), (24, 24), (32, 32),
                                          (48, 48), (64, 64), (128, 128), (256, 256)])
    print("assets/monitor.ico written")


if __name__ == "__main__":
    main()
