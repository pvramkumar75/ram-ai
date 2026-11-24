from ramai.dock import dock_width, plan

AREA = (0, 0, 1920, 1032)          # 1080p work area above the taskbar
DOCK = 1920 - dock_width(1920)     # RAM AI panel starts here


def test_dock_width_is_sensible():
    assert dock_width(1920) == 576 and dock_width(1280) == 440 and dock_width(3840) == 640


def test_maximized_app_fills_the_left_area():
    assert plan((-8, -8, 1928, 1040), AREA, DOCK, True) == (0, 0, DOCK, 1032)


def test_small_dialog_slides_left_and_keeps_size():
    assert plan((1500, 300, 1900, 600), AREA, DOCK, False) == (DOCK - 400, 300, 400, 300)


def test_window_already_on_the_left_is_left_alone():
    assert plan((0, 0, 1200, 900), AREA, DOCK, False) is None
