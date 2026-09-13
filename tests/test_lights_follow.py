"""灯条常驻颜色跟随配置档。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ps5mapper import lights as lt

BLUE = (40, 110, 255)
PROF = (170, 80, 255)


def base(**over):
    d = lt.default_lights()
    d.update(over)
    return d


def test_off_by_default_keeps_the_global_colour():
    assert lt.default_lights()["color_follows_profile"] is False
    v = lt.resolve(base(brightness=100), 0.0, profile_color=PROF)
    assert v["lightbar"] == BLUE


def test_following_uses_the_profile_colour():
    v = lt.resolve(base(color_follows_profile=True, brightness=100),
                   0.0, profile_color=PROF)
    assert v["lightbar"] == PROF


def test_following_without_a_profile_colour_falls_back():
    v = lt.resolve(base(color_follows_profile=True, brightness=100), 0.0)
    assert v["lightbar"] == BLUE


def test_flash_is_skipped_when_already_following():
    """常驻颜色已经跟着档走了，再闪一下和不闪长得一样 —— 别白发包。"""
    v = lt.resolve(base(color_follows_profile=True, brightness=100), 0.0,
                   flash_color=(255, 0, 0), flash_until=99.0, profile_color=PROF)
    assert v["lightbar"] == PROF


def test_flash_still_works_when_not_following():
    v = lt.resolve(base(brightness=100), 0.0,
                   flash_color=(255, 0, 0), flash_until=99.0, profile_color=PROF)
    assert v["lightbar"] == (255, 0, 0)


def test_following_respects_breathing_and_brightness():
    v = lt.resolve(base(color_follows_profile=True, brightness=50,
                        effect="breathe"), 0.0, profile_color=(200, 100, 50))
    assert v["lightbar"] != (200, 100, 50)
    assert all(c <= 100 for c in v["lightbar"])


def test_pause_still_wins_over_following():
    v = lt.resolve(base(color_follows_profile=True, brightness=100), 0.0,
                   paused=True, profile_color=PROF)
    assert v["lightbar"] == lt.PAUSED_COLOR
