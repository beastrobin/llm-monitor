"""Desktop icons for the loader and ComfyUI launchers.

Both shortcuts pointed at .bat files, so Windows drew the generic batch
sheet -- nothing on the desktop said which launcher was which.  These
icons share make_icon.py's visual family (Nord panel background, the
same corner radius) so the three launchers read as one set:

* loader.ico  -- an arrow loading a model into the tray, which is what
  llamacpp-loader does with its presets;
* comfyui.ico -- three nodes and two edges, the node graph ComfyUI is.

Same 256-then-downsample rule as make_icon.py.
"""

from PIL import Image, ImageDraw

from make_icon import SIZE, BG_TOP, BG_BOTTOM, BORDER, GREEN, GREEN_DIM

FROST = (136, 192, 208)     # nord7 -- ComfyUI's graph reads better in blue
FROST_DIM = (136, 192, 208, 90)


def base():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], 52, fill=255)
    grad = Image.new("RGBA", (SIZE, SIZE))
    gd = ImageDraw.Draw(grad)
    for y in range(SIZE):
        t = y / SIZE
        row = tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
        gd.line([(0, y), (SIZE, y)], fill=row + (255,))
    img.paste(grad, (0, 0), mask)
    ImageDraw.Draw(img).rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], 52,
                                          outline=BORDER + (255,), width=3)
    return img


def loader():
    img = base()
    d = ImageDraw.Draw(img)
    # Arrow down into an open tray: the "load a preset" gesture.
    d.line([(128, 44), (128, 148)], fill=GREEN + (255,), width=18)
    d.polygon([(96, 128), (160, 128), (128, 176)], fill=GREEN + (255,))
    d.line([(64, 196), (64, 214), (192, 214), (192, 196)],
           fill=GREEN_DIM, width=16, joint="curve")
    img.save("assets/loader.ico", sizes=[(16, 16), (24, 24), (32, 32),
                                         (48, 48), (64, 64), (128, 128), (256, 256)])


def comfyui():
    img = base()
    d = ImageDraw.Draw(img)
    # Node graph: two inputs feeding one output.
    edges = [((78, 84), (170, 128)), ((78, 176), (170, 128))]
    for a, b in edges:
        d.line([a, b], fill=FROST_DIM, width=22)
        d.line([a, b], fill=FROST + (255,), width=10)
    for cx, cy, r in [(78, 84, 26), (78, 176, 26), (170, 128, 34)]:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BG_BOTTOM + (255,),
                  outline=FROST + (255,), width=10)
    d.ellipse([170 - 12, 128 - 12, 170 + 12, 128 + 12], fill=FROST + (255,))
    img.save("assets/comfyui.ico", sizes=[(16, 16), (24, 24), (32, 32),
                                          (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    loader()
    comfyui()
    print("assets/loader.ico, assets/comfyui.ico written")
