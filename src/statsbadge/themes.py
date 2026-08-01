"""The themes, as data.

A theme is a table of colours and one gradient rule, so it is config and not code: the
selected one travels to the badge in its layout, which means a palette can be changed or
invented with nothing installed. The badge carries one of these to boot with and takes the
rest from here.

  bg / panel      the page, and the header, footer and tiles on it
  ink / dim       text, and text that is only labelling something
  accent          the one colour that says "this is the thing"
  grid            the unfilled part of any gauge, and a graph's rules
  ramp            what a gauge fills with as it climbs, cold to hot
  case            the four case lights, which are one brightness each and not RGB
"""

PALETTES = {
    "dark": {
        "bg": (18, 20, 28), "panel": (26, 30, 43),
        "ink": (242, 245, 255), "dim": (139, 147, 171),
        "accent": (56, 232, 209), "grid": (44, 51, 70),
        "case": 0.22,
        "ramp": ((0.0, (56, 232, 209)),
                 (0.45, (126, 211, 117)),
                 (0.72, (236, 159, 7)),
                 (1.0, (215, 25, 8))),
    },
    "light": {
        "bg": (250, 247, 242), "panel": (240, 236, 228),
        "ink": (30, 26, 20), "dim": (102, 94, 82),
        "accent": (16, 145, 157), "grid": (216, 209, 195),
        "case": 0.3,
        "ramp": ((0.0, (16, 145, 157)),
                 (0.45, (81, 146, 74)),
                 (0.72, (188, 103, 12)),
                 (1.0, (138, 3, 22))),
    },
    "frost": {
        "bg": (244, 248, 252), "panel": (231, 237, 244),
        "ink": (22, 27, 33), "dim": (87, 96, 107),
        "accent": (0, 100, 185), "grid": (200, 211, 223),
        "case": 0.3,
        "ramp": ((0.0, (0, 142, 182)),
                 (0.45, (0, 125, 120)),
                 (0.72, (125, 75, 0)),
                 (1.0, (136, 0, 1))),
    },
    "mono": {
        "bg": (8, 8, 8), "panel": (20, 20, 20),
        "ink": (245, 245, 245), "dim": (110, 110, 110),
        "accent": (235, 235, 235), "grid": (38, 38, 38),
        "case": 0.14,
        "ramp": ((0.0, (110, 110, 110)),
                 (1.0, (255, 255, 255))),
    },
    "red": {
        "bg": (28, 18, 16), "panel": (42, 26, 23),
        "ink": (255, 242, 240), "dim": (169, 140, 134),
        "accent": (255, 82, 62), "grid": (68, 45, 41),
        "case": 0.24,
        "ramp": ((0.0, (165, 0, 0)),
                 (0.7, (255, 82, 62)),
                 (1.0, (255, 199, 188))),
    },
    "green": {
        "bg": (16, 22, 15), "panel": (24, 34, 22),
        "ink": (240, 248, 239), "dim": (135, 154, 133),
        "accent": (2, 185, 0), "grid": (41, 56, 40),
        "case": 0.24,
        "ramp": ((0.0, (0, 105, 0)),
                 (0.7, (2, 185, 0)),
                 (1.0, (75, 255, 57))),
    },
    "cyan": {
        "bg": (12, 22, 26), "panel": (16, 33, 40),
        "ink": (236, 248, 252), "dim": (124, 153, 165),
        "accent": (0, 169, 212), "grid": (30, 56, 65),
        "case": 0.24,
        "ramp": ((0.0, (0, 95, 121)),
                 (0.7, (0, 169, 212)),
                 (1.0, (141, 230, 255))),
    },
    "amber": {
        "bg": (14, 8, 0), "panel": (30, 18, 2),
        "ink": (255, 190, 70), "dim": (120, 80, 20),
        "accent": (255, 176, 0), "grid": (56, 34, 4),
        "case": 0.26,
        "ramp": ((0.0, (140, 80, 0)),
                 (0.7, (255, 176, 0)),
                 (1.0, (255, 240, 180))),
    },
    "blueprint": {
        "bg": (6, 16, 34), "panel": (12, 28, 56),
        "ink": (214, 232, 255), "dim": (88, 120, 170),
        "accent": (90, 180, 255), "grid": (28, 56, 100),
        "case": 0.18,
        "ramp": ((0.0, (60, 130, 220)),
                 (0.6, (120, 210, 255)),
                 (1.0, (255, 255, 255))),
    },
    "vapor": {
        "bg": (18, 8, 30), "panel": (34, 14, 56),
        "ink": (245, 225, 255), "dim": (140, 100, 180),
        "accent": (255, 90, 200), "grid": (56, 26, 90),
        "case": 0.24,
        "ramp": ((0.0, (90, 220, 255)),
                 (0.5, (190, 130, 255)),
                 (1.0, (255, 80, 190))),
    },
    # From a photograph of a kanzan cherry in flower: the pale sky as the page, the branch
    # as its ink, and the ramp running sky, blossom, stamen, to the deep cerise at a
    # flower's throat.
    "sakura": {
        "bg": (247, 241, 244), "panel": (240, 226, 234),
        "ink": (58, 40, 50), "dim": (139, 110, 124),
        "accent": (226, 116, 154), "grid": (226, 205, 216),
        "case": 0.3,
        "ramp": ((0.0, (138, 178, 212)),
                 (0.45, (232, 138, 174)),
                 (0.75, (214, 146, 60)),
                 (1.0, (176, 30, 78))),
    },
    # The five colours of the palette it is named after, rind to flesh: #83AF9B, #C8C8A9,
    # #F9CDAD, #FC9D9A, #FE4365. The page is the seed, so the fruit is what shows.
    "watermelon": {
        "bg": (16, 26, 22), "panel": (25, 40, 34),
        "ink": (249, 205, 173), "dim": (150, 152, 129),
        "accent": (254, 67, 101), "grid": (38, 58, 48),
        "case": 0.24,
        "ramp": ((0.0, (131, 175, 155)),
                 (0.4, (200, 200, 169)),
                 (0.72, (252, 157, 154)),
                 (1.0, (254, 67, 101))),
    },
    # Unit-00 as it came back from the repair: navy armour, orange trim. The ramp is that
    # trim warming to the red of a Rei eye.
    "eva00": {
        "bg": (8, 16, 28), "panel": (16, 28, 44),
        "ink": (230, 240, 250), "dim": (124, 150, 178),
        "accent": (240, 146, 42), "grid": (26, 44, 66),
        "case": 0.2,
        "ramp": ((0.0, (74, 168, 226)),
                 (0.5, (240, 146, 42)),
                 (1.0, (214, 42, 42))),
    },
    # Unit-01: violet armour, the acid green of its chest plate, and the ramp going where
    # that unit goes - green, through the orange of the horn, to berserk.
    "eva01": {
        "bg": (22, 14, 34), "panel": (34, 22, 52),
        "ink": (236, 232, 245), "dim": (146, 130, 172),
        "accent": (143, 212, 0), "grid": (52, 34, 78),
        "case": 0.24,
        "ramp": ((0.0, (143, 212, 0)),
                 (0.55, (238, 170, 40)),
                 (1.0, (255, 60, 30))),
    },
    # Unit-02: red armour over orange, which is a theme that is hot before it starts, so the
    # ramp finishes pale the way the other single-hue ones do.
    "eva02": {
        "bg": (28, 12, 12), "panel": (44, 18, 18),
        "ink": (255, 238, 232), "dim": (176, 132, 126),
        "accent": (226, 72, 61), "grid": (70, 28, 26),
        "case": 0.24,
        "ramp": ((0.0, (238, 125, 47)),
                 (0.6, (226, 72, 61)),
                 (1.0, (255, 214, 120))),
    },
    # Gold, night navy, slate, mint and pale green, off a palette of the 1995 film. Green on
    # navy is the terminal; the gold is the only warm thing in it, so the ramp ends there.
    "shell": {
        "bg": (14, 24, 52), "panel": (22, 36, 72),
        "ink": (182, 222, 181), "dim": (120, 156, 176),
        "accent": (120, 214, 168), "grid": (34, 54, 92),
        "case": 0.22,
        "ramp": ((0.0, (52, 95, 117)),
                 (0.4, (120, 214, 168)),
                 (0.7, (182, 222, 181)),
                 (1.0, (218, 163, 60))),
    },
}

DEFAULT = "dark"
