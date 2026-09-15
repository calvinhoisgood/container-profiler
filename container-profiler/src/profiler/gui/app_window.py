"""Application shell that composes agent-style Explorer features around MainWindow."""
from __future__ import annotations

import time

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction, QCloseEvent
from PyQt6.QtWidgets import QMessageBox, QToolBar

from ..core.alert_runtime import AlertRuntime
from ..core.alerting import AlertStatus
from ..core.openmetrics import OpenMetricsCollector
from ..core.openmetrics_runtime import (
    OpenMetricsRuntimeWorker,
    resolve_openmetrics_for_containers,
)
from ..utils.paths import get_alert_config_path
from .alert_history import AlertHistoryDialog
from .custom_metrics_explorer import CustomMetricsExplorerDialog
from .main_window import MainWindow
from .workload_explorer import WorkloadExplorerDialog


class AppMainWindow(MainWindow):
    """Full desktop window with workload, alerting, and custom-metric runtime."""

    CUSTOM_METRIC_DRAIN_MS = 500
    CUSTOM_METRIC_DRAIN_BATCH = 1000
    CUSTOM_METRIC_RETENTION_ROWS = 250_000
    CUSTOM_METRIC_RETENTION_S = 7 * 24 * 60 * 60
    CUSTOM_METRIC_RETENTION_EVERY_TICKS = 600  # five minutes at 500 ms

    def __init__(self) -> None:
        self.alert_runtime = AlertRuntime(get_alert_config_path())
        self._last_alert_config_error = self.alert_runtime.last_config_error
        self.last_alert_storage_error: str | None = None

        # MainWindow.__init__ calls _refresh_container_list dynamically, so all
        # subclass fields touched by that override must exist before super().
        self.openmetrics_worker: OpenMetricsRuntimeWorker | None = None
        self.openmetrics_discovery_errors: tuple[str, ...] = ()
        self.custom_metric_storage_error: str | None = None
        self._openmetrics_target_count = 0
        self._last_containers: tuple = ()
        self._custom_metric_drain_ticks = 0

        super().__init__()
        self._setup_workload_toolbar()
        self._setup_openmetrics_runtime()
        # Re-run once now that the OpenMetrics worker exists. Docker lifecycle
        # events already funnel through this same refresh path afterwards.
        self._refresh_container_list()

    def _setup_openmetrics_runtime(self) -> None:
        worker = OpenMetricsRuntimeWorker(
            OpenMetricsCollector(max_payload_bytes=4 * 1024 * 1024, timeout_s=5.0),
            max_idle_wait_s=0.5,
        )
        worker.start()
        self.openmetrics_worker = worker

        self.custom_metric_drain_timer = QTimer(self)
        self.custom_metric_drain_timer.setInterval(self.CUSTOM_METRIC_DRAIN_MS)
        self.custom_metric_drain_timer.timeout.connect(self._drain_custom_metrics)
        self.custom_metric_drain_timer.start()
        self._sync_openmetrics_targets(self._last_containers)

    def _refresh_container_list(self) -> None:
        """Refresh Docker Explorer and atomically refresh discovered checks."""
        containers = self.docker_monitor.list_containers()
        self._last_containers = tuple(containers)
        self.container_list.update_list(containers)
        if not containers:
            detail = self.docker_monitor.last_error or "未發現容器"
            self.statusBar().showMessage(f"Docker: {detail}")
        if self.openmetrics_worker is not None:
            self._sync_openmetrics_targets(self._last_containers)

    def _sync_openmetrics_targets(self, containers) -> None:
        worker = self.openmetrics_worker
        if worker is None:
            return
        resolved = resolve_openmetrics_for_containers(containers)
        self.openmetrics_discovery_errors = resolved.errors
        self._openmetrics_target_count = len(resolved.targets)
        try:
            worker.replace_targets(resolved.targets)
        except Exception as exc:
            self.openmetrics_discovery_errors = (*resolved.errors, str(exc))
        self._refresh_custom_metric_action()

    def _drain_custom_metrics(self) -> None:
        worker = self.openmetrics_worker
        if worker is None:
            return

        # SQLite was opened on the Qt/main thread. Keep all DB writes on that
        # same thread; the background worker performs only blocking scrape I/O
        # and bounded buffering.
        store = self.telemetry_store
        if store is not None:
            points = worker.drain_points(self.CUSTOM_METRIC_DRAIN_BATCH)
            if points:
                try:
                    store.append_custom_metrics(points)
                    self.custom_metric_storage_error = None
                except Exception as exc:
                    # Preserve points for a later attempt. Requeue itself is
                    # bounded and may drop the oldest data under sustained DB
                    # failure rather than growing process memory indefinitely.
                    worker.requeue_points(points)
                    self.custom_metric_storage_error = str(exc)

            self._custom_metric_drain_ticks += 1
            if (
                self._custom_metric_drain_ticks
                % self.CUSTOM_METRIC_RETENTION_EVERY_TICKS
                == 0
            ):
                try:
                    store.prune_custom_metrics(
                        before_timestamp=time.time() - self.CUSTOM_METRIC_RETENTION_S,
                        max_rows=self.CUSTOM_METRIC_RETENTION_ROWS,
                    )
                except Exception as exc:
                    self.custom_metric_storage_error = str(exc)

        self._refresh_custom_metric_action()

    def _setup_workload_toolbar(self) -> None:
        toolbar = QToolBar("Container Explorer", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.workload_action = QAction("進程 / 日誌", self)
        self.workload_action.setToolTip("查看所選容器的進程和 bounded 日誌快照")
        self.workload_action.setEnabled(bool(self.selected_container_id))
        self.workload_action.triggered.connect(self._show_workload_explorer)
        toolbar.addAction(self.workload_action)

        self.custom_metric_action = QAction("自訂指標", self)
        self.custom_metric_action.setToolTip(
            "Docker label Autodiscovery → background OpenMetrics scrape → bounded buffer → SQLite"
        )
        self.custom_metric_action.setEnabled(self.telemetry_store is not None)
        self.custom_metric_action.triggered.connect(self._show_custom_metrics)
        toolbar.addAction(self.custom_metric_action)

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
        self._refresh_custom_metric_action()

        self.container_list.container_selected.connect(
            lambda container_id: self.workload_action.setEnabled(bool(container_id))
        )

    def _refresh_custom_metric_action(self) -> None:
        action = getattr(self, "custom_metric_action", None)
        if action is None:
            return
        worker = self.openmetrics_worker
        if worker is None:
            action.setText("自訂指標 0 checks")
            return
        snapshot = worker.snapshot()
        failed = sum(1 for health in snapshot.target_health if health.last_error)
        degraded = bool(
            self.openmetrics_discovery_errors
            or self.custom_metric_storage_error
            or snapshot.last_error
            or failed
        )
        text = (
            f"自訂指標 {self._openmetrics_target_count} checks / "
            f"{snapshot.buffer.queued_points} queued / "
            f"{snapshot.buffer.dropped_points} dropped"
        )
        if degraded:
            text += " / degraded"
        action.setText(text)
        details = []
        if self.openmetrics_discovery_errors:
            details.append(
                "discovery: " + "; ".join(self.openmetrics_discovery_errors[:3])
            )
        if snapshot.last_error:
            details.append("runtime: " + snapshot.last_error)
        if failed:
            details.append(f"failed checks: {failed}")
        if self.custom_metric_storage_error:
            details.append("storage: " + self.custom_metric_storage_error)
        if details:
            action.setToolTip("\n".join(details))

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

    def _show_custom_metrics(self) -> None:
        if self.telemetry_store is None:
            QMessageBox.warning(self, "Custom Metrics Explorer", "本機 SQLite 資料庫不可用")
            return
        CustomMetricsExplorerDialog(self.telemetry_store, self).exec()

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

    def closeEvent(self, event: QCloseEvent) -> None:
        timer = getattr(self, "custom_metric_drain_timer", None)
        if timer is not None:
            timer.stop()
        worker = self.openmetrics_worker
        self.openmetrics_worker = None
        if worker is not None:
            worker.stop(timeout_s=6.0)
        super().closeEvent(event)
