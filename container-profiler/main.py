"""
Container Profiler - Main Entry Point
"""
import sys
from pathlib import Path

# Add src to path if running from source
if not getattr(sys, 'frozen', False):
    src_path = Path(__file__).resolve().parent / "src"
    sys.path.insert(0, str(src_path))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from profiler.gui.main_window import MainWindow
from profiler.utils.paths import get_resource_path

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Container Profiler")
    
    # Set icon
    icon_path = get_resource_path("resources/icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
