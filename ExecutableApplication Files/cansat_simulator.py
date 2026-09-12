#!/usr/bin/env python3
"""
CanSat GCS — Python Telemetry Simulator  v2.1  (bug-fixed)
============================================================
Changes from v1:
  - BUG FIX: desc field uses signed convention:
      negative = ascending (climb rate)
      positive = descending (drop rate)
  - BUG FIX: D1 fault band widened to 7–13 m/s to absorb sensor
      noise and prevent constant false alarms during descent.
  - BUG FIX: D1 fault only fires during DESCENT phase; never
      triggers during ascent (where desc is negative).
  - Improved terminal output with signed vertical-speed display.

INSTALL:
    pip install websockets

RUN:
    python cansat_simulator.py

Then open cansat_gcs.html, enter port 8765, click WS CONNECT.
"""

import asyncio
import json
import math
import random
import time
import websockets

# ── Mission parameters ──────────────────────────────────────────────────────
PACKET_RATE_HZ       = 1       # packets per second
WS_PORT              = 8765
WS_HOST              = "localhost"

BASE_LAT = 28.6271
BASE_LON = 77.0831

# Flight profile phase boundaries (seconds from T+0)
PHASE_PRELAUNCH_END  = 5
PHASE_ASCENT_END     = 45
PHASE_DESCENT_END    = 130

# ── BUG FIX: D1 fault safe band — widened to absorb sensor noise ────────────
D1_SAFE_MIN = 7.0    # m/s  (was implicitly 8, too tight)
D1_SAFE_MAX = 13.0   # m/s  (was 10, too tight)

# ── Simulator state ──────────────────────────────────────────────────────────
class MissionState:
    def __init__(self):
        self.pkt         = 0
        self.t_start     = time.time()
        # BUG FIX: altitude starts at 0 (ground), not some arbitrary value
        self.alt         = 0.0
        self.vert_speed  = 0.0   # signed: negative=ascending, positive=descending
        self.lat         = BASE_LAT + random.uniform(-0.01, 0.01)
        self.lon         = BASE_LON + random.uniform(-0.01, 0.01)
        self.phase       = 0     # 0=prelaunch, 1=ascent, 2=descent, 3=landed

    def elapsed(self):
        return time.time() - self.t_start

    def update(self):
        t = self.elapsed()

        # Phase transitions
        if   t < PHASE_PRELAUNCH_END:  self.phase = 0
        elif t < PHASE_ASCENT_END:     self.phase = 1
        elif t < PHASE_DESCENT_END:    self.phase = 2
        else:                          self.phase = 3

        if self.phase == 1:
            # ASCENDING — vert_speed is NEGATIVE
            climb = 8.0 + random.uniform(0, 4)
            self.alt        += climb
            self.vert_speed  = -climb + random.uniform(-0.3, 0.3)

        elif self.phase == 2:
            # DESCENDING — vert_speed is POSITIVE
            drop = 8.5 + random.uniform(0, 2)
            self.alt         = max(0.0, self.alt - drop)
            self.vert_speed  = drop + random.uniform(-0.3, 0.3)

        else:
            self.vert_speed = 0.0

        # Small GPS drift each tick
        self.lat += random.uniform(-0.0001, 0.0001)
        self.lon += random.uniform(-0.0001, 0.0001)

    def generate_packet(self):
        self.pkt += 1
        self.update()
        t = self.elapsed()

        pressure    = 1013.25 * math.pow(max(0, 1 - self.alt / 44330), 5.255)
        temperature = 15.0 - self.alt * 0.0065 + random.uniform(-0.3, 0.3)
        battery     = max(3.5, 4.2 - self.pkt * 0.0008 + math.sin(t * 0.1) * 0.04)
        voltage     = battery * 0.98

        roll  = 10 * math.sin(t * 0.4) + random.uniform(-2, 2)
        pitch = 8  * math.cos(t * 0.3) + random.uniform(-1, 1)
        yaw   = (t * 15) % 360

        sats = 0 if self.phase == 0 else 8 + random.randint(0, 4)
        fix  = "FIX 3D" if sats > 4 else ("FIX 2D" if sats > 0 else "NO FIX")

        # ── Error codes ──────────────────────────────────────────────────
        # D1: descent rate fault — only fires in DESCENT phase,
        #     and only when vert_speed is outside the safe band.
        #     During ASCENT vert_speed is negative → never faults D1.
        d1 = int(
            self.phase == 2 and
            not (D1_SAFE_MIN <= self.vert_speed <= D1_SAFE_MAX)
        )

        # D2: GPS unavailable
        d2 = int(sats < 4)

        # D3: payload separation fault (rare, low-altitude event during descent)
        d3 = int(self.phase == 2 and self.alt < 200 and random.random() < 0.05)

        # D4: emergency parachute (vert_speed exceeds safe ceiling)
        d4 = int(self.vert_speed > D1_SAFE_MAX)

        error_code = f"{d1}{d2}{d3}{d4}"

        packet = {
            "pkt":        self.pkt,
            "alt":        round(self.alt, 1),
            "pres":       round(pressure, 2),
            "temp":       round(temperature, 1),
            # "desc" field: signed vertical speed (neg=up, pos=down)
            "desc":       round(self.vert_speed, 2),
            "batt":       round(battery, 2),
            "volt":       round(voltage, 2),
            "roll":       round(roll, 1),
            "pitch":      round(pitch, 1),
            "yaw":        round(yaw, 1),
            "lat":        round(self.lat, 6),
            "lon":        round(self.lon, 6),
            "sats":       sats,
            "fix":        fix,
            "phase":      ["PRE-LAUNCH","ASCENT","DESCENT","LANDED"][self.phase],
            "error_code": error_code,
            "timestamp":  round(t, 2)
        }
        return packet


