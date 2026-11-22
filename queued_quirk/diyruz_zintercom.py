"""ZHA support for xyzroe/DIYRuZ DIY_Zintercom firmware.

The firmware advertises its custom reporting cluster as an output cluster and
omits Power Configuration from its simple descriptor. Add their server sides
without discarding Basic or the OnOff client used for incoming-call events.

Protocol reference: xyzroe/Zintercom, commit
5b2db2f803adb25d47cef93e337922eb1e34eefe, Source/zcl_app_data.c.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Final

from zha.application.platforms import AttrConfig, ClusterConfig
from zha.application.platforms.button import BaseButton
from zha.application.platforms.number import NumberConfigurationEntity
from zha.application.platforms.sensor import BaseSensor, SensorState
from zha.application.platforms.switch import ConfigurableAttributeSwitch
from zha.exceptions import ZHAException
from zigpy.config import CONF_DATABASE
from zigpy.exceptions import ZigbeeException
import zigpy.types as t
from zigpy.zcl import (
    AttributeReportedEvent,
    AttributeUpdatedEvent,
    ClusterType,
    foundation,
)
from zigpy.zcl.clusters.general import OnOff, PowerConfiguration
from zigpy.zcl.foundation import BaseAttributeDefs, ZCLAttributeDef

from zhaquirks.builder import (
    BinarySensorDeviceClass,
    EntityPlatform,
    EntityType,
    QuirkBuilder,
    ReportingConfig,
    UnitOfTime,
)
from zhaquirks.builder.device import QuirkV2Device
from zhaquirks.clusters import CustomCluster


class ZintercomState(t.enum8):
    """Reported state of the intercom electronics, not a door-position sensor."""

    Idle = 0
    Ring = 1
    Talk = 2
    Open = 3
    Drop = 4


class ZintercomMode(t.enum8):
    """Automatic handling of an incoming call."""

    Never = 0
    Once = 1
    Always = 2
    Drop = 3


class DIYZintercomDoorLockCluster(CustomCluster):
    """The firmware's custom attributes share the DoorLock cluster ID."""

    cluster_id = 0x0101
    ep_attribute = "zintercom"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize wake-read bookkeeping."""
        self._battery_task: asyncio.Task | None = None
        self._battery_last_attempt: float | None = None
        self._battery_last_success: float | None = None
        super().__init__(*args, **kwargs)

    def _refresh_battery_when_awake(self) -> None:
        """Read battery measurements when an Idle report indicates a wake window."""
        power = self.endpoint.in_clusters.get(PowerConfiguration.cluster_id)
        if power is None or (
            self._battery_task is not None and not self._battery_task.done()
        ):
            return
        now = time.monotonic()
        if (
            self._battery_last_attempt is not None
            and now - self._battery_last_attempt < 300
        ):
            return
        percentage = power.get("battery_percentage_remaining")
        if (
            percentage is not None
            and percentage != 0xFF
            and self._battery_last_success is not None
            and now - self._battery_last_success < 21600
        ):
            return
        self._battery_last_attempt = now
        self._battery_task = asyncio.create_task(
            self._read_awake_battery(power), name="zintercom-battery"
        )

    async def _read_awake_battery(self, power: PowerConfiguration) -> None:
        """Refresh real measurements without writing any intercom attributes."""
        try:
            # RingEnd updates the measurement in the same firmware task.
            async with asyncio.timeout(0.6):
                success, _ = await power.read_attributes(
                    ["battery_voltage", "battery_percentage_remaining"],
                    allow_cache=False,
                    retries=0,
                    priority=t.PacketPriority.LOW,
                )
            percentage = success.get("battery_percentage_remaining")
            if percentage is not None and 0 <= percentage <= 200:
                self._battery_last_success = time.monotonic()
        except (ZigbeeException, TimeoutError):
            # A sleepy device may already be asleep again. Retry on a later
            # natural Idle report, subject to the cooldown; never wake/reset it.
            self.debug("Battery refresh missed the wake window")

    class AttributeDefs(BaseAttributeDefs):
        """Use the exact ZCL types declared by the firmware."""

        state: Final = ZCLAttributeDef(
            id=0x0050,
            type=ZintercomState,
            access="rp",
            manufacturer_code=None,
        )
        mode: Final = ZCLAttributeDef(
            id=0x0051,
            type=ZintercomMode,
            access="rwp",
            manufacturer_code=None,
        )
        sound: Final = ZCLAttributeDef(
            id=0x0052,
            type=t.Bool,
            access="rwp",
            manufacturer_code=None,
        )
        time_ring: Final = ZCLAttributeDef(
            id=0x0053,
            type=t.uint8_t,
            access="rw",
            manufacturer_code=None,
        )
        time_talk: Final = ZCLAttributeDef(
            id=0x0054,
            type=t.uint8_t,
            access="rw",
            manufacturer_code=None,
        )
        time_open: Final = ZCLAttributeDef(
            id=0x0055,
            type=t.uint8_t,
            access="rw",
            manufacturer_code=None,
        )
        time_report: Final = ZCLAttributeDef(
            id=0x0056,
            type=t.uint8_t,
            access="rw",
            manufacturer_code=None,
        )
        time_bell: Final = ZCLAttributeDef(
            id=0x0057,
            type=t.uint8_t,
            access="rw",
            manufacturer_code=None,
        )


class DIYZintercomPowerConfigurationCluster(CustomCluster, PowerConfiguration):
    """Treat the firmware's uninitialised battery measurements as unknown."""

    def get(self, key: str | int, default: Any = None) -> Any:
        """Normalize unknown measurements, including values restored from cache."""
        # Quirk replacement restores the old attribute cache directly, bypassing
        # _update_attribute. Normalise on read so both restored and fresh values
        # use the same unknown-value semantics without changing wire data.
        value = super().get(key, default)
        if (
            self.find_attribute(key).id
            in (
                PowerConfiguration.AttributeDefs.battery_voltage.id,
                PowerConfiguration.AttributeDefs.battery_percentage_remaining.id,
            )
            and value == 0xFF
        ):
            return None
        return value


