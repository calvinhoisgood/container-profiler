"""Container Profiler desktop entry point."""
import sys
from pathlib import Path

# Add src to path if running from source.
if not getattr(sys, "frozen", False):
    src_path = Path(__file__).resolve().parent / "src"
    sys.path.insert(0, str(src_path))

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from profiler.gui.app_window import AppMainWindow
from profiler.utils.paths import get_resource_path


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Container Profiler")

    icon_path = get_resource_path("resources/icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    window = AppMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
