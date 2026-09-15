"""Application shell that composes agent-style Explorer features around MainWindow."""
from __future__ import annotations

import time

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction, QCloseEvent
from PyQt6.QtWidgets import QMessageBox, QToolBar

from ..core.alert_runtime import AlertRuntime
from ..core.alerting import AlertStatus
from ..core.forwarding import DurableDeliveryQueue, DeliveryWorker, HTTPDeliveryTransport
from ..core.forwarding_config import ForwardingConfig, ForwardingConfigError, load_forwarding_config
from ..core.host_monitor import HostRuntimeWorker
from ..core.metric_forwarding import enqueue_metric_points
from ..core.openmetrics import OpenMetricsCollector
from ..core.openmetrics_runtime import OpenMetricsRuntimeWorker, resolve_openmetrics_for_containers
from ..core.system_metrics import SystemMetricsRuntimeWorker
from ..utils.paths import (
    get_alert_config_path,
    get_forwarding_config_path,
    get_forwarding_db_path,
)
from .alert_history import AlertHistoryDialog
from .custom_metrics_explorer import CustomMetricsExplorerDialog
from .host_metrics_explorer import HostMetricsExplorerDialog
from .main_window import MainWindow
from .workload_explorer import WorkloadExplorerDialog


class AppMainWindow(MainWindow):
    """Full desktop window with agent-style host/container/check runtimes."""

    AGENT_DRAIN_MS = 500
    BUFFER_DRAIN_BATCH = 1000
    LOCAL_RETENTION_ROWS = 250_000
    LOCAL_RETENTION_S = 7 * 24 * 60 * 60
    RETENTION_EVERY_TICKS = 600  # five minutes at 500 ms

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

        self.host_worker: HostRuntimeWorker | None = None
        self.host_runtime_error: str | None = None
        self.host_storage_error: str | None = None
        self.system_metrics_worker: SystemMetricsRuntimeWorker | None = None
        self.system_metric_runtime_error: str | None = None
        self.system_metric_storage_error: str | None = None
        self._agent_drain_ticks = 0

        self.forwarding_config = ForwardingConfig()
        self.forwarding_config_error: str | None = None
        self.forwarding_queue: DurableDeliveryQueue | None = None
        self.delivery_worker: DeliveryWorker | None = None
        self.forwarding_enqueue_error: str | None = None
        self.forwarding_points_enqueued = 0
        self.forwarding_points_dropped = 0

        super().__init__()
        self._setup_workload_toolbar()
        self._setup_host_runtime()
        self._setup_system_metrics_runtime()
        self._setup_openmetrics_runtime()
        self._reload_forwarding(show_dialog=False)
        self._setup_agent_drain_timer()
        # Re-run once now that the OpenMetrics worker exists. Docker lifecycle
        # events already funnel through this same refresh path afterwards.
        self._refresh_container_list()

    def _setup_host_runtime(self) -> None:
        try:
            worker = HostRuntimeWorker(interval_s=1.0, max_queue=3600)
            worker.start()
            self.host_worker = worker
            self.host_runtime_error = None
        except Exception as exc:
            self.host_worker = None
            self.host_runtime_error = str(exc)
        self._refresh_host_action()

    def _setup_system_metrics_runtime(self) -> None:
        try:
            worker = SystemMetricsRuntimeWorker(
                interval_s=1.0,
                max_points=20_000,
                max_bytes=16 * 1024 * 1024,
            )
            worker.start()
            self.system_metrics_worker = worker
            self.system_metric_runtime_error = None
        except Exception as exc:
            self.system_metrics_worker = None
            self.system_metric_runtime_error = str(exc)
        self._refresh_host_action()

    def _setup_openmetrics_runtime(self) -> None:
        worker = OpenMetricsRuntimeWorker(
            OpenMetricsCollector(max_payload_bytes=4 * 1024 * 1024, timeout_s=5.0),
            max_idle_wait_s=0.5,
        )
        worker.start()
        self.openmetrics_worker = worker
        self._sync_openmetrics_targets(self._last_containers)

    def _setup_agent_drain_timer(self) -> None:
        self.agent_drain_timer = QTimer(self)
        self.agent_drain_timer.setInterval(self.AGENT_DRAIN_MS)
        self.agent_drain_timer.timeout.connect(self._drain_agent_buffers)
        self.agent_drain_timer.start()

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

    def _drain_agent_buffers(self) -> None:
        store = self.telemetry_store
        self._drain_custom_metrics(store)
        self._drain_system_metrics(store)
        self._drain_host_metrics(store)
        self._agent_drain_ticks += 1

        if store is not None and self._agent_drain_ticks % self.RETENTION_EVERY_TICKS == 0:
            cutoff = time.time() - self.LOCAL_RETENTION_S
            try:
                store.prune_custom_metrics(
                    before_timestamp=cutoff,
                    max_rows=self.LOCAL_RETENTION_ROWS,
                )
                store.prune_host_samples(
                    before_timestamp=cutoff,
                    max_rows=self.LOCAL_RETENTION_ROWS,
                )
            except Exception as exc:
                # Retention failure is visible but must not stop collection.
                self.custom_metric_storage_error = str(exc)

        self._refresh_custom_metric_action()
        self._refresh_host_action()
        self._refresh_forwarding_action()

    def _forward_metric_points(self, points) -> None:
        queue = self.forwarding_queue
        if not self.forwarding_config.enabled or queue is None or not points:
            return
        try:
            result = enqueue_metric_points(
                queue,
                points,
                max_points_per_batch=self.forwarding_config.batch_points,
                max_payload_bytes=self.forwarding_config.max_payload_bytes,
            )
            self.forwarding_points_enqueued += result.points_enqueued
            self.forwarding_points_dropped += result.points_dropped
            self.forwarding_enqueue_error = None
        except Exception as exc:
            # Local persistence has already succeeded. Forwarding failure is
            # independent and must never re-ingest duplicate local points.
            self.forwarding_enqueue_error = str(exc)

    def _drain_metric_worker(self, worker, store, *, error_attr: str) -> None:
        if worker is None or store is None:
            return
        points = worker.drain_points(self.BUFFER_DRAIN_BATCH)
        if not points:
            return
        try:
            store.append_custom_metrics(points)
            setattr(self, error_attr, None)
        except Exception as exc:
            worker.requeue_points(points)
            setattr(self, error_attr, str(exc))
            return
        self._forward_metric_points(points)

    def _drain_custom_metrics(self, store) -> None:
        self._drain_metric_worker(
            self.openmetrics_worker,
            store,
            error_attr="custom_metric_storage_error",
        )

    def _drain_system_metrics(self, store) -> None:
        self._drain_metric_worker(
            self.system_metrics_worker,
            store,
            error_attr="system_metric_storage_error",
        )

    def _drain_host_metrics(self, store) -> None:
        worker = self.host_worker
        if worker is None or store is None:
            return
        samples = worker.drain(self.BUFFER_DRAIN_BATCH)
        if not samples:
            return
        try:
            store.append_host_samples(samples)
            self.host_storage_error = None
        except Exception as exc:
            worker.requeue_front(samples)
            self.host_storage_error = str(exc)

    def _setup_workload_toolbar(self) -> None:
        toolbar = QToolBar("Container Explorer", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.host_action = QAction("Host", self)
        self.host_action.setToolTip(
            "Native host CPU/memory/uptime/network/disk I/O: Windows APIs 或 Linux /proc，不依賴 HWiNFO"
        )
        self.host_action.setEnabled(self.telemetry_store is not None)
        self.host_action.triggered.connect(self._show_host_metrics)
        toolbar.addAction(self.host_action)

        self.workload_action = QAction("進程 / 日誌", self)
        self.workload_action.setToolTip("查看所選容器的進程和 bounded 日誌快照")
        self.workload_action.setEnabled(bool(self.selected_container_id))
        self.workload_action.triggered.connect(self._show_workload_explorer)
        toolbar.addAction(self.workload_action)

        self.custom_metric_action = QAction("自訂指標", self)
        self.custom_metric_action.setToolTip(
            "OpenMetrics 與 native system metrics → bounded buffers → SQLite → optional forwarding"
        )
        self.custom_metric_action.setEnabled(self.telemetry_store is not None)
        self.custom_metric_action.triggered.connect(self._show_custom_metrics)
        toolbar.addAction(self.custom_metric_action)

        self.forwarding_action = QAction("Forwarding disabled", self)
        self.forwarding_action.setToolTip(
            f"遠端轉送預設停用。配置: {get_forwarding_config_path()}\n點擊重新載入配置"
        )
        self.forwarding_action.triggered.connect(self._reload_forwarding)
        toolbar.addAction(self.forwarding_action)

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
        self._refresh_host_action()
        self._refresh_forwarding_action()

        self.container_list.container_selected.connect(
            lambda container_id: self.workload_action.setEnabled(bool(container_id))
        )

    def _install_forwarding_config(self, config: ForwardingConfig) -> None:
        new_queue = None
        new_worker = None
        if config.enabled:
            new_queue = DurableDeliveryQueue(
                get_forwarding_db_path(),
                max_items=config.queue_max_items,
                max_bytes=config.queue_max_bytes,
                max_payload_bytes=config.max_payload_bytes,
            )
            transport = HTTPDeliveryTransport(
                config.endpoints,
                timeout_s=config.timeout_s,
                headers=config.header_mapping,
            )
            new_worker = DeliveryWorker(
                new_queue,
                transport,
                batch_size=100,
                poll_interval_s=config.poll_interval_s,
                base_delay_s=config.base_delay_s,
                max_delay_s=config.max_delay_s,
                jitter=config.jitter,
            )

        old_worker = self.delivery_worker
        old_queue = self.forwarding_queue
        if old_worker is not None:
            old_worker.stop(timeout_s=6.0)
        if old_queue is not None:
            old_queue.close()

        self.forwarding_config = config
        self.forwarding_queue = new_queue
        self.delivery_worker = new_worker
        if new_worker is not None:
            new_worker.start()

    def _reload_forwarding(self, checked: bool = False, *, show_dialog: bool = True) -> None:
        del checked
        try:
            config = load_forwarding_config(get_forwarding_config_path())
            # Build the new queue/worker before replacing the currently working
            # runtime. Invalid config therefore has last-known-good semantics.
            self._install_forwarding_config(config)
            self.forwarding_config_error = None
        except (ForwardingConfigError, OSError, ValueError) as exc:
            self.forwarding_config_error = str(exc)
            if show_dialog:
                QMessageBox.warning(
                    self,
                    "Forwarding 配置無效",
                    "已保留目前 forwarding runtime。\n\n" + self.forwarding_config_error,
                )
        self._refresh_forwarding_action()

    def _refresh_forwarding_action(self) -> None:
        action = getattr(self, "forwarding_action", None)
        if action is None:
            return
        if self.forwarding_config_error:
            action.setText("Forwarding config error")
            action.setToolTip(
                f"{self.forwarding_config_error}\n配置: {get_forwarding_config_path()}"
            )
            return
        worker = self.delivery_worker
        queue = self.forwarding_queue
        if not self.forwarding_config.enabled or worker is None or queue is None:
            action.setText("Forwarding disabled")
            action.setToolTip(
                f"遠端轉送預設停用。配置: {get_forwarding_config_path()}\n點擊重新載入配置"
            )
            return
        snapshot = worker.snapshot()
        oldest = snapshot.queue.oldest_age_s
        age_text = "-" if oldest is None else f"{oldest:.0f}s"
        action.setText(
            f"Forward {snapshot.queue.queued_items} queued / {snapshot.queue.dropped_items} dropped"
        )
        details = [
            f"spool: {snapshot.queue.queued_bytes} bytes; oldest {age_text}",
            f"delivered: {snapshot.queue.delivered_items}; failed attempts: {snapshot.queue.failed_attempts}",
            f"metric points enqueued: {self.forwarding_points_enqueued}; dropped before spool: {self.forwarding_points_dropped}",
            f"config: {get_forwarding_config_path()}",
        ]
        if snapshot.last_error:
            details.insert(0, "last error: " + snapshot.last_error)
        if self.forwarding_enqueue_error:
            details.insert(0, "enqueue: " + self.forwarding_enqueue_error)
        action.setToolTip("\n".join(details))

    def _refresh_host_action(self) -> None:
        action = getattr(self, "host_action", None)
        if action is None:
            return
        worker = self.host_worker
        system_worker = self.system_metrics_worker
        if worker is None:
            action.setText("Host unavailable")
            details = [self.host_runtime_error] if self.host_runtime_error else []
            if self.system_metric_runtime_error:
                details.append("I/O: " + self.system_metric_runtime_error)
            if details:
                action.setToolTip("\n".join(details))
            return
        snapshot = worker.snapshot()
        stats = snapshot.last_stats
        io_text = ""
        io_details: list[str] = []
        if system_worker is not None:
            io_snapshot = system_worker.snapshot()
            io_text = (
                f" / IO q {io_snapshot.buffer.queued_points} "
                f"drop {io_snapshot.buffer.dropped_points}"
            )
            if io_snapshot.last_error:
                io_details.append("I/O collector: " + io_snapshot.last_error)
            if io_snapshot.network_error:
                io_details.append("network: " + io_snapshot.network_error)
            if io_snapshot.disk_error:
                io_details.append("disk I/O: " + io_snapshot.disk_error)
        elif self.system_metric_runtime_error:
            io_details.append("I/O runtime: " + self.system_metric_runtime_error)

        if stats is None:
            action.setText(
                f"Host starting / {snapshot.queued_samples} queued / {snapshot.dropped_samples} dropped{io_text}"
            )
        else:
            cpu = "-" if stats.cpu_percent is None else f"{stats.cpu_percent:.1f}%"
            action.setText(
                f"Host CPU {cpu} / MEM {stats.memory_percent:.1f}% / {snapshot.dropped_samples} dropped{io_text}"
            )
        details = []
        if snapshot.last_error:
            details.append("collector: " + snapshot.last_error)
        if self.host_storage_error:
            details.append("storage: " + self.host_storage_error)
        if self.system_metric_storage_error:
            details.append("I/O storage: " + self.system_metric_storage_error)
        details.extend(io_details)
        if details:
            action.setToolTip("\n".join(details))

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
            f"{snapshot.buffer.queued_points} queued / {snapshot.buffer.dropped_points} dropped"
        )
        if degraded:
            text += " / degraded"
        action.setText(text)
        details = []
        if self.openmetrics_discovery_errors:
            details.append("discovery: " + "; ".join(self.openmetrics_discovery_errors[:3]))
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
                store.append_alert_event(event, session_id=session_id, container_id=container_id)
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

    def _show_host_metrics(self) -> None:
        if self.telemetry_store is None:
            QMessageBox.warning(self, "Host Metrics", "本機 SQLite 資料庫不可用")
            return
        HostMetricsExplorerDialog(self.telemetry_store, self).exec()

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
        timer = getattr(self, "agent_drain_timer", None)
        if timer is not None:
            timer.stop()

        delivery_worker = self.delivery_worker
        self.delivery_worker = None
        if delivery_worker is not None:
            delivery_worker.stop(timeout_s=6.0)
        queue = self.forwarding_queue
        self.forwarding_queue = None
        if queue is not None:
            queue.close()

        host_worker = self.host_worker
        self.host_worker = None
        if host_worker is not None:
            host_worker.stop(timeout_s=2.0)

        system_metrics_worker = self.system_metrics_worker
        self.system_metrics_worker = None
        if system_metrics_worker is not None:
            system_metrics_worker.stop(timeout_s=2.0)

        openmetrics_worker = self.openmetrics_worker
        self.openmetrics_worker = None
        if openmetrics_worker is not None:
            openmetrics_worker.stop(timeout_s=6.0)

        super().closeEvent(event)