SETTING_LIMITS = {
    "time_ring": (1, 32),
    "time_talk": (1, 32),
    "time_open": (1, 32),
    "time_bell": (1, 32),
    "time_report": (1, 255),
    "sound": (0, 1),
}
WAKE_WINDOW = 2.0
OPERATION_TIMEOUT = 0.65


class DeferredSettings:
    """Persist configuration intent separately from confirmed Zigbee attributes.

    There is one owner per ZHA device. Only six idempotent configuration
    attributes are accepted; opening modes never pass through this queue.
    """

    def __init__(self, cluster: DIYZintercomDoorLockCluster) -> None:
        """Initialize configuration state and lifecycle bookkeeping."""
        self.cluster = cluster
        self.ieee = str(cluster.endpoint.device.ieee)
        self.endpoint_id = cluster.endpoint.endpoint_id
        database = cluster.endpoint.device.application.config.get(CONF_DATABASE)
        self.path = None
        if (
            isinstance(database, str)
            and database
            and database != ":memory:"
            and not database.startswith("file:")
        ):
            db_path = Path(database).expanduser().absolute()
            self.path = (
                db_path.parent
                / (db_path.name + ".zintercom")
                / f"{self.ieee}-{self.endpoint_id}.json"
            )
        self.pending: dict[str, dict[str, Any]] = {}
        self.confirmed: dict[str, dict[str, Any]] = {}
        self.revision = 0
        self.storage_error: str | None = None
        self.errors: dict[str, str] = {}
        self._cursor = 0
        self._saved_cursor = 0
        self._radio_task: asyncio.Task | None = None
        self.loaded = False
        self.closed = False
        self.deadline = 0.0
        self.epoch = 0
        self.state = ZintercomState.Idle
        self.worker: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._listeners: set[Any] = set()
        self._inflight: str | None = None

    @property
    def last_error(self):
        """Return a storage error or the remaining per-setting failures."""
        return self.storage_error or ("; ".join(self.errors.values()) or None)

    def listen(self, callback):
        """Subscribe a diagnostic entity to queue changes."""
        self._listeners.add(callback)
        return lambda: self._listeners.discard(callback)

    def _notify(self) -> None:
        for callback in tuple(self._listeners):
            callback()

    @staticmethod
    def validate(name: str, value: Any) -> int:
        """Reject opening commands, fractional values and invalid ranges."""
        if name not in SETTING_LIMITS:
            raise ValueError("Only timer and handset-sound settings can be queued")
        if not isinstance(value, (int, float)):
            raise TypeError("The setting must be a whole number")
        low, high = SETTING_LIMITS[name]
        if not low <= value <= high or int(value) != value:
            raise ValueError(f"{name} must be a whole number between {low} and {high}")
        return int(value)

    def _document(self, pending, confirmed, revision):
        return {
            "version": 1,
            "ieee": self.ieee,
            "endpoint_id": self.endpoint_id,
            "revision": revision,
            "next_attribute": self._cursor,
            "pending": pending,
            "confirmed": confirmed,
        }

    @staticmethod
    def _atomic_save(path: Path, document: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=path.parent, prefix=".pending-", delete=False
            ) as handle:
                temp_path = Path(handle.name)
                os.chmod(temp_path, 0o600)
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            # Atomic rename plus directory sync protects the accepted intent.
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    async def _save_locked(self, pending, confirmed, revision) -> None:
        if self.path is None:
            raise OSError(
                "A filesystem Zigbee database is required for durable settings"
            )
        previous = self.pending
        document = self._document(pending, confirmed, revision)
        task = asyncio.create_task(
            asyncio.to_thread(self._atomic_save, self.path, document)
        )
        was_cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                was_cancelled = True
                if task.done():
                    task.result()
                    break
        self.pending, self.confirmed, self.revision = pending, confirmed, revision
        self._saved_cursor = document["next_attribute"]
        self.storage_error = None
        self.errors = {
            name: error
            for name, error in self.errors.items()
            if name in pending and previous.get(name) == pending.get(name)
        }
        if (
            self._inflight is not None
            and previous.get(self._inflight) != pending.get(self._inflight)
            and self._radio_task is not None
        ):
            self._radio_task.cancel()
        self._notify()
        if was_cancelled:
            raise asyncio.CancelledError

    async def _load_locked(self) -> None:
        if self.loaded:
            return
        if self.path is None:
            raise OSError(
                "A filesystem Zigbee database is required for durable settings"
            )
        try:
            text = await asyncio.to_thread(self.path.read_text)
        except FileNotFoundError:
            document = self._document({}, {}, 0)
        else:
            document = json.loads(text)
        if not isinstance(document, dict):
            raise TypeError("Settings store must be an object")
        if (
            document.get("version") != 1
            or document.get("ieee") != self.ieee
            or document.get("endpoint_id") != self.endpoint_id
        ):
            raise ValueError("Settings store does not match this device")
        revision = document["revision"]
        if type(revision) is not int or revision < 0:
            raise ValueError("Invalid settings-store revision")
        pending = document["pending"]
        confirmed = document.get("confirmed", {})
        if not isinstance(pending, dict) or not isinstance(confirmed, dict):
            raise TypeError("Invalid settings-store contents")
        for name, item in pending.items():
            if not isinstance(item, dict) or not isinstance(
                item.get("requested_at"), str
            ):
                raise TypeError("Invalid pending-setting record")
            self.validate(name, item["value"])
            if (
                type(item.get("revision")) is not int
                or not 0 < item["revision"] <= revision
            ):
                raise ValueError("Invalid pending-setting revision")
        for name, item in confirmed.items():
            if not isinstance(item, dict) or not isinstance(
                item.get("confirmed_at"), str
            ):
                raise TypeError("Invalid confirmation record")
            self.validate(name, item["value"])
        cursor = document.get("next_attribute", 0)
        if type(cursor) is not int or not 0 <= cursor < len(SETTING_LIMITS):
            raise ValueError("Invalid settings cursor")
        self.pending, self.confirmed, self.revision = pending, confirmed, revision
        self._cursor = self._saved_cursor = cursor
        self.loaded = True

    async def load(self) -> bool:
        """Restore pending settings without blocking other intercom features."""
        try:
            async with self._lock:
                await self._load_locked()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.storage_error = f"Storage unavailable: {exc}"
            self._notify()
            return False
        self.storage_error = None
        self._notify()
        return True

    async def enqueue(self, name: str, value: Any) -> None:
        """Accept only after durable persistence; latest request wins per field."""
        value = self.validate(name, value)
        if self.closed:
            raise ZHAException("The settings queue is shutting down")
        try:
            async with self._lock:
                if self.closed:
                    raise ZHAException("The settings queue is shutting down")
                await self._load_locked()
                pending = dict(self.pending)
                revision = self.revision + 1
                pending[name] = {
                    "value": value,
                    "revision": revision,
                    "requested_at": datetime.now(UTC).isoformat(),
                }
                await self._save_locked(pending, self.confirmed, revision)
                self.pending, self.revision = pending, revision
                self.storage_error = None
                self.errors.pop(name, None)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.storage_error = f"Could not save setting: {exc}"
            self._notify()
            raise ZHAException(self.storage_error) from exc
        self._notify()
        self._start_worker()

    async def cancel(self) -> None:
        """Cancel unsent intent; packets already transmitted cannot be recalled."""
        try:
            async with self._lock:
                if self.closed:
                    raise ZHAException("The settings queue is shutting down")
                await self._load_locked()
                revision = self.revision + 1
                await self._save_locked({}, self.confirmed, revision)
                self.pending, self.revision = {}, revision
                self.storage_error = None
                self.errors.clear()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.storage_error = f"Could not save cancellation: {exc}"
            self._notify()
            raise ZHAException(self.storage_error) from exc
        self._notify()

    def suspend(self) -> None:
        """Revoke the wake window before any subsequent configuration request."""
        self.deadline = 0
        self.epoch += 1
        if self._radio_task is not None:
            self._radio_task.cancel()
        battery = self.cluster._battery_task
        if battery is not None:
            battery.cancel()

    def report(self, event: AttributeReportedEvent) -> None:
        """Only an actual Idle report, including an unchanged one, opens a window."""
        if event.attribute_id != self.cluster.AttributeDefs.state.id:
            return
        self.state = event.value
        if self.state != ZintercomState.Idle:
            self.suspend()
            return
        if self.closed:
            return
        if self.worker is not None and not self.worker.done():
            if self.deadline <= time.monotonic():
                self.epoch += 1
                self.deadline = time.monotonic() + WAKE_WINDOW
            return
        self.epoch += 1
        self.deadline = time.monotonic() + WAKE_WINDOW
        self._start_worker()

    def _start_worker(self) -> None:
        if (
            self.closed
            or self.state != ZintercomState.Idle
            or time.monotonic() >= self.deadline
        ):
            return
        if self.worker is not None and not self.worker.done():
            return
        self.worker = asyncio.create_task(
            self._drain(self.epoch), name="zintercom-settings"
        )

    def _awake(self, epoch: int) -> bool:
        return (
            not self.closed
            and epoch == self.epoch
            and self.state == ZintercomState.Idle
            and time.monotonic() < self.deadline
        )

    async def _operation(self, epoch: int, factory):
        if not self._awake(epoch):
            raise TimeoutError("The intercom wake window ended")
        task = asyncio.create_task(factory())
        self._radio_task = task
        try:
            async with asyncio.timeout(
                min(OPERATION_TIMEOUT, self.deadline - time.monotonic())
            ):
                try:
                    return await task
                except asyncio.CancelledError:
                    if self.closed or asyncio.current_task().cancelling():
                        raise
                    raise TimeoutError(
                        "Configuration suspended for an incoming call"
                    ) from None
        finally:
            self._radio_task = None

    async def _acknowledge(self, name: str, entry: dict, actual: int) -> None:
        async with self._lock:
            if self.closed or self.pending.get(name) != entry:
                return
            pending = dict(self.pending)
            del pending[name]
            confirmed = {
                **self.confirmed,
                name: {"value": actual, "confirmed_at": datetime.now(UTC).isoformat()},
            }
            await self._save_locked(pending, confirmed, self.revision)
            self.pending, self.confirmed = pending, confirmed
            self.storage_error = None
            self.errors.pop(name, None)
        self._notify()

    async def _drain(self, epoch: int) -> None:
        try:
            if not await self.load():
                return
            attempted = set()
            names = list(SETTING_LIMITS)
            while self._awake(epoch):
                ordered = names[self._cursor :] + names[: self._cursor]
                remaining = [
                    (name, self.pending[name])
                    for name in ordered
                    if name in self.pending
                    and (name, self.pending[name]["revision"]) not in attempted
                ]
                if not remaining:
                    break
                name, entry = remaining[0]
                attempted.add((name, entry["revision"]))
                self._cursor = (names.index(name) + 1) % len(names)
                self._inflight = name
                self._notify()
                try:
                    success, _ = await self._operation(
                        epoch,
                        lambda name=name: self.cluster.read_attributes(
                            [name],
                            allow_cache=False,
                            retries=0,
                            priority=t.PacketPriority.LOW,
                        ),
                    )
                    actual = success.get(name)
                    async with self._lock:
                        current = self.pending.get(name) == entry
                    if not current:
                        continue
                    if actual != entry["value"]:
                        response = await self._operation(
                            epoch,
                            lambda name=name, value=entry["value"]: (
                                self.cluster.write_attributes(
                                    {name: value},
                                    update_cache=False,
                                    retries=0,
                                    priority=t.PacketPriority.LOW,
                                )
                            ),
                        )
                        if any(
                            record.status != foundation.Status.SUCCESS
                            for record in response[0]
                        ):
                            raise ValueError(f"Device rejected {name}")
                        success, _ = await self._operation(
                            epoch,
                            lambda name=name: self.cluster.read_attributes(
                                [name],
                                allow_cache=False,
                                retries=0,
                                priority=t.PacketPriority.LOW,
                            ),
                        )
                        actual = success.get(name)
                    if actual != entry["value"]:
                        raise ValueError(f"Device did not confirm {name}")
                    await self._acknowledge(name, entry, int(actual))
                except (ZigbeeException, TimeoutError, OSError, ValueError) as exc:
                    if self.pending.get(name) == entry:
                        self.errors[name] = f"{name}: {type(exc).__name__}: {exc}"
                        self._notify()
                finally:
                    self._inflight = None
            if self.loaded and not self.closed and self._cursor != self._saved_cursor:
                try:
                    async with self._lock:
                        await self._save_locked(
                            self.pending, self.confirmed, self.revision
                        )
                except OSError as exc:
                    self.storage_error = f"Could not save queue progress: {exc}"
        finally:
            self._notify()
            if not self.closed and epoch != self.epoch:
                asyncio.get_running_loop().call_soon(self._start_worker)
            elif self._awake(epoch):
                self.cluster._refresh_battery_when_awake()

    async def close(self) -> None:
        """Revoke callbacks and await all work before a new device owner is bound."""
        self.closed = True
        self.suspend()
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        battery = self.cluster._battery_task
        if battery is not None:
            battery.cancel()
            await asyncio.gather(battery, return_exceptions=True)
        async with self._lock:
            pass
        self._listeners.clear()


