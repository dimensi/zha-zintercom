# XYZROE / DIYRuZ DIY_Zintercom for ZHA

[![Tests](https://github.com/dimensi/zha-zintercom/actions/workflows/tests.yml/badge.svg)](https://github.com/dimensi/zha-zintercom/actions/workflows/tests.yml)

Custom device handler (quirk) for the Zigbee intercom adapter `DIY_Zintercom`.
It runs inside Home Assistant's existing ZHA integration. This is a manual
custom quirk, not a HACS integration or an accepted upstream release.

## Features

- Read-only call states: Idle, Ring, Talk, Open and Drop.
- Incoming-call and opening-signal binary sensors; existing OnOff call sensor
  identity is retained.
- Native Never, Once, Always and Drop modes, plus handset sound control.
- Four timing controls (1–32 seconds) and reporting interval (1–255 minutes).
- Standard ZHA battery percentage and voltage; unknown 0xff measurements are
  not shown as a charge level or as 25.5 volts.
- Missing server clusters are added without losing Basic or the OnOff client.
- Correct uint8 timer encoding, immediate state reporting and initial reads.
- An additional battery read after Idle reports, with rate limiting and no
  changes to the opening mode.

## Install

1. Back up any existing custom handler for this model. Keep only one active
   custom handler for the model in your custom-quirks directory.
2. For a battery-powered adapter, copy **`queued_quirk/diyruz_zintercom.py`**
   into `/config/custom_zha_quirks/` on Home Assistant. This extended variant
   accepts configuration changes while the board is asleep. For protocol-only
   support, use `custom_zha_quirks/diyruz_zintercom.py` instead. Install only one
   variant, under that same filename; do not copy both into the active directory.
3. Merge the following into your existing `configuration.yaml`. Do not create
   a second `zha:` section if you already have one:

   ```yaml
   zha:
     custom_quirks_path: /config/custom_zha_quirks
   ```

4. Reload ZHA. The tested versions reload custom quirks this way. If your
   version does not, restart Home Assistant after checking the configuration.
5. Open the device page and check the quirk is applied and the new entities
   are available. Keep the existing Zigbee pairing; re-pairing was not needed
   in the tested installation.

## Persistent configuration queue (extended variant)

Changes from the existing ZHA number controls and handset-sound switch are
saved before HA reports success, then delivered at the next genuine Idle
report from the adapter. The queue supports all five timers and sound. It
keeps the latest request per setting, survives a restart and confirms each
value with a fresh device read. One failed field does not block the others.

The existing controls continue to show **actual device values**. A diagnostic
**Pending settings** sensor shows the remaining count; its `status` text
lists requested values and any retry error. **Cancel pending settings** cancels
unsent requests. An already-transmitted Zigbee packet cannot be recalled.

The opening mode remains an immediate command with normal ZHA transport
behavior; it is **never stored in this durable queue**. Once/Always/Drop and
emergency Never are intentionally excluded from replay after a later call.
Use an HA call-policy automation for an intentional future one-shot pass.

Storage is atomic JSON next to the configured Zigbee database, isolated by
its database filename, device IEEE and endpoint. No hardcoded HA directory
or device address is required. An absent/in-memory/URI database or a storage
failure produces an explicit queue error. Corrupt data is preserved for
recovery; other intercom functionality remains available.

A short Idle receive window, low-priority single attempts, per-setting
versions, cancellation and round-robin scheduling avoid background retry
loops and give incoming calls priority. Battery removal is not needed to
apply a pending setting. Read [the queue design](docs/settings-queue.md) for
semantics, limits and the proposed upstream architecture.

The protocol-only variant has no persistent queue: a configuration request
sent while the board sleeps can time out. Configure it while naturally awake.

## Opening behavior

`Once` allows one incoming call to be answered/opened. `Always` allows every
incoming call; `Never` leaves automatic opening disabled. These settings are
native firmware behavior. This device does not implement the standard Zigbee
DoorLock unlock command. An opening signal is not proof of door position.

A Home Assistant automation can request Once during Ring while the board is
awake. Voice assistants, HomeKit permission state and actionable phone pushes
are application-level features and are not included in this core handler.

Do not turn off handset sound merely to silence notifications: the battery
firmware's hardware behavior can increase current consumption in that mode.
See the manufacturer's firmware documentation.

## Compatibility and validation

Tested with Home Assistant 2026.11.2, ZHA 2.4.3, zigpy 2.4.1,
zha-quirks 2.4.1 and Python 3.14. Older versions are not verified.

Physical tests used xyzroe / DIY_Zintercom battery firmware reporting
`15/06/2021 19:24`: incoming call, speech, handset-button opening, automatic
Once opening, return to Never, timer write/readback and 100% / 3.2 V battery
measurement. A one-second talk-to-open interval was observed on a real call.
The DIYRuZ manufacturer alias and descriptor variants are covered by offline
tests; they have not been tested on a second physical board.

The 41 offline tests use real ZHA/zigpy classes and simulated controllers.
They cover the base protocol and entities, actual report-frame decoding,
battery handling, all six queued settings, UI setters, real ZHA restart,
confirmation, stale write acknowledgements, concurrent edits, cancellation
inside disk and transport operations, storage failures, fairness and priority
for incoming calls. They do not contact a physical coordinator.

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m unittest discover -s tests -v
```

## Upstream contribution

The intended upstream is [zigpy/zha-device-handlers](https://github.com/zigpy/zha-device-handlers).
The original support request is [#3763](https://github.com/zigpy/zha-device-handlers/issues/3763).
Upstream support is proposed in [PR #5781](https://github.com/zigpy/zha-device-handlers/pull/5781).
That PR includes an anonymized physical-device diagnostics fixture, 19
device-specific pytest cases and validation of the complete upstream suite
(5265 passed, 2 expected failures), plus all pre-commit checks. It is pending
review; acceptance and release timing depend on the maintainers. Until it
ships in Home Assistant, the manual installation above remains available.

The extended queue is maintained here separately from that base-support PR.
Local quirks take precedence over the built-in handler for this device, so
upstream acceptance does not remove the extended version's features. Existing
entity IDs remain the same. To return to built-in support, cancel pending
settings, remove the local handler and reload ZHA. Keep the local extension
updated: its override does not automatically inherit future changes to the
built-in handler.

Protocol references:

- [Firmware and attributes](https://github.com/xyzroe/Zintercom/tree/5b2db2f803adb25d47cef93e337922eb1e34eefe).
- [Battery implementation](https://github.com/diyruz/zstack-lib/blob/47c4672dfe82d73b0d8c13b2d6bf8766aa56450d/bettery.c).
- [ZHA documentation](https://www.home-assistant.io/integrations/zha/).

Apache-2.0 license. This package contains no live device identifiers,
credentials, phone registrations, HomeKit pairing data or home configuration.
