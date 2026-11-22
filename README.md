# XYZROE / DIYRuZ DIY_Zintercom for ZHA

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
2. Copy `custom_zha_quirks/diyruz_zintercom.py` into
   `/config/custom_zha_quirks/` on Home Assistant.
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

Battery-powered firmware sleeps. A timed-out configuration request is not
queued automatically by the standard number control. Configure the board
while it is naturally awake, for example around a real call. Frequent battery
removal is not an intended operating procedure. This portable handler does not
include a Home Assistant filesystem-based queue for delayed configuration.

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

The 15 offline tests use real ZHA/zigpy classes and a simulated device. They
check entity creation, wire types, actual report-frame decoding, battery
scaling, unknown cached measurements, reporting, wake-read rate limiting and
that unrelated devices are unchanged. They do not contact a coordinator.

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m unittest discover -s tests -v
```

## Upstream contribution

The intended upstream is [zigpy/zha-device-handlers](https://github.com/zigpy/zha-device-handlers).
The original support request is [#3763](https://github.com/zigpy/zha-device-handlers/issues/3763).
This package is not yet an upstream PR: adapting tests to the repository's
fixtures, adding sanitized diagnostics and running its current checks are
still required before submission. Acceptance and release timing depend on
its maintainers.

Protocol references:

- [Firmware and attributes](https://github.com/xyzroe/Zintercom/tree/5b2db2f803adb25d47cef93e337922eb1e34eefe).
- [Battery implementation](https://github.com/diyruz/zstack-lib/blob/47c4672dfe82d73b0d8c13b2d6bf8766aa56450d/bettery.c).
- [ZHA documentation](https://www.home-assistant.io/integrations/zha/).

Apache-2.0 license. This package contains no live device identifiers,
credentials, phone registrations, HomeKit pairing data or home configuration.