class DeferredZintercomDevice(QuirkV2Device):
    """Own the settings queue for the complete ZHA device lifecycle."""

    def _init_from_zigpy_device(self, zigpy_device) -> None:
        super()._init_from_zigpy_device(zigpy_device)
        cluster = zigpy_device.endpoints[1].zintercom
        self.settings_queue = DeferredSettings(cluster)
        self._on_remove_callbacks.append(
            cluster.on_event(
                AttributeReportedEvent.event_type, self.settings_queue.report
            )
        )
        call_cluster = zigpy_device.endpoints[1].out_clusters.get(OnOff.cluster_id)
        if call_cluster is not None:

            def incoming(event):
                if event.attribute_id == OnOff.AttributeDefs.on_off.id and event.value:
                    self.settings_queue.suspend()

            self._on_remove_callbacks.append(
                call_cluster.on_event(AttributeUpdatedEvent.event_type, incoming)
            )

    async def async_initialize(self, from_cache: bool = False) -> None:
        """Restore intent before normal ZHA initialization."""
        await self.settings_queue.load()
        await super().async_initialize(from_cache=from_cache)

    async def async_teardown(self, *, emit_entity_events: bool) -> None:
        """Finish durable writes and revoke work before removing the device."""
        await self.settings_queue.close()
        await super().async_teardown(emit_entity_events=emit_entity_events)