# ── WebSocket server ─────────────────────────────────────────────────────────
mission = MissionState()
connected_clients = set()

async def telemetry_handler(websocket):
    connected_clients.add(websocket)
    client_addr = websocket.remote_address
    print(f"[WS] Client connected: {client_addr}  (total: {len(connected_clients)})")

    try:
        while True:
            packet = mission.generate_packet()
            await websocket.send(json.dumps(packet))

            # ── Terminal display ─────────────────────────────────────────
            ec = packet['error_code']
            ec_display = " ".join(
                f"\033[91m{c}\033[0m" if c == "1" else f"\033[92m{c}\033[0m"
                for c in ec
            )
            phase_colors = {
                "PRE-LAUNCH": "\033[90m",
                "ASCENT":     "\033[92m",
                "DESCENT":    "\033[93m",
                "LANDED":     "\033[96m",
            }
            pc   = phase_colors.get(packet['phase'], "")
            vspd = packet['desc']
            vdir = "↑" if vspd < 0 else ("↓" if vspd > 0 else "■")

            print(
                f"PKT:{packet['pkt']:04d} | "
                f"ALT:{packet['alt']:7.1f}m | "
                f"TEMP:{packet['temp']:5.1f}°C | "
                f"VSPD:{vdir}{abs(vspd):5.2f}m/s | "
                f"BAT:{packet['batt']:.2f}V | "
                f"GPS:{packet['lat']:.5f},{packet['lon']:.5f} | "
                f"ERR:[{ec_display}] | "
                f"{pc}{packet['phase']}\033[0m"
            )

            await asyncio.sleep(1.0 / PACKET_RATE_HZ)

    except websockets.exceptions.ConnectionClosed:
        print(f"[WS] Client disconnected: {client_addr}")
    finally:
        connected_clients.discard(websocket)


async def main():
    print("=" * 68)
    print("  CanSat GCS — Python Telemetry Simulator  v2.1  (bug-fixed)")
    print("=" * 68)
    print(f"  WebSocket Server : ws://{WS_HOST}:{WS_PORT}")
    print(f"  Packet Rate      : {PACKET_RATE_HZ} Hz")
    print(f"  Base GPS         : {BASE_LAT:.4f}, {BASE_LON:.4f} (New Delhi)")
    print()
    print("  FLIGHT PROFILE:")
    print(f"    T+00s — T+{PHASE_PRELAUNCH_END:02d}s  →  PRE-LAUNCH (on ground)")
    print(f"    T+{PHASE_PRELAUNCH_END:02d}s — T+{PHASE_ASCENT_END:02d}s  →  ASCENT   (vert_speed < 0)")
    print(f"    T+{PHASE_ASCENT_END:02d}s — T+{PHASE_DESCENT_END:02d}s →  DESCENT  (vert_speed > 0)")
    print(f"    T+{PHASE_DESCENT_END:02d}s+        →  LANDED")
    print()
    print("  VERTICAL SPEED CONVENTION (desc field):")
    print("    Negative value  →  ascending  (e.g. -9.2 m/s)")
    print("    Positive value  →  descending (e.g. +8.7 m/s)")
    print()
    print(f"  D1 FAULT BAND: {D1_SAFE_MIN}–{D1_SAFE_MAX} m/s safe during descent")
    print("  ERROR CODE:  D1=DescentRate  D2=GPS  D3=PayloadSep  D4=Chute")
    print("  Colors:      \033[92mGreen=OK (0)\033[0m   \033[91mRed=FAULT (1)\033[0m")
    print("=" * 68)
    print()
    print("  ► Open cansat_gcs.html in your browser")
    print("  ► Enter port 8765 and click 'WS CONNECT'")
    print()
    print("  Waiting for GCS connection ...")
    print()

    async with websockets.serve(telemetry_handler, WS_HOST, WS_PORT):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SYS] Simulator stopped.")