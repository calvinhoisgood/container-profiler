"""Application shell that composes optional explorer features around MainWindow."""
from __future__ import annotations

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMessageBox, QToolBar

from .main_window import MainWindow
from .workload_explorer import WorkloadExplorerDialog


class AppMainWindow(MainWindow):
    """Full desktop window with container workload inspection tools."""

    def __init__(self) -> None:
        super().__init__()
        self._setup_workload_toolbar()

    def _setup_workload_toolbar(self) -> None:
        toolbar = QToolBar("Container Explorer", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.workload_action = QAction("進程 / 日誌", self)
        self.workload_action.setToolTip("查看所選容器的進程和 bounded 日誌快照")
        self.workload_action.setEnabled(bool(self.selected_container_id))
        self.workload_action.triggered.connect(self._show_workload_explorer)
        toolbar.addAction(self.workload_action)

        # MainWindow already consumes this signal for status/selection state.
        # The second listener only updates feature availability.
        self.container_list.container_selected.connect(
            lambda container_id: self.workload_action.setEnabled(bool(container_id))
        )

    def _show_workload_explorer(self) -> None:
        container_id = self.selected_container_id
        if not container_id:
            QMessageBox.information(self, "Container Explorer", "請先選擇一個容器")
            return

        # Reuse the established Docker SDK client. list_containers() performs
        # the monitor's normal reconnect path if the previous client went stale.
        if not self.docker_monitor.is_connected():
            self.docker_monitor.list_containers()
        client = self.docker_monitor.client
        if client is None:
            detail = self.docker_monitor.last_error or "Docker 不可用"
            QMessageBox.warning(self, "Container Explorer 不可用", detail)
            return

        dialog = WorkloadExplorerDialog(
            client,
            container_id,
            parent=self,
        )
        dialog.exec()