class DeferredNumber(NumberConfigurationEntity):
    """Keep the displayed number factual, but save UI writes for the next wake."""

    def __init__(self, *args, attribute_name: str, **kwargs) -> None:
        """Initialize configuration state and lifecycle bookkeeping."""
        self._server_cluster_config = {
            DIYZintercomDoorLockCluster.cluster_id: ClusterConfig(
                attributes={attribute_name: AttrConfig(read_on_startup=True)}
            )
        }
        super().__init__(*args, attribute_name=attribute_name, **kwargs)

    @property
    def available(self) -> bool:
        """Keep configuration available even when the radio is sleeping."""
        return not self._device.settings_queue.closed

    async def async_set_native_value(self, value: float) -> None:
        """Persist the requested number without changing the confirmed value."""
        await self._device.settings_queue.enqueue(self._attribute_name, value)


class DeferredSound(ConfigurableAttributeSwitch):
    """Queue the handset-sound preference without synthesizing a device ACK."""

    def __init__(self, *args, attribute_name: str, **kwargs) -> None:
        """Initialize configuration state and lifecycle bookkeeping."""
        self._server_cluster_config = {
            DIYZintercomDoorLockCluster.cluster_id: ClusterConfig(
                bind=True,
                attributes={
                    attribute_name: AttrConfig(
                        read_on_startup=True, reporting=CALL_REPORTING
                    )
                },
            )
        }
        super().__init__(*args, attribute_name=attribute_name, **kwargs)

    @property
    def available(self) -> bool:
        """Keep configuration available even when the radio is sleeping."""
        return not self._device.settings_queue.closed

    async def async_turn_on_off(self, state: bool) -> None:
        """Persist the desired handset-sound setting."""
        await self._device.settings_queue.enqueue(self._attribute_name, int(state))


@dataclass(frozen=True, kw_only=True)
class SettingsQueueState(SensorState):
    """Diagnostic data forwarded by Home Assistant's ZHA sensor wrapper."""

    status: str


class SettingsQueueSensor(BaseSensor):
    """Show pending values and confirmations next to the existing controls."""

    _attr_extra_state_attribute_names = frozenset({"status"})

    def on_add(self) -> None:
        """Subscribe diagnostics to changes in the settings queue."""
        super().on_add()
        self._on_remove_callbacks.append(
            self._device.settings_queue.listen(self.maybe_emit_state_changed_event)
        )

    @property
    def available(self) -> bool:
        """Keep configuration available even when the radio is sleeping."""
        return not self._device.settings_queue.closed

    @property
    def native_value(self) -> int:
        """Return the number of settings still awaiting confirmation."""
        return len(self._device.settings_queue.pending)

    @property
    def state(self) -> SettingsQueueState:
        """Expose a concise status supported by Home Assistant sensor wrappers."""
        queue = self._device.settings_queue
        if queue.last_error:
            status = "error"
        elif queue._inflight:
            status = "applying"
        else:
            status = "waiting" if queue.pending else "idle"
        labels = {
            "time_ring": "Answer delay",
            "time_talk": "Opening delay",
            "time_open": "Opening pulse",
            "time_bell": "Call end",
            "time_report": "Report interval",
            "sound": "Handset sound",
        }
        details = []
        for name, entry in queue.pending.items():
            value = (
                ("on" if entry["value"] else "off")
                if name == "sound"
                else f"{entry['value']} {'min' if name == 'time_report' else 's'}"
            )
            details.append(f"{labels[name]}: {value}")
        text = {
            "error": "Retry pending",
            "applying": "Applying",
            "waiting": "Waiting for wake",
            "idle": "No pending settings",
        }[status]
        if details:
            text += " · " + "; ".join(details)
        if queue.last_error:
            text += " · " + queue.last_error[: max(0, 252 - len(text))]
        return SettingsQueueState(**super().state.__dict__, status=text[:255])


