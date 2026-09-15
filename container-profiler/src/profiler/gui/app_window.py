"""Application shell that composes optional explorer features around MainWindow."""
from __future__ import annotations

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMessageBox, QToolBar

from ..core.alert_runtime import AlertRuntime
from ..core.alerting import AlertStatus
from ..utils.paths import get_alert_config_path
from .alert_history import AlertHistoryDialog
from .main_window import MainWindow
from .workload_explorer import WorkloadExplorerDialog


class AppMainWindow(MainWindow):
    """Full desktop window with workload inspection and local alerting."""

    def __init__(self) -> None:
        self.alert_runtime = AlertRuntime(get_alert_config_path())
        self._last_alert_config_error = self.alert_runtime.last_config_error
        self.last_alert_storage_error: str | None = None
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

        self.alert_action = QAction(self)
        self.alert_action.setToolTip(f"告警配置: {get_alert_config_path()}\n點擊強制重新載入")
        self.alert_action.triggered.connect(self._reload_alerts)
        toolbar.addAction(self.alert_action)

        self.alert_history_action = QAction("告警歷史", self)
        self.alert_history_action.setToolTip("查看持久化告警 transition、篩選並確認事件")
        self.alert_history_action.setEnabled(self.telemetry_store is not None)
        self.alert_history_action.triggered.connect(self._show_alert_history)
        toolbar.addAction(self.alert_history_action)
        self._refresh_alert_action()

        self.container_list.container_selected.connect(
            lambda container_id: self.workload_action.setEnabled(bool(container_id))
        )

    def _refresh_alert_action(self) -> None:
        snapshot = self.alert_runtime.snapshot()
        firing = sum(1 for status in snapshot.states.values() if status == AlertStatus.FIRING)
        pending = sum(1 for status in snapshot.states.values() if status == AlertStatus.PENDING)
        if snapshot.last_config_error:
            self.alert_action.setText("告警配置錯誤")
        else:
            self.alert_action.setText(
                f"告警 {firing} firing / {pending} pending / {snapshot.rule_count} rules"
            )

    def _reload_alerts(self) -> None:
        installed = self.alert_runtime.reload(force=True)
        self._last_alert_config_error = self.alert_runtime.last_config_error
        self._refresh_alert_action()
        if self.alert_runtime.last_config_error:
            QMessageBox.warning(
                self, "告警配置無效",
                "已保留上一個可用規則集。\n\n" + self.alert_runtime.last_config_error,
            )
        elif installed:
            QMessageBox.information(
                self, "告警配置", f"已載入 {len(self.alert_runtime.engine.rules)} 條規則。"
            )

    def _persist_alert_events(self, container_id: str, events) -> None:
        store = self.telemetry_store
        if store is None:
            return
        session_id = self.data_manager.session_id
        for event in events:
            try:
                store.append_alert_event(
                    event, session_id=session_id, container_id=container_id
                )
                self.last_alert_storage_error = None
            except Exception as exc:
                # Alert evaluation and live telemetry remain operational if the
                # audit store degrades. Surface the failure rather than retrying
                # synchronously on every sample and blocking the Qt thread.
                self.last_alert_storage_error = str(exc)
                break

    def _handle_new_data(self, container_id: str, stats, power_stats) -> None:
        super()._handle_new_data(container_id, stats, power_stats)
        if container_id != self.monitoring_container_id:
            return

        self.alert_runtime.reload_if_changed()
        config_error = self.alert_runtime.last_config_error
        if config_error != self._last_alert_config_error:
            self._last_alert_config_error = config_error
            if config_error:
                self.statusBar().showMessage("告警配置無效，已保留上一版本: " + config_error)

        events = self.alert_runtime.evaluate(stats.timestamp, stats, power_stats)
        self._refresh_alert_action()
        if not events:
            return

        self._persist_alert_events(container_id, events)
        event = events[-1]
        suffix = " | alert storage degraded" if self.last_alert_storage_error else ""
        if event.current == AlertStatus.FIRING:
            self.statusBar().showMessage(
                f"ALERT [{event.severity}] {event.rule_name}: {event.metric}={event.value:g}{suffix}"
            )
        else:
            self.statusBar().showMessage(
                f"RECOVERED {event.rule_name}: {event.metric}={event.value:g}{suffix}"
            )

    def _show_alert_history(self) -> None:
        if self.telemetry_store is None:
            QMessageBox.warning(self, "Alert Explorer", "本機 SQLite 資料庫不可用")
            return
        AlertHistoryDialog(self.telemetry_store, self).exec()

    def _show_workload_explorer(self) -> None:
        container_id = self.selected_container_id
        if not container_id:
            QMessageBox.information(self, "Container Explorer", "請先選擇一個容器")
            return
        if not self.docker_monitor.is_connected():
            self.docker_monitor.list_containers()
        client = self.docker_monitor.client
        if client is None:
            detail = self.docker_monitor.last_error or "Docker 不可用"
            QMessageBox.warning(self, "Container Explorer 不可用", detail)
            return
        WorkloadExplorerDialog(client, container_id, parent=self).exec()
