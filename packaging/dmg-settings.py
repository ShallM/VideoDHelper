from pathlib import Path

app = Path(defines["app"])
assets = Path(defines["assets"])
files = [str(app)]
symlinks = {"Applications": "/Applications"}
format = "UDZO"
filesystem = "HFS+"
volume_name = "VideoDHelper"
background = str(assets / "dmg-background.png")
icon = str(app / "Contents/Resources/AppIcon.icns")
window_rect = ((200, 160), (760, 470))
default_view = "icon-view"
show_toolbar = False
show_status_bar = False
show_tab_view = False
show_pathbar = False
show_sidebar = False
include_icon_view_settings = True
include_list_view_settings = False
icon_size = 128
text_size = 13
label_pos = "bottom"
icon_locations = {"VideoDHelper.app": (190, 220), "Applications": (570, 220)}
# SetFile -a E adds FinderInfo after signing and invalidates strict verification.
# Finder already presents application bundles without their extension by default.
hide_extensions = []