class CancelSettingsQueue(BaseButton):
    """Cancel queued configuration requests without sending a Zigbee command."""

    @property
    def available(self) -> bool:
        """Keep configuration available even when the radio is sleeping."""
        return not self._device.settings_queue.closed

    async def async_press(self) -> None:
        """Persist cancellation without replaying opening actions."""
        await self._device.settings_queue.cancel()


CALL_REPORTING = ReportingConfig(min_interval=0, max_interval=3600, reportable_change=0)

builder = (
    QuirkBuilder("xyzroe", "DIY_Zintercom")
    .also_applies_to("DIYRuZ", "DIY_Zintercom")
    .zha_device_class(DeferredZintercomDevice)
    .replaces(DIYZintercomDoorLockCluster)
    .replaces(DIYZintercomPowerConfigurationCluster)
    # Preserve the old binary sensor's unique ID, but describe what it measures:
    # the firmware sends On for an active call and Off when the call ends.
    .change_entity_metadata(
        endpoint_id=1,
        cluster_id=OnOff.cluster_id,
        cluster_type=ClusterType.Client,
        function=lambda entity: entity.PLATFORM.value == "binary_sensor",
        new_translation_key="call_active",
        new_device_class=BinarySensorDeviceClass.SOUND,
        new_fallback_name="Call active",
    )
    .enum(
        "state",
        ZintercomState,
        DIYZintercomDoorLockCluster.cluster_id,
        entity_platform=EntityPlatform.SENSOR,
        entity_type=EntityType.STANDARD,
        attribute_initialized_from_cache=False,
        reporting_config=CALL_REPORTING,
        translation_key="state",
        fallback_name="Call state",
    )
    .binary_sensor(
        "state",
        DIYZintercomDoorLockCluster.cluster_id,
        entity_type=EntityType.STANDARD,
        device_class=BinarySensorDeviceClass.SOUND,
        attribute_initialized_from_cache=False,
        attribute_converter=lambda value: value == ZintercomState.Ring,
        reporting_config=CALL_REPORTING,
        unique_id_suffix="ringing",
        translation_key="ringing",
        fallback_name="Incoming call",
    )
    .binary_sensor(
        "state",
        DIYZintercomDoorLockCluster.cluster_id,
        entity_type=EntityType.STANDARD,
        attribute_initialized_from_cache=False,
        attribute_converter=lambda value: value == ZintercomState.Open,
        reporting_config=CALL_REPORTING,
        unique_id_suffix="opening_signal",
        translation_key="opening_signal",
        fallback_name="Opening signal",
    )
    .enum(
        "mode",
        ZintercomMode,
        DIYZintercomDoorLockCluster.cluster_id,
        attribute_initialized_from_cache=False,
        reporting_config=CALL_REPORTING,
        translation_key="mode",
        fallback_name="Mode",
    )
    .adds_entity(
        DeferredSound,
        endpoint_id=1,
        cluster_id=DIYZintercomDoorLockCluster.cluster_id,
        attribute_name="sound",
        unique_id_suffix="sound",
        entity_type=EntityType.CONFIG,
        force_create=True,
        translation_key="sound",
        fallback_name="Handset sound",
    )
)

