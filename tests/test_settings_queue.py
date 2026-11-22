"""Persistent settings queue tests: no physical radio or door commands."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
from _queue_helpers import join_device_from_diagnostics, zha_gateway
from test_zintercom import make_device
from zha.application import Platform
from zha.exceptions import ZHAException
from zha.quirks import DeviceRegistry
from zha.zigbee.cluster_config import aggregate_cluster_configs
from zigpy.config import CONF_DATABASE
from zigpy.exceptions import DeliveryError
from zigpy.zcl import AttributeReportedEvent, ClusterType, foundation

SPEC = importlib.util.spec_from_file_location(
    "queued_zintercom",
    Path(__file__).resolve().parents[1] / "queued_quirk/diyruz_zintercom.py",
)
queued = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = queued
SPEC.loader.exec_module(queued)


def idle_event(cluster, state=0):
    """Build the typed event emitted by a genuine ZCL attribute report."""
    return AttributeReportedEvent(
        device_ieee=str(cluster.endpoint.device.ieee),
        endpoint_id=1,
        cluster_type=ClusterType.Server,
        cluster_id=cluster.cluster_id,
        attribute_name="state",
        attribute_id=80,
        manufacturer_code=None,
        raw_value=state,
        value=state,
    )


class QueueTests(unittest.IsolatedAsyncioTestCase):
    """Exercise persistence and concurrent state changes at the wire boundary."""

    async def asyncSetUp(self):
        """Create an isolated durable store and simulated device."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        raw = make_device()
        raw.application.config = {
            CONF_DATABASE: str(Path(self.directory.name) / "zigbee.db")
        }
        registry = DeviceRegistry()
        registry.register(queued.ZINTERCOM_QUIRK)
        self.device = registry.resolve(raw)
        self.cluster = self.device.endpoints[1].zintercom
        self.cluster._refresh_battery_when_awake = MagicMock()
        self.queue = queued.DeferredSettings(self.cluster)
        self.addAsyncCleanup(self.queue.close)
        self.actual = dict.fromkeys(queued.SETTING_LIMITS, 3)
        self.actual.update(time_report=60, sound=1)
        self.writes = []
        for name, value in self.actual.items():
            self.cluster.update_attribute(
                self.cluster.attributes_by_name[name].id, value
            )

        async def read(names, **kwargs):
            self.assertFalse(kwargs["allow_cache"])
            for name in names:
                self.cluster.update_attribute(
                    self.cluster.attributes_by_name[name].id, self.actual[name]
                )
            return {name: self.actual[name] for name in names}, {}

        async def write(values, **kwargs):
            self.assertIs(kwargs.get("update_cache"), False)
            self.assertEqual(kwargs.get("retries"), 0)
            self.writes.append(dict(values))
            self.actual.update(values)
            return [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS)]]

        self.cluster.read_attributes = AsyncMock(side_effect=read)
        self.cluster.write_attributes = AsyncMock(side_effect=write)

    async def wake(self):
        """Report a real Idle event and await this receive window."""
        self.queue.report(idle_event(self.cluster))
        if self.queue.worker:
            await self.queue.worker

    async def test_accepts_all_six_settings_without_sending_while_asleep(self):
        """Persist UI intents without pretending the cached attributes changed."""
        wanted = {
            "time_ring": 2,
            "time_talk": 1,
            "time_open": 4,
            "time_bell": 5,
            "time_report": 45,
            "sound": 0,
        }
        for name, value in wanted.items():
            await self.queue.enqueue(name, value)
        self.assertEqual({n: e["value"] for n, e in self.queue.pending.items()}, wanted)
        self.cluster.read_attributes.assert_not_called()
        self.cluster.write_attributes.assert_not_called()
        self.assertEqual(self.cluster.get("time_talk"), 3)
        saved = json.loads(self.queue.path.read_text())
        self.assertEqual(len(saved["pending"]), 6)
        await self.wake()
        self.assertEqual(self.actual, wanted)
        self.assertFalse(self.queue.pending)
        self.assertEqual(len(json.loads(self.queue.path.read_text())["confirmed"]), 6)

    async def test_latest_edit_wins_and_restores_after_restart(self):
        """Keep only the latest value for each field across device recreation."""
        await self.queue.enqueue("time_talk", 2)
        await self.queue.enqueue("time_talk", 1)
        restored = queued.DeferredSettings(self.cluster)
        self.addAsyncCleanup(restored.close)
        self.assertTrue(await restored.load())
        self.assertEqual(restored.pending["time_talk"]["value"], 1)
        restored.report(idle_event(self.cluster))
        await restored.worker
        self.assertEqual(self.writes, [{"time_talk": 1}])

    async def test_uncertain_write_is_read_before_any_retry(self):
        """Recover an applied write whose acknowledgement was lost."""
        await self.queue.enqueue("time_talk", 1)

        async def lost_response(values, **kwargs):
            self.actual.update(values)
            self.writes.append(dict(values))
            raise DeliveryError("lost acknowledgement")

        self.cluster.write_attributes.side_effect = lost_response
        await self.wake()
        self.assertIn("time_talk", self.queue.pending)
        restored = queued.DeferredSettings(self.cluster)
        self.addAsyncCleanup(restored.close)
        restored.report(idle_event(self.cluster))
        await restored.worker
        self.assertFalse(restored.pending)
        self.assertEqual(self.writes, [{"time_talk": 1}])

    async def test_failed_read_keeps_request_and_other_fields_progress(self):
        """A bad attribute does not discard or block unrelated desired settings."""
        await self.queue.enqueue("time_talk", 1)
        await self.queue.enqueue("sound", 0)
        normal = self.cluster.read_attributes.side_effect

        async def partial(names, **kwargs):
            if names == ["time_talk"]:
                raise DeliveryError("unavailable")
            return await normal(names, **kwargs)

        self.cluster.read_attributes.side_effect = partial
        await self.wake()
        self.assertEqual(set(self.queue.pending), {"time_talk"})
        self.assertEqual(self.actual["sound"], 0)

    async def test_cancel_before_write_sends_nothing(self):
        """Persist cancellation so the request cannot reappear after restart."""
        await self.queue.enqueue("time_talk", 1)
        await self.queue.cancel()
        await self.wake()
        self.assertFalse(self.writes)
        restored = queued.DeferredSettings(self.cluster)
        self.addAsyncCleanup(restored.close)
        await restored.load()
        self.assertFalse(restored.pending)

    async def test_cancel_during_read_prevents_write(self):
        """Cancellation is rechecked after the asynchronous read."""
        await self.queue.enqueue("time_talk", 1)
        started, release = asyncio.Event(), asyncio.Event()
        normal = self.cluster.read_attributes.side_effect

        async def delayed(names, **kwargs):
            started.set()
            await release.wait()
            return await normal(names, **kwargs)

        self.cluster.read_attributes.side_effect = delayed
        self.queue.report(idle_event(self.cluster))
        await started.wait()
        await self.queue.cancel()
        release.set()
        await self.queue.worker
        self.assertFalse(self.writes)

    async def test_replacement_during_write_is_not_cleared_by_old_confirmation(self):
        """An in-flight version cannot acknowledge a newer UI edit."""
        await self.queue.enqueue("time_talk", 1)
        started, release = asyncio.Event(), asyncio.Event()
        normal = self.cluster.write_attributes.side_effect

        async def delayed(values, **kwargs):
            started.set()
            await release.wait()
            return await normal(values, **kwargs)

        self.cluster.write_attributes.side_effect = delayed
        self.queue.report(idle_event(self.cluster))
        await started.wait()
        await self.queue.enqueue("time_talk", 2)
        release.set()
        await self.queue.worker
        if self.queue.pending:
            self.assertEqual(self.queue.pending["time_talk"]["value"], 2)
        else:
            self.assertEqual(self.actual["time_talk"], 2)

    async def test_ring_between_read_and_write_revokes_wake(self):
        """Real-time opening gets priority over configuration traffic."""
        await self.queue.enqueue("time_talk", 1)
        normal = self.cluster.read_attributes.side_effect

        async def ring_while_reading(names, **kwargs):
            self.queue.report(idle_event(self.cluster, state=1))
            return await normal(names, **kwargs)

        self.cluster.read_attributes.side_effect = ring_while_reading
        await self.wake()
        self.assertFalse(self.writes)
        self.assertIn("time_talk", self.queue.pending)

    async def test_repeated_idle_does_not_start_parallel_workers(self):
        """Repeated and post-write Idle reports coalesce into one attempt."""
        await self.queue.enqueue("time_talk", 1)
        for _ in range(5):
            self.queue.report(idle_event(self.cluster))
        await self.queue.worker
        self.assertEqual(self.writes, [{"time_talk": 1}])

    async def test_cache_update_and_expired_window_do_not_flush(self):
        """A cached Idle and an old last-seen timestamp cannot wake the worker."""
        self.cluster.update_attribute(80, 0)
        await self.queue.enqueue("time_talk", 1)
        self.queue.deadline = 0
        await self.queue.enqueue("time_ring", 2)
        await asyncio.sleep(0)
        self.assertIsNone(self.queue.worker)
        self.assertFalse(self.writes)

    async def test_disk_failure_does_not_report_acceptance(self):
        """The previous on-disk and in-memory request survives an ENOSPC error."""
        await self.queue.enqueue("time_talk", 2)
        saved = self.queue.path.read_bytes()
        with (
            patch.object(
                self.queue, "_atomic_save", side_effect=OSError("No space left")
            ),
            self.assertRaises(ZHAException),
        ):
            await self.queue.enqueue("time_talk", 1)
        self.assertEqual(self.queue.path.read_bytes(), saved)
        self.assertEqual(self.queue.pending["time_talk"]["value"], 2)

    async def test_corrupt_store_is_not_silently_overwritten(self):
        """Show corruption while preserving the file for recovery."""
        self.queue.path.parent.mkdir(parents=True)
        self.queue.path.write_text("{broken")
        with self.assertRaises(ZHAException):
            await self.queue.enqueue("time_talk", 1)
        self.assertEqual(self.queue.path.read_text(), "{broken")
        self.assertFalse(self.queue.pending)

    async def test_modes_and_invalid_values_never_enter_the_queue(self):
        """Reject delayed opening modes and out-of-range configuration."""
        for name, value in [
            ("mode", 1),
            ("state", 0),
            ("time_talk", 0),
            ("time_ring", 33),
            ("time_report", 256),
            ("sound", 2),
            ("time_talk", 1.5),
        ]:
            with self.assertRaises(ValueError):
                await self.queue.enqueue(name, value)
        self.assertFalse(self.queue.pending)
        self.assertFalse(self.writes)

    async def test_close_preserves_pending_and_prevents_future_sends(self):
        """Teardown leaves durable intent but no running sender on the old device."""
        await self.queue.enqueue("time_talk", 1)
        await self.queue.close()
        self.queue.report(idle_event(self.cluster))
        self.assertFalse(self.writes)
        self.assertTrue(self.queue.path.exists())
        with self.assertRaises(ZHAException):
            await self.queue.enqueue("time_ring", 2)

    async def test_repeated_cancellation_during_save_keeps_memory_and_disk_equal(self):
        """Finish a durable commit despite two cancellation requests."""
        entered, release = threading.Event(), threading.Event()
        save = self.queue._atomic_save

        def blocked(path, document):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test release timed out")
            save(path, document)

        with patch.object(self.queue, "_atomic_save", side_effect=blocked):
            task = asyncio.create_task(self.queue.enqueue("time_talk", 1))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(
            self.queue.pending, json.loads(self.queue.path.read_text())["pending"]
        )
        await self.queue.enqueue("sound", 0)
        self.assertEqual(
            set(json.loads(self.queue.path.read_text())["pending"]),
            {"time_talk", "sound"},
        )

    async def test_cancelled_cancellation_does_not_resurrect_old_intent(self):
        """A committed cancellation remains canceled after caller cancellation."""
        await self.queue.enqueue("time_talk", 1)
        entered, release = threading.Event(), threading.Event()
        save = self.queue._atomic_save

        def blocked(path, document):
            entered.set()
            release.wait(3)
            save(path, document)

        with patch.object(self.queue, "_atomic_save", side_effect=blocked):
            task = asyncio.create_task(self.queue.cancel())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.queue.pending)
        await self.queue.enqueue("sound", 0)
        self.assertEqual(
            set(json.loads(self.queue.path.read_text())["pending"]), {"sound"}
        )

    async def test_cancel_cancels_unsent_transport_work(self):
        """A write still waiting for a transport slot must not send after cancel."""
        await self.queue.enqueue("time_talk", 1)
        entered, release = asyncio.Event(), asyncio.Event()
        normal = self.cluster.write_attributes.side_effect

        async def waiting_for_transport(values, **kwargs):
            entered.set()
            await release.wait()
            return await normal(values, **kwargs)

        self.cluster.write_attributes.side_effect = waiting_for_transport
        self.queue.report(idle_event(self.cluster))
        await entered.wait()
        await self.queue.cancel()
        release.set()
        await self.queue.worker
        self.assertFalse(self.writes)
        self.assertIsNone(self.queue.last_error)

    async def test_new_idle_during_old_worker_cleanup_is_not_lost(self):
        """Re-arm a fresh window after Ring even before the old read unwinds."""
        await self.queue.enqueue("time_talk", 1)
        entered = asyncio.Event()
        normal = self.cluster.read_attributes.side_effect
        calls = 0

        async def first_read_waits(names, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await asyncio.Event().wait()
            return await normal(names, **kwargs)

        self.cluster.read_attributes.side_effect = first_read_waits
        self.queue.report(idle_event(self.cluster))
        first_worker = self.queue.worker
        await entered.wait()
        self.queue.report(idle_event(self.cluster, state=1))
        self.queue.report(idle_event(self.cluster, state=0))
        await first_worker
        await asyncio.sleep(0)
        self.assertIsNot(self.queue.worker, first_worker)
        await self.queue.worker
        self.assertEqual(self.actual["time_talk"], 1)
        self.assertFalse(self.queue.pending)

    async def test_cancelled_cancel_still_revokes_unsent_radio_operation(self):
        """Post-commit effects survive cancellation of the cancel caller itself."""
        await self.queue.enqueue("time_talk", 1)
        radio_entered, radio_release = asyncio.Event(), asyncio.Event()
        normal_write = self.cluster.write_attributes.side_effect

        async def waiting(values, **kwargs):
            radio_entered.set()
            await radio_release.wait()
            return await normal_write(values, **kwargs)

        self.cluster.write_attributes.side_effect = waiting
        self.queue.report(idle_event(self.cluster))
        await radio_entered.wait()
        save_entered, save_release = threading.Event(), threading.Event()
        save = self.queue._atomic_save

        def blocked(path, document):
            save_entered.set()
            save_release.wait(3)
            save(path, document)

        with patch.object(self.queue, "_atomic_save", side_effect=blocked):
            task = asyncio.create_task(self.queue.cancel())
            self.assertTrue(await asyncio.to_thread(save_entered.wait, 2))
            task.cancel()
            save_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        radio_release.set()
        await self.queue.worker
        self.assertFalse(self.queue.pending)
        self.assertFalse(self.writes)
        self.assertIsNone(self.queue.last_error)

    async def test_round_robin_progresses_past_fields_timing_out(self):
        """A reachable field at the tail eventually gets its own wake budget."""
        for name, value in [
            ("time_ring", 2),
            ("time_open", 2),
            ("time_report", 30),
            ("sound", 0),
        ]:
            await self.queue.enqueue(name, value)
        normal = self.cluster.read_attributes.side_effect

        async def bad_timer(names, **kwargs):
            if names != ["sound"]:
                await asyncio.sleep(1)
            return await normal(names, **kwargs)

        self.cluster.read_attributes.side_effect = bad_timer
        with (
            patch.object(queued, "WAKE_WINDOW", 0.05),
            patch.object(queued, "OPERATION_TIMEOUT", 0.02),
        ):
            for _ in range(3):
                await self.wake()
                if "sound" not in self.queue.pending:
                    break
        self.assertEqual(self.actual["sound"], 0)
        self.assertNotIn("sound", self.queue.pending)
        self.assertTrue(self.queue.last_error)

    async def test_no_periodic_disk_writes_when_nothing_is_pending(self):
        """Normal hourly Idle reports do not rewrite an empty queue file."""
        with patch.object(
            self.queue, "_atomic_save", wraps=self.queue._atomic_save
        ) as save:
            for _ in range(3):
                await self.wake()
            save.assert_not_called()

    async def test_malformed_store_does_not_escape_as_unexpected_exception(self):
        """Handle wrong roots, history and huge values as queue-only errors."""
        self.queue.path.parent.mkdir(parents=True)
        good = self.queue._document({}, {}, 0)
        documents = [
            [],
            None,
            {**good, "confirmed": {"sound": 42}},
            {
                **good,
                "revision": 1,
                "pending": {
                    "time_talk": {
                        "value": 10**400,
                        "revision": 1,
                        "requested_at": "now",
                    }
                },
            },
        ]
        for document in documents:
            self.queue.path.write_text(json.dumps(document))
            self.assertFalse(await self.queue.load())
            self.assertTrue(self.queue.last_error)

    async def test_canceling_battery_before_first_tick_does_not_wedge_reads(self):
        """A suspended battery task cannot leave a permanent pending boolean."""
        self.cluster._refresh_battery_when_awake = (
            queued.DIYZintercomDoorLockCluster._refresh_battery_when_awake.__get__(
                self.cluster
            )
        )
        power = self.device.endpoints[1].power
        power.read_attributes = AsyncMock(
            return_value=(
                {"battery_voltage": 32, "battery_percentage_remaining": 200},
                {},
            )
        )
        self.cluster._refresh_battery_when_awake()
        self.queue.suspend()
        await asyncio.sleep(0)
        self.cluster._battery_last_attempt = None
        self.cluster._refresh_battery_when_awake()
        await self.cluster._battery_task
        power.read_attributes.assert_awaited_once()


class QueueEntityTests(unittest.IsolatedAsyncioTestCase):
    """Exercise actual ZHA control setters and diagnostics without a real radio."""

    async def test_existing_controls_queue_and_diagnostics_expose_pending(self):
        """Preserve existing IDs, actual values and startup/reporting configuration."""
        registry = DeviceRegistry()
        registry.register(queued.ZINTERCOM_QUIRK)
        with tempfile.TemporaryDirectory() as directory:
            async with zha_gateway(str(Path(directory) / "zigbee.db")) as gateway:
                device = await join_device_from_diagnostics(
                    gateway, "xyzroe-diy-zintercom.json", registry
                )
                ieee = str(device.ieee)
                cluster = device.device.endpoints[1].zintercom
                cluster._refresh_battery_when_awake = MagicMock()
                cluster.read_attributes = AsyncMock(wraps=cluster.read_attributes)
                cluster.write_attributes = AsyncMock()
                number = device.get_platform_entity(
                    Platform.NUMBER, f"{ieee}-1-time_talk"
                )
                old = number.native_value
                await number.async_set_native_value(2)
                self.assertEqual(number.native_value, old)
                sound = device.get_platform_entity(Platform.SWITCH, f"{ieee}-1-sound")
                await sound.async_turn_off()
                sensor = device.get_platform_entity(
                    Platform.SENSOR, f"{ieee}-pending_settings"
                )
                self.assertEqual(sensor.native_value, 2)
                self.assertEqual(set(sensor.state.extra_state_attributes), {"status"})
                self.assertIn(
                    "Opening delay: 2 s", sensor.state.extra_state_attributes["status"]
                )
                self.assertIn(
                    "Waiting for wake", sensor.state.extra_state_attributes["status"]
                )
                cluster.write_attributes.assert_not_called()
                cluster.read_attributes.assert_not_called()
                for name in (
                    "time_ring",
                    "time_talk",
                    "time_open",
                    "time_bell",
                    "time_report",
                ):
                    entity = device.get_platform_entity(
                        Platform.NUMBER, f"{ieee}-1-{name}"
                    )
                    self.assertEqual(entity.native_min_value, 1)
                    self.assertEqual(
                        entity.native_max_value, 255 if name == "time_report" else 32
                    )
                config = aggregate_cluster_configs(
                    list(device.platform_entities.values())
                )[(1, 257, True)]
                self.assertTrue(config.bind)
                self.assertEqual(
                    set(config.attributes), set(cluster.attributes_by_name)
                )
                button = device.get_platform_entity(
                    Platform.BUTTON, f"{ieee}-cancel_pending_settings"
                )
                await button.async_press()
                self.assertEqual(sensor.native_value, 0)
                self.assertEqual(
                    device.get_platform_entity(
                        Platform.SELECT, f"{ieee}-1-mode"
                    ).current_option,
                    "Never",
                )

    async def test_genuine_report_flushes_existing_ui_request(self):
        """Connect UI intent to an actual ZCL report and a confirmed entity update."""
        registry = DeviceRegistry()
        registry.register(queued.ZINTERCOM_QUIRK)
        with tempfile.TemporaryDirectory() as directory:
            async with zha_gateway(str(Path(directory) / "zigbee.db")) as gateway:
                device = await join_device_from_diagnostics(
                    gateway, "xyzroe-diy-zintercom.json", registry
                )
                cluster = device.device.endpoints[1].zintercom
                cluster._refresh_battery_when_awake = MagicMock()
                actual = {"time_talk": cluster.get("time_talk")}

                async def read(names, **kwargs):
                    for name in names:
                        cluster.update_attribute(
                            cluster.attributes_by_name[name].id, actual[name]
                        )
                    return {name: actual[name] for name in names}, {}

                async def write(values, **kwargs):
                    actual.update(values)
                    return [
                        [
                            foundation.WriteAttributesStatusRecord(
                                foundation.Status.SUCCESS
                            )
                        ]
                    ]

                cluster.read_attributes = AsyncMock(side_effect=read)
                cluster.write_attributes = AsyncMock(side_effect=write)
                ieee = str(device.ieee)
                number = device.get_platform_entity(
                    Platform.NUMBER, f"{ieee}-1-time_talk"
                )
                await number.async_set_native_value(2)
                self.assertNotEqual(number.native_value, 2)
                header, command = cluster.deserialize(
                    bytes.fromhex("18 01 0a 50 00 30 00")
                )
                cluster.handle_message(header, command)
                self.assertIsNotNone(device.settings_queue.worker)
                await device.settings_queue.worker
                self.assertEqual(number.native_value, 2)
                self.assertFalse(device.settings_queue.pending)
                cluster.write_attributes.assert_awaited_once()
                self.assertEqual(
                    cluster.write_attributes.call_args.args[0], {"time_talk": 2}
                )

    async def test_zha_device_restart_restores_ui_intent_without_sending(self):
        """A fresh ZHA owner reloads pending settings but cached Idle cannot flush."""
        registry = DeviceRegistry()
        registry.register(queued.ZINTERCOM_QUIRK)
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "zigbee.db")
            async with zha_gateway(database) as gateway:
                device = await join_device_from_diagnostics(
                    gateway, "xyzroe-diy-zintercom.json", registry
                )
                await device.get_platform_entity(
                    Platform.NUMBER, f"{device.ieee}-1-time_talk"
                ).async_set_native_value(2)
                path = device.settings_queue.path
            self.assertEqual(
                json.loads(path.read_text())["pending"]["time_talk"]["value"], 2
            )
            async with zha_gateway(database) as gateway:
                restored = await join_device_from_diagnostics(
                    gateway, "xyzroe-diy-zintercom.json", registry
                )
                self.assertEqual(
                    restored.settings_queue.pending["time_talk"]["value"], 2
                )
                self.assertIsNone(restored.settings_queue.worker)
                self.assertEqual(
                    restored.get_platform_entity(
                        Platform.SENSOR, f"{restored.ieee}-pending_settings"
                    ).native_value,
                    1,
                )


if __name__ == "__main__":
    unittest.main()
