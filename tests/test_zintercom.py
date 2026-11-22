"""Offline protocol tests. No coordinator, network, or physical door is used."""

import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_zha_quirks"))

from diyruz_zintercom import (
    CALL_REPORTING,
    DIYZintercomDoorLockCluster,
    ZINTERCOM_QUIRK,
    ZintercomMode,
    ZintercomState,
)
from zha.quirks import DeviceRegistry
from zha.zigbee.cluster_config import aggregate_cluster_configs
from zhaquirks.builder import BinarySensorDeviceClass, EntityPlatform
from zigpy.application import ControllerApplication
from zigpy.device import Device
from zigpy.exceptions import DeliveryError
from zigpy.profiles import zha
import zigpy.state
import zigpy.types as t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import Basic, OnOff, PowerConfiguration
from zigpy.zdo.types import NodeDescriptor


def make_device(manufacturer="xyzroe", extra_servers=()):
    app = MagicMock(spec=ControllerApplication)
    app.state = zigpy.state.State()
    app._dblistener = None
    app.register_callback_listener.return_value = lambda: None
    device = Device(app, t.EUI64.convert("00:11:22:33:44:55:66:77"), t.NWK(0x1927))
    device.manufacturer = manufacturer
    device.model = "DIY_Zintercom"
    device.node_desc = NodeDescriptor(2, 64, 128, 0, 80, 160, 0, 160, 0)
    ep = device.add_endpoint(1)
    ep.profile_id = zha.PROFILE_ID
    ep.device_type = zha.DeviceType.SIMPLE_SENSOR
    for cluster_id in (Basic.cluster_id, *extra_servers):
        ep.add_input_cluster(cluster_id)
    ep.add_output_cluster(OnOff.cluster_id)
    ep.add_output_cluster(0x0101)
    return device


class ZintercomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = DeviceRegistry()
        self.registry.register(ZINTERCOM_QUIRK)
        self.raw = make_device()
        self.device = self.registry.resolve(self.raw)
        self.cluster = self.device.endpoints[1].in_clusters[0x0101]
        self.definition = ZINTERCOM_QUIRK.zha_device_factory.quirk_definition
        self.metadata = self.definition.entity_metadata

    async def test_original_sparse_signature_matches_and_preserves_raw_device(self):
        self.assertIsNot(self.device, self.raw)
        self.assertIsInstance(self.cluster, DIYZintercomDoorLockCluster)
        self.assertEqual(set(self.raw.endpoints[1].in_clusters), {0})
        self.assertEqual(set(self.device.endpoints[1].in_clusters), {0, 1, 0x0101})
        self.assertEqual(set(self.device.endpoints[1].out_clusters), {6, 0x0101})
        self.assertIsInstance(self.device.endpoints[1].in_clusters[0], Basic)
        self.assertIsInstance(self.device.endpoints[1].out_clusters[6], OnOff)

    async def test_diyruz_name_and_complete_signature_match(self):
        for manufacturer in ["xyzroe", "DIYRuZ"]:
            with self.subTest(manufacturer=manufacturer):
                device = self.registry.resolve(make_device(manufacturer, (1, 0x0101)))
                self.assertIsInstance(
                    device.endpoints[1].in_clusters[0x0101], DIYZintercomDoorLockCluster
                )
                self.assertEqual(set(device.endpoints[1].in_clusters), {0, 1, 0x0101})

    async def test_unrelated_device_is_not_changed(self):
        other = make_device("other manufacturer")
        self.assertIs(self.registry.resolve(other), other)

    async def test_read_only_state_has_no_control_entity_or_standard_unlock(self):
        state_entities = [
            m for m in self.metadata if getattr(m, "attribute_name", None) == "state"
        ]
        self.assertEqual(len(state_entities), 3)
        self.assertEqual(
            {m.entity_platform for m in state_entities},
            {EntityPlatform.SENSOR, EntityPlatform.BINARY_SENSOR},
        )
        attr = self.cluster.AttributeDefs.state
        self.assertFalse(attr.access & foundation.ZCLAttributeAccess.Write)
        self.assertFalse(self.cluster.server_commands)
        self.assertEqual(self.cluster.ep_attribute, "zintercom")
        self.assertFalse(self.definition.disabled_default_entities)

    async def test_state_mode_sound_report_without_hour_long_minimum(self):
        for name in ["state", "mode", "sound"]:
            for metadata in [
                m for m in self.metadata if getattr(m, "attribute_name", None) == name
            ]:
                self.assertEqual(metadata.reporting_config, CALL_REPORTING)
                self.assertEqual(metadata.reporting_config.min_interval, 0)
                self.assertEqual(metadata.reporting_config.max_interval, 3600)

    async def test_timer_limits_units_and_initial_reads(self):
        for name in ["time_ring", "time_talk", "time_open", "time_bell", "time_report"]:
            attr = self.cluster.attributes_by_name[name]
            self.assertIs(attr.type, t.uint8_t)
            self.assertFalse(attr.is_manufacturer_specific)
            metadata = next(
                m for m in self.metadata if getattr(m, "attribute_name", None) == name
            )
            self.assertFalse(metadata.attribute_initialized_from_cache)
            self.assertIsNone(metadata.reporting_config)
            self.assertEqual(metadata.min, 1)
            self.assertEqual(metadata.max, 255 if name == "time_report" else 32)
            self.assertEqual(metadata.unit, "min" if name == "time_report" else "s")

    async def test_real_report_frame_updates_state_and_binary_converters(self):
        # Server->client, foundation Report Attributes, enum8 state=Ring,
        # enum8 mode=Never and boolean sound=On, exactly as the firmware sends.
        frame = bytes.fromhex("18 01 0a 50 00 30 01 51 00 30 00 52 00 10 01")
        header, command = self.cluster.deserialize(frame)
        self.cluster.handle_message(header, command)
        await asyncio.sleep(0)
        self.assertEqual(self.cluster.get("state"), ZintercomState.Ring)
        self.assertEqual(self.cluster.get("mode"), ZintercomMode.Never)
        self.assertEqual(self.cluster.get("sound"), t.Bool.true)
        ringing = next(m for m in self.metadata if m.unique_id_suffix == "ringing")
        opening = next(
            m for m in self.metadata if m.unique_id_suffix == "opening_signal"
        )
        self.assertTrue(ringing.attribute_converter(self.cluster.get("state")))
        self.assertFalse(opening.attribute_converter(self.cluster.get("state")))
        self.cluster.update_attribute(0x0050, ZintercomState.Open)
        self.assertFalse(ringing.attribute_converter(self.cluster.get("state")))
        self.assertTrue(opening.attribute_converter(self.cluster.get("state")))

    async def test_timer_write_uses_one_byte_without_manufacturer_header(self):
        self.cluster._write_attributes = AsyncMock(
            return_value=[
                [foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS)]
            ]
        )
        await self.cluster.write_attributes({"time_ring": 7})
        call = self.cluster._write_attributes.call_args
        record = call.args[0][0]
        self.assertEqual(record.attrid, 0x0053)
        self.assertEqual(record.value.type, foundation.DataTypeId.uint8)
        self.assertEqual(record.value.value.serialize(), b"\x07")
        self.assertIsNone(call.kwargs.get("manufacturer"))

    async def test_battery_uses_standard_cluster_and_no_duplicate_sensor(self):
        power = self.device.endpoints[1].in_clusters[1]
        self.assertIsInstance(power, PowerConfiguration)
        self.assertFalse(any(m.cluster_id == 1 for m in self.metadata))
        header, command = power.deserialize(
            bytes.fromhex("18 02 0a 21 00 20 64 20 00 20 1e")
        )
        power.handle_message(header, command)
        await asyncio.sleep(0)
        self.assertEqual(power.get("battery_percentage_remaining"), 100)
        self.assertEqual(power.get("battery_voltage"), 30)

    async def test_idle_report_refreshes_battery_once_without_writing_modes(self):
        power = self.device.endpoints[1].in_clusters[1]
        power.read_attributes = AsyncMock(
            return_value=(
                {"battery_voltage": 32, "battery_percentage_remaining": 200},
                {},
            )
        )
        self.cluster.write_attributes = AsyncMock()
        self.cluster.update_attribute(0x50, ZintercomState.Idle)
        self.cluster.update_attribute(0x50, ZintercomState.Idle)
        await asyncio.sleep(0.08)
        power.read_attributes.assert_awaited_once_with(
            ["battery_voltage", "battery_percentage_remaining"], allow_cache=False
        )
        self.cluster.write_attributes.assert_not_called()
        self.assertIsNotNone(self.cluster._battery_last_success)
        self.cluster.update_attribute(0x50, ZintercomState.Idle)
        await asyncio.sleep(0.08)
        self.assertEqual(power.read_attributes.await_count, 1)

    async def test_active_call_does_not_trigger_battery_reads(self):
        power = self.device.endpoints[1].in_clusters[1]
        power.read_attributes = AsyncMock()
        for state in (ZintercomState.Ring, ZintercomState.Talk, ZintercomState.Open):
            self.cluster.update_attribute(0x50, state)
        await asyncio.sleep(0.08)
        power.read_attributes.assert_not_called()
        self.assertEqual(self.cluster.get("state"), ZintercomState.Open)

    async def test_sleeping_battery_read_failure_does_not_break_idle(self):
        power = self.device.endpoints[1].in_clusters[1]
        power.read_attributes = AsyncMock(side_effect=DeliveryError("asleep"))
        self.cluster.update_attribute(0x50, ZintercomState.Idle)
        await asyncio.sleep(0.08)
        self.assertFalse(self.cluster._battery_read_pending)
        self.assertIsNone(self.cluster._battery_last_success)
        self.assertEqual(self.cluster.get("state"), ZintercomState.Idle)

    async def test_successful_battery_refresh_is_throttled_for_six_hours(self):
        import time

        power = self.device.endpoints[1].in_clusters[1]
        power.update_attribute(0x21, 200)
        power.read_attributes = AsyncMock()
        self.cluster._battery_last_success = time.monotonic()
        self.cluster._battery_last_attempt = time.monotonic() - 301
        self.cluster.update_attribute(0x50, ZintercomState.Idle)
        await asyncio.sleep(0.08)
        power.read_attributes.assert_not_called()

    async def test_unknown_battery_restored_from_database_is_not_25_volts(self):
        raw = make_device(extra_servers=(1,))
        raw.endpoints[1].in_clusters[1].update_attribute(0x20, 0xFF)
        raw.endpoints[1].in_clusters[1].update_attribute(0x21, 0xFF)
        resolved = self.registry.resolve(raw)
        power = resolved.endpoints[1].in_clusters[1]
        self.assertIsNone(power.get("battery_voltage"))
        self.assertIsNone(power.get("battery_percentage_remaining"))
        gateway = MagicMock()
        gateway.config.config.device_options.consider_unavailable_battery = 21000
        gateway.config.config.device_options.consider_unavailable_mains = 21000
        gateway.config.config.device_overrides = {}
        device = ZINTERCOM_QUIRK.zha_device_factory(resolved, gateway)
        device._discover_new_entities()
        battery = next(
            e for e in device._pending_entities if type(e).__name__ == "Battery"
        )
        self.assertIsNone(battery.state.battery_voltage)
        self.assertIsNone(battery.state.native_value)

    async def test_actual_zha_entity_discovery_and_battery_scaling(self):
        gateway = MagicMock()
        gateway.config.config.device_options.consider_unavailable_battery = 21000
        gateway.config.config.device_options.consider_unavailable_mains = 21000
        gateway.config.config.device_overrides = {}
        device = ZINTERCOM_QUIRK.zha_device_factory(self.device, gateway)
        device._discover_new_entities()
        entities = device._pending_entities
        custom = [
            e
            for e in entities
            if getattr(e, "_attribute_name", None) in self.cluster.attributes_by_name
        ]
        self.assertEqual(len(custom), 10)
        battery = next(e for e in entities if type(e).__name__ == "Battery")
        power = self.device.endpoints[1].in_clusters[1]
        power.update_attribute(0x21, 150)
        power.update_attribute(0x20, 30)
        self.assertEqual(battery.state.native_value, 75)
        power.update_attribute(0x21, 0xFF)
        power.update_attribute(0x20, 0xFF)
        self.assertIsNone(battery.state.native_value)
        self.assertIsNone(power.get("battery_voltage"))
        self.assertFalse(any(e.PLATFORM.value == "lock" for e in entities))
        call = next(e for e in entities if type(e).__name__ == "Opening")
        self.assertEqual(call._attr_device_class, BinarySensorDeviceClass.SOUND)
        self.assertEqual(call._attr_fallback_name, "Call active")
        self.assertEqual(call.unique_id, "00:11:22:33:44:55:66:77-1-6")
        configs = aggregate_cluster_configs(entities)
        config = configs[(1, 0x0101, True)]
        self.assertTrue(config.bind)
        self.assertEqual(set(config.attributes), set(self.cluster.attributes_by_name))
        for name in ["state", "mode", "sound"]:
            self.assertEqual(config.attributes[name].reporting.min_interval, 0)
        for name in ["time_ring", "time_talk", "time_open", "time_bell", "time_report"]:
            self.assertTrue(config.attributes[name].read_on_startup)
            self.assertIsNone(config.attributes[name].reporting)


if __name__ == "__main__":
    unittest.main()