# Timers are uint8 on the wire. The four second-based timers are multiplied by
# 1000 before widening to uint32 in the CC2530 firmware (16-bit int). Keep writes
# within 32 seconds to avoid overflowing that intermediate calculation.
for attribute, name in (
    ("time_ring", "Delay before answering"),
    ("time_talk", "Delay before opening"),
    ("time_open", "Opening signal duration"),
    ("time_bell", "Call end timeout"),
):
    builder.adds_entity(
        DeferredNumber,
        endpoint_id=1,
        cluster_id=DIYZintercomDoorLockCluster.cluster_id,
        attribute_name=attribute,
        unique_id_suffix=attribute,
        entity_type=EntityType.CONFIG,
        force_create=True,
        min_value=1,
        max_value=32,
        step=1,
        unit=UnitOfTime.SECONDS,
        translation_key=attribute,
        fallback_name=name,
    )

builder.adds_entity(
    DeferredNumber,
    endpoint_id=1,
    cluster_id=DIYZintercomDoorLockCluster.cluster_id,
    attribute_name="time_report",
    unique_id_suffix="time_report",
    entity_type=EntityType.CONFIG,
    force_create=True,
    min_value=1,
    max_value=255,
    step=1,
    unit=UnitOfTime.MINUTES,
    translation_key="time_report",
    fallback_name="Reporting interval",
)

builder.adds_entity(
    SettingsQueueSensor,
    unique_id_suffix="pending_settings",
    entity_type=EntityType.DIAGNOSTIC,
    fallback_name="Pending settings",
    translation_key="zintercom_pending_settings",
)
builder.adds_entity(
    CancelSettingsQueue,
    unique_id_suffix="cancel_pending_settings",
    entity_type=EntityType.CONFIG,
    fallback_name="Cancel pending settings",
    translation_key="zintercom_cancel_pending_settings",
)

ZINTERCOM_QUIRK = builder.add_to_registry()
