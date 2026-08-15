# Bluetooth LE (BLE) Printers

For BLE thermal printers — Phomemo, cat printers (GB01/GB02/GT01), POS58-BLE, and the many
unbranded clones — including printers that are **nowhere near your Home Assistant host**,
reached through an ESPHome Bluetooth proxy.

> **BLE is not the same as Bluetooth Classic.** If your printer pairs like a headset and shows
> up in `bluetoothctl`, it is a Classic/RFCOMM printer — use
> [Bluetooth (RFCOMM) printers](bluetooth.md) instead. See
> [Which one do I have?](#which-one-do-i-have) below.

## Why you might want this

Compared with the Classic/RFCOMM flow, BLE:

- **Needs no pairing.** No `bluetoothctl`, no host shell access, no pairing mode.
- **Needs no D-Bus access.** No `/run/dbus` mount, no `--net=host`, no container privilege
  grants. It works on HA Container out of the box.
- **Works through Bluetooth proxies.** The printer only has to be in range of *something* Home
  Assistant can talk to — most usefully a cheap ESP32 running ESPHome.
- **Costs nothing to poll.** Status comes from advertisement data Home Assistant already
  receives, so the printer is never woken and never beeps. (RFCOMM status checks open a real
  connection, which many cheap printers announce loudly.)

## Requirements

- Home Assistant's **Bluetooth integration** set up, with at least one *connectable* scanner:
  either a Bluetooth adapter on the HA host, or a Bluetooth proxy.
- The printer powered on and advertising.

That's it. No pairing step.

## Using an ESPHome Bluetooth proxy

This is the setup that makes a garage or shop-counter printer practical. Flash any ESP32 with
ESPHome and add:

```yaml
esp32:
  board: esp32dev
  framework:
    type: esp-idf

bluetooth_proxy:
  active: true
```

**`active: true` is mandatory.** Without it the proxy only forwards advertisements — it can see
your printer but can never open a connection to it. The integration deliberately hides
non-connectable devices from the picker rather than letting you configure a printer that can
never print.

Adopt the ESP32 in Home Assistant, put it within a few metres of the printer, and the printer
appears in this integration's BLE picker. Nothing else is needed: the integration itself is not
proxy-aware, it just asks Home Assistant for a connection and HA picks the best route.

> **Connection slots are finite.** An ESP32 proxy typically supports three simultaneous
> connections. The integration holds the printer's link open for a short while after each print
> (see [Idle disconnect](#idle-disconnect)) rather than reconnecting every time, because a BLE
> connect takes 1-3 seconds. If you have several BLE devices sharing one proxy and see
> connection failures, lower the idle-disconnect time or add a second proxy.

## Adding the printer

1. **Settings → Devices & services → Add integration → ESC/POS Thermal Printer**
2. Choose **Bluetooth LE (GATT, proxy-capable)**
3. Pick your printer from the list

The list shows every connectable BLE device, strongest signal first, with its dBm reading.
Devices whose names look like printers are shown by default; **Show all discovered BLE
devices...** reveals the rest, which you will need if your printer advertises something generic
like `BT-Printer` or a bare address.

Setup opens a real connection and resolves the printer's write characteristic before accepting
the entry, so a printer that can't be reached — or that isn't actually an ESC/POS device — is
rejected at setup rather than failing on your first print.

## Which one do I have?

| Signal | Bluetooth Classic (RFCOMM) | Bluetooth LE (this page) |
|---|---|---|
| Pairing | Required, with a PIN (often `0000`) | None |
| Appears in phone's Bluetooth settings | Yes, as a pairable device | Usually not |
| Works via ESPHome proxy | **No — impossible** | Yes |
| Shows in `bluetoothctl devices` after pairing | Yes | Not usually |

Many printers support **both**. If yours does, prefer BLE when you want proxy reach or want to
avoid the container/D-Bus setup; prefer Classic for raw throughput on large images.

You can configure the same physical printer over both transports simultaneously — the entries
use separate unique IDs and won't collide.

## Tuning

### Write characteristic

BLE has no equivalent of the Serial Port Profile, so there is no single standard "printer data"
characteristic. The integration auto-detects one, preferring these known conventions in order:

| UUID | Typical printers |
|---|---|
| `0xFF02` (service `0xFF00`) | Cat printers (GB01/GB02/GT01), Goojprt, MTP series |
| `0x2AF1` (service `0x18F0`) | Phomemo, POS58-BLE, most generic clones |
| Nordic UART `6e400002-…` | nRF-module-based printers |
| Microchip transparent UART `49535343-8841-…` | BM70 / RN487x family |
| `0xFFE1` (service `0xFFE0`) | HM-10 style serial bridges |

If none match, it falls back to the first writable characteristic it finds.

**If your printer connects but nothing prints**, auto-detection probably picked a vendor control
characteristic instead of the data channel. Download the diagnostics for the entry
(**⋮ → Download diagnostics** on the device page) — it lists every characteristic the device
exposes, which are writable, and which are recognised. Then set **Write characteristic UUID** in
the entry's reconfigure form. Both the full 128-bit form and 16-bit shorthand (`ff02`) are
accepted.

### Write acknowledgement

BLE offers acknowledged (`write`) and unacknowledged (`write-without-response`) writes. The
default, **Auto**, uses acknowledged writes whenever the characteristic supports them, because
they give real backpressure — the printer can make us wait instead of silently dropping bytes.

Switch to **Never acknowledged** only if printing is unacceptably slow *and* your images come
out clean. If images come out garbled or truncated, you want acknowledged writes.

### BLE write delay

**Settings → Devices & services → ESC/POS → Configure → BLE write delay (ms)**, default `20`.

The pause between individual BLE packets. Raise it if large images print with gaps, garbage, or
stop partway. This is the first knob to reach for on a misbehaving printer.

For a printer that drops bytes on images, also try the **BLE-safe** reliability profile, which
pairs small image slices with a long per-slice wait.

### Idle disconnect

**Settings → ... → Configure → BLE idle disconnect (seconds)**, default `30`.

How long the GATT link is held open after a print. Keeping it open makes back-to-back prints
much faster (a reconnect costs 1-3 seconds) at the cost of holding one of your proxy's
connection slots. Set to `0` to disconnect after every print.

## Entities

Alongside the usual connectivity sensor, BLE entries get a **Signal strength** diagnostic sensor
reporting advertisement RSSI. It is **disabled by default** — enable it from the entity's
settings if you want to see how well the printer is heard, which is the quickest way to decide
whether it needs a proxy closer to it.

Paper status is not available on BLE (the GATT write channel is one-way, so the `DLE EOT` query
has nowhere to answer). Battery level is not available either — the bluez battery interface
used by Classic entries doesn't apply to proxied devices.

## Troubleshooting

- **`ble_no_bluetooth`** — Home Assistant's Bluetooth integration isn't set up. Add a Bluetooth
  adapter or a proxy first.
- **`ble_not_found`** — nothing can currently reach the printer. Check it's powered on and
  advertising (many portable models advertise only for a few minutes after power-on), and that a
  connectable scanner is in range.
- **`ble_no_write_char`** — connected, but the device exposes nothing writable. Usually means
  it isn't an ESC/POS printer. If you're sure it is, set the write characteristic UUID manually.
- **`ble_connect_failed`** — the connection attempt failed. Common causes: the printer is
  already connected to a phone (BLE printers accept one client), it's at the edge of range, or
  the proxy is out of free connection slots.
- **Printer missing from the picker** — choose **Show all discovered BLE devices...**. If it's
  still absent, the only scanner that can see it is passive; check `active: true` on your
  ESPHome proxy.
- **Prints start fine then turn to garbage** — raise the BLE write delay, switch write
  acknowledgement to **Always acknowledged**, or select the **BLE-safe** reliability profile.

## Security

BLE links to these printers are unencrypted and unauthenticated — any device in range can
connect to the printer and print to it, and traffic is recoverable over the air with consumer
SDR equipment. **Don't route OTPs, 2FA codes, door-access logs, alarm-disarm codes, or other
sensitive content to a BLE printer.** The same warning applies to Bluetooth Classic printers.

Using a Bluetooth proxy does not change this: the proxy-to-HA hop is over your network, but the
proxy-to-printer hop is still plain BLE.
