# Deferred intercom configuration

## Scope and user-visible contract

The extended handler exposes the existing five number controls and sound
switch with the same unique IDs and confirmed values. Their setters persist
configuration intent without pretending the board acknowledged a write. The
Pending settings diagnostic shows count and human-readable `status`; the
cancel button revokes unsent intent. Home Assistant 2026.11's ZHA sensor
wrapper supports this textual status without unexpected-attribute warnings.

Native mode is direct and is not queued. Its ordinary Zigbee retries are
unchanged; this extension does not claim to revoke a packet already sent to
the network. Future-call permission is an application policy, not a setting
that should be replayed after an unrelated call.

## Storage and delivery

Each physical IEEE/endpoint gets a sidecar in
`<zigpy database filename>.zintercom/`. The filesystem database path comes
from the controller configuration. No per-home identifiers are in the code.

A versioned document stores one latest request per allowed attribute and
confirmation history. Saving uses a temporary file in the same directory,
flush/fsync, atomic replacement and directory sync. Successful commits publish
memory state and cancellation effects even if the caller is canceled during
persistence. Storage errors are visible; requests are never accepted only in
memory. There is one supervised owner per ZHA device lifecycle, so old tasks
finish before a re-interview or reload binds a new owner.

A cached Idle or an ordinary attribute read cannot start delivery. A genuine
Idle report grants a fixed short monotonic window. Ring/Talk/Open/Drop and the
incoming-call signal revoke it and cancel any configuration operation still
waiting on a network slot. Already-transmitted packets cannot be recalled.
Repeated Idle reports coalesce rather than keeping the receive window open.

Each attempt reads first, writes only if necessary, and reads back without a
cache. Writes do not update the device cache optimistically. A lost reply
leaves the intent for a later read, so an already-applied value need not be
written again. Per-attribute versions prevent an old confirmation from
removing a newer edit. At most one attempt per version per window is made;
round-robin progress prevents a failing setting from starving another.

No polling timer repeatedly wakes or writes the board. Delivery waits for
actual firmware reports. Disabling sound has the same hardware/battery
implications as the original firmware; the queue does not change that.

## Compatibility with standard support

The base handler and extended handler are alternative implementations for
one device; install only one custom file. ZHA selects local custom handlers
after built-in ones. Accepting upstream PR #5781 therefore does not make the
extension unusable. Its controls retain the base handler's unique IDs,
startup reads, units, limits and sound reporting configuration.

The old one-parameter file `zintercom_pending_time_talk.json` is not consumed
by this extension. Before upgrading an installation that used that prototype,
check that it has no unconfirmed request, or migrate its validated request
explicitly. Do not run both consumers. Ordinary installations do not use that
prototype file.

## Proposal for upstream

The protocol-only PR is useful but does not provide this deferred-configuration
UX by itself. A durable queue should not be presented as already accepted by
ZHA maintainers. A portable upstream design would separate responsibilities:

1. The quirk declares the six idempotent settings and device-specific signals
   that open/revoke a receive window.
2. ZHA coordinates versions, priority, confirmation, cancellation and delivery.
3. The host, such as Home Assistant, supplies persistent storage and localized
   UI for requested versus confirmed state.

This needs API agreement and tests across hosts and sleeping devices. The
standalone sidecar implementation is available now as an opt-in extension;
it is not a promise that its exact filesystem design will be merged upstream.

## Validation

The suite exercises real ZHA control setters and a fake gateway using
anonymized physical-device diagnostics. It includes actual ZCL report
callbacks, a full ZHA owner restart, unchanged confirmed values while pending,
all six settings, uncertain delivery, latest-write-wins, cancellation during
fsync and transport waits, a new Idle during old-worker cleanup, timeout
fairness, corrupt stores and unavailable disk. Base protocol tests remain.

Independent architecture review used reproductions of the disk,
transport and lifecycle cancellation cases before deployment.

The extended handler was also installed on Home Assistant 2026.11.2 with
ZHA 2.4.3, zigpy 2.4.1 and zha-quirks 2.4.1. Live service calls queued all
six current configuration values, preserved them across a ZHA reload and
canceled them through the exposed button. Confirmed settings and opening
permissions remained unchanged. This smoke check did not send an opening
command or wait for a physical wake; receive-window delivery was validated
with the simulated gateway, not claimed as a new physical-radio test.
