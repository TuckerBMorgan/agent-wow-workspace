"""Small, synchronous WoW 3.3.5a helpers for agent-written controllers.

Client.connect(character, navigator=...) builds a small Go packet relay against
the workspace's agent-wow checkout, using the CLI's saved login and realm.
Use it instead of char play: one world session/controller per character.
Alternatively supply Client(send, poll, clock) callbacks or Bridge(url).
Navigation is a callback (map_id, start_xyz, end_xyz) returning ground waypoints.
Quest order, special objectives and class spell choices remain caller supplied.
Python dependencies are standard library only; starting the relay requires Go.
"""
from collections import deque
import base64
import fcntl
import http.client
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import urllib.request
import zlib


def rpc(method, params=None, *, url=None, timeout_s=10):
    """Call an existing module's JSON-RPC method. Writes are never retried."""
    url = url or f"http://127.0.0.1:{os.environ.get('AGENT_WOW_WORLDRPC_PORT', '8086')}/rpc"
    body = json.dumps(dict(jsonrpc="2.0", id=1, method=method, params=params or {})).encode()
    request = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        result = json.load(response)
    if "error" in result:
        raise RuntimeError(f"{method}: {result['error']}")
    return result["result"]


def pack(fmt, *values):
    return struct.pack("<" + fmt, *values)


def packed_guid(guid):
    octets = int(guid).to_bytes(8, "little")
    return bytes([sum(1 << i for i, byte in enumerate(octets) if byte)]) + bytes(b for b in octets if b)


class Reader:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def read(self, fmt):
        values = struct.unpack_from("<" + fmt, self.data, self.offset)
        self.offset += struct.calcsize("<" + fmt)
        return values[0] if len(values) == 1 else values

    def skip(self, count):
        if count < 0 or self.offset + count > len(self.data):
            raise ValueError("Truncated packet")
        self.offset += count

    def string(self):
        end = self.data.index(0, self.offset)
        value = self.data[self.offset:end].decode("utf-8", "replace")
        self.offset = end + 1
        return value

    def guid(self):
        mask = self.read("B")
        return sum(self.read("B") << (8 * i) for i in range(8) if mask & (1 << i))


def _movement(r):
    flags, extra, _ = r.read("IHI")
    position = list(r.read("ffff"))
    if flags & 0x200:
        r.guid()
        r.skip(21 + (4 if extra & 0x400 else 0))
    if flags & 0x2200000 or extra & 0x20: r.skip(4)
    r.skip(4)
    if flags & 0x1000: r.skip(16)
    if flags & 0x4000000: r.skip(4)
    return flags, position


def _object_movement(r):
    flags, position, speed = r.read("H"), None, None
    if flags & 0x20:
        moving, position = _movement(r)
        speed = r.read("9f")[1]
        if moving & 0x8000000:
            spline = r.read("I")
            if spline & 0x20000: r.skip(4)
            elif spline & 0x10000: r.skip(8)
            elif spline & 0x8000: r.skip(12)
            r.skip(28)
            r.skip(r.read("I") * 12 + 13)
    elif flags & 0x100:
        r.guid()
        position = list(r.read("fff"))
        r.skip(12)
        position.append(r.read("f"))
        r.skip(4)
    elif flags & 0x40:
        position = list(r.read("ffff"))
    for flag, size in ((8, 4), (16, 4)):
        if flags & flag: r.skip(size)
    if flags & 4: r.guid()
    for flag, size in ((2, 4), (0x80, 8), (0x200, 8)):
        if flags & flag: r.skip(size)
    return flags, position, speed


class Bridge:
    """Cursor-based HTTP transport. Attaching to a URL never takes process ownership."""
    def __init__(self, url):
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
            raise ValueError("Bridge must use an http://127.0.0.1:PORT URL")
        self.url, self.cursor, self.guid, self.session, self.process = url.rstrip("/"), 0, None, None, None
        self._time, self._sample = 0, time.monotonic()
        self._http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def start(cls, character, *, workspace=None, client_dir=None, timeout_s=120):
        """Build/start a relay. Login with agent-wow auth login and select a realm first."""
        workspace = Path(workspace or Path.cwd()).resolve()
        client = Path(client_dir or os.environ.get("AGENT_WOW_CLIENT_DIR", workspace / "agent-wow")).resolve()
        if not (client / "go.mod").is_file(): raise FileNotFoundError(f"Missing agent-wow checkout: {client}")
        root = workspace / "scripts" / ".wow-bridge"
        root.mkdir(parents=True, exist_ok=True)
        build = root / "build"
        build.mkdir(exist_ok=True)
        session = Path(tempfile.mkdtemp(prefix="session-", dir=root))
        end, ready = time.monotonic() + timeout_s, session / "ready.json"
        with (session / "bridge.log").open("ab") as log:
            with (root / "build.lock").open("a") as lock:
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        _remaining(end)
                        time.sleep(.1)
                # Keep one executable; Go's build cache handles source and dependency changes.
                (build / "main.go").write_text(_BRIDGE_GO)
                (build / "go.mod").write_text("module github.com/agent-wow/agent-wow/commonbridge\n\ngo 1.27.1\n"
                    "require github.com/agent-wow/agent-wow v0.0.0\nreplace github.com/agent-wow/agent-wow => " + json.dumps(str(client)) + "\n")
                try:
                    subprocess.run(["go", "build", "-mod=mod", "-o", str(build / "bridge"), "."], cwd=build,
                                   stdout=log, stderr=log, check=True, timeout=_remaining(end))
                except (subprocess.SubprocessError, OSError) as e:
                    raise RuntimeError(f"Bridge build failed; see {session / 'bridge.log'}") from e
                process = subprocess.Popen([str(build / "bridge"), character, str(ready)], cwd=workspace,
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        try:
            while not ready.is_file():
                if process.poll() is not None: raise RuntimeError(f"Bridge exited; see {session / 'bridge.log'}")
                _remaining(end)
                time.sleep(.1)
            info = json.loads(ready.read_text())
            bridge = cls(info["url"])
            bridge.guid, bridge.session, bridge.process = int(info["guid"]), info["session"], process
            return bridge
        except BaseException:
            _terminate(process)
            raise

    def _request(self, path, body=None, *, timeout_s=10):
        if self.process is not None and self.process.poll() is not None: raise ConnectionError("World bridge exited")
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data, {"Content-Type": "application/json"})
        with self._http.open(req, timeout=timeout_s) as response: return json.load(response)

    def send(self, opcode, payload=b""):
        return self._request("/send", dict(opcode=opcode, payload=base64.b64encode(payload).decode()))

    def poll(self):
        result = self._request(f"/poll?after={self.cursor}")
        if self.session is not None and result["session"] != self.session:
            raise ConnectionError("Bridge session changed; create a fresh Client")
        packets = [(p["opcode"], base64.b64decode(p["payload"], validate=True)) for p in result["packets"]]
        if result["next"] != self.cursor + len(packets): raise ConnectionError("Packet history is discontinuous")
        self.cursor, self.guid, self.session = result["next"], int(result["guid"]), result["session"]
        self._time, self._sample = result["clock"], time.monotonic()
        return packets

    def clock(self):
        return (self._time + int((time.monotonic() - self._sample) * 1000)) & 0xffffffff

    def close(self):
        if self.process is None: return
        try:
            if self.process.poll() is None:
                try: self._request("/logout", {}, timeout_s=5)
                except (OSError, http.client.HTTPException): pass  # Logout can close the HTTP process too.
        finally:
            _terminate(self.process)
            self.process = None


def _terminate(process):
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _remaining(end):
    remaining = end - time.monotonic()
    if remaining <= 0: raise TimeoutError("Action deadline exceeded")
    return remaining


class CastError(RuntimeError):
    def __init__(self, spell_id, code):
        self.spell_id, self.code = spell_id, code
        super().__init__(f"Cast {spell_id} rejected: code {code}")


class Client:
    def __init__(self, send, poll, clock, *, guid=None, navigator=None):
        self._send, self._poll, self._clock = send, poll, clock
        self.navigator, self._bridge = navigator, None
        self.guid = int(guid) if guid is not None else None
        self.objects, self.names, self.spells = {}, {}, set()
        self.position = self.map_id = self.run_speed = None
        self.events, self.sequence, self.world, self.cast_count = deque(maxlen=256), 0, 0, 0
        self._queried = set()
        self._splines = {}

    @classmethod
    def connect(cls, character, *, navigator=None, timeout_s=120, **bridge_options):
        """Open a world session. Use as a context manager to stop and log out on exit."""
        end = time.monotonic() + timeout_s
        bridge = Bridge.start(character, timeout_s=_remaining(end), **bridge_options)
        client = cls(bridge.send, bridge.poll, bridge.clock, guid=bridge.guid, navigator=navigator)
        client._bridge = bridge
        try:
            client.wait_until(lambda c: c.guid in c.objects and c.position is not None, timeout_s=_remaining(end))
            return client
        except BaseException:
            bridge.close()
            raise

    def __enter__(self): return self

    def __exit__(self, kind, value, traceback):
        try: self.close()
        except Exception:
            if kind is None: raise

    def close(self):
        if self._bridge is not None:
            try: self.stop()
            finally:
                self._bridge.close()
                self._bridge = None

    def send(self, opcode, payload=b""):
        """Return the bridge's transport result; this does not confirm a game action."""
        return self._send(opcode, payload)

    def refresh(self):
        """Drain the bridge. Callbacks and helpers run on the controller's thread."""
        for opcode, payload in self._poll():
            self._packet(opcode, payload)
        for guid, (started, duration, points) in list(self._splines.items()):
            if guid not in self.objects:
                self._splines.pop(guid)
                continue
            fraction = min(1, (time.monotonic() - started) / max(.001, duration))
            lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
            remaining = sum(lengths) * fraction
            position = points[-1]
            for a, b, length in zip(points, points[1:], lengths):
                if length and remaining < length:
                    position = [a[i] + (b[i] - a[i]) * remaining / length for i in range(3)]
                    break
                remaining -= length
            self.objects[guid]["position"] = list(position) + [0]
            if fraction == 1: self._splines.pop(guid)

    def _packet(self, opcode, data):
        self.sequence += 1
        self.events.append((self.sequence, opcode, data))
        r = Reader(data)
        if opcode in (0x236, 0x03E):  # LOGIN_VERIFY_WORLD, NEW_WORLD
            self.map_id = r.read("I")
            self.position = list(r.read("ffff"))
            self.objects.clear()
            self._splines.clear()
            self.world += 1
        elif opcode in (0x0A9, 0x1F6):  # UPDATE_OBJECT, COMPRESSED_UPDATE_OBJECT
            if opcode == 0x1F6:
                size = r.read("I")
                if size > 8 << 20: raise ValueError("Oversized object update")
                inflater = zlib.decompressobj()
                data = inflater.decompress(data[4:], size + 1)
                if len(data) != size or not inflater.eof: raise ValueError("Invalid compressed update")
            self._update(data)
        elif opcode == 0x0AA:  # DESTROY_OBJECT
            self.objects.pop(r.read("Q"), None)
        elif opcode == 0x0DD:  # Approximate NPC spline timing along reported points.
            guid = r.guid()
            r.skip(1)
            start = list(r.read("fff"))
            r.skip(4)
            kind = r.read("B")
            self._splines.pop(guid, None)
            if guid in self.objects: self.objects[guid]["position"] = start + [0]
            if kind == 1: return
            r.skip({0: 0, 2: 12, 3: 8, 4: 4}[kind])
            flags = r.read("I")
            if flags & 0x200000: r.skip(5)
            duration = r.read("I") / 1000
            if flags & 0x800: r.skip(8)
            count = r.read("I")
            if not count: return
            if flags & 0x42000:  # Uncompressed Catmull-Rom; use a polyline approximation.
                points = [start] + [list(r.read("fff")) for _ in range(count)]
            else:
                destination = list(r.read("fff"))
                middle = [(a + b) / 2 for a, b in zip(start, destination)]
                points = [start]
                for _ in range(count - 1):
                    word = r.read("I")
                    delta = [(word >> shift) & ((1 << bits) - 1) for shift, bits in ((0, 11), (11, 11), (22, 10))]
                    delta = [v - (1 << bits) if v & (1 << (bits - 1)) else v for v, bits in zip(delta, (11, 11, 10))]
                    points.append([middle[i] - delta[i] / 4 for i in range(3)])
                points.append(destination)
            self._splines[guid] = time.monotonic(), duration, points
        elif 0x0B5 <= opcode <= 0x0C1 or opcode in (0x0DA, 0x0EE, 0x0C7):
            guid = r.guid()
            if opcode == 0x0C7: r.skip(4)  # TELEPORT_ACK has an order counter.
            _, position = _movement(r)
            self._splines.pop(guid, None)
            if guid in self.objects: self.objects[guid]["position"] = position
            if guid == self.guid:
                self.position = position
                if opcode == 0x0C7: self.world += 1
        elif opcode == 0x0E2:  # FORCE_RUN_SPEED_CHANGE; ACK belongs to the bridge.
            guid = r.guid()
            r.skip(5)
            if guid == self.guid: self.run_speed = r.read("f")
        elif opcode == 0x12A:  # INITIAL_SPELLS
            r.skip(1)
            self.spells = {r.read("IH")[0] for _ in range(r.read("H"))}
        elif opcode == 0x12B: self.spells.add(r.read("I"))
        elif opcode == 0x203: self.spells.discard(r.read("I"))
        elif opcode in (0x061, 0x05F, 0x058):  # Creature, gameobject, item query responses.
            entry = r.read("I")
            if entry & 0x80000000: return
            kind = {0x061: 3, 0x05F: 5, 0x058: 1}[opcode]
            if kind == 5: r.skip(8)
            if kind == 1: r.skip(12)
            self.names[kind, entry] = r.string()

    def _update(self, data):
        r = Reader(data)
        for _ in range(r.read("I")):
            kind = r.read("B")
            if kind in (4, 5):
                for _ in range(r.read("I")): self.objects.pop(r.guid(), None)
                continue
            guid = r.guid()
            obj = self.objects.setdefault(guid, dict(guid=guid, type=None, fields={}, position=None))
            if kind in (2, 3):
                obj["type"] = r.read("B")
                if obj["type"] in (3, 4): obj["fields"].update({24: 0, 32: 0, 150: 0})
            if kind in (1, 2, 3):
                flags, position, speed = _object_movement(r)
                if position is not None: obj["position"] = position
                if flags & 1: self.guid = guid  # UPDATEFLAG_SELF
                if guid == self.guid:
                    if position is not None: self.position = position
                    if speed is not None: self.run_speed = speed
            if kind in (0, 2, 3):
                masks = [r.read("I") for _ in range(r.read("B"))]
                obj["fields"].update({i * 32 + bit: r.read("I") for i, mask in enumerate(masks)
                                      for bit in range(32) if mask & (1 << bit)})
            elif kind != 1: raise ValueError(f"Unknown object update type: {kind}")
            entry = obj["fields"].get(3)
            category = 1 if obj["type"] == 2 else obj["type"]
            if entry and category in (1, 3, 5) and (category, entry) not in self._queried:
                self._queried.add((category, entry))
                self.send({1: 0x056, 3: 0x060, 5: 0x05E}[category], pack("IQ", entry, guid))

    def _entity(self, obj):
        fields, position = obj["fields"], obj["position"]
        category = obj["type"]
        return dict(guid=obj["guid"], type=category, entry_id=fields.get(3),
                    world=self.world,
                    name=self.names.get((category, fields.get(3))), position=position,
                    health=fields.get(24) if category in (3, 4) else None,
                    max_health=fields.get(32) if category in (3, 4) else None,
                    level=fields.get(54) if category in (3, 4) else None,
                    distance=math.dist(position[:3], self.position[:3]) if position and self.position else None,
                    fields=dict(fields))

    def get_player(self):
        self.refresh()
        if self.guid not in self.objects or self.position is None:
            raise RuntimeError("Player has not been observed; replay packets from login")
        player = self._entity(self.objects[self.guid])
        fields = player["fields"]
        power_type = fields.get(23, 0) >> 24
        scale = 10 if power_type in (1, 6) else 1
        player.update(position=self.position[:], map_id=self.map_id, run_speed=self.run_speed,
                      power_type=power_type, power=fields.get(25 + power_type, 0) / scale,
                      max_power=fields.get(33 + power_type, 0) / scale,
                      in_combat=bool(fields.get(59, 0) & 0x80000),
                      target_guid=fields.get(18, 0) | fields.get(19, 0) << 32)
        return player

    def nearby(self, entry_id=None, *, name=None, kind=3, radius=50, alive=None):
        """Return matching entities, nearest first. kind: 3 NPC, 4 player, 5 object."""
        self.refresh()
        entities = [self._entity(o) for o in self.objects.values() if o["type"] == kind]
        return sorted((o for o in entities if o["distance"] is not None and o["distance"] <= radius
                       and (entry_id is None or o["entry_id"] == entry_id)
                       and (name is None or (o["name"] or "").casefold() == name.casefold())
                       and (alive is None or o["health"] is not None and (o["health"] > 0) == alive)),
                      key=lambda o: o["distance"])

    def quest_log(self):
        fields = self.get_player()["fields"]
        quests = {}
        for base in range(158, 283, 5):
            if fields.get(base):
                quests[fields[base]] = dict(complete=bool(fields.get(base + 1, 0) & 1),
                    failed=bool(fields.get(base + 1, 0) & 2),
                    counts=[(fields.get(base + 2 + i // 2, 0) >> (16 * (i % 2))) & 0xffff for i in range(4)])
        return quests

    def inventory(self):
        """Return carried and equipped item stacks, with their bag and slot."""
        fields = self.get_player()["fields"]
        guid_at = lambda values, index: values.get(index, 0) | values.get(index + 1, 0) << 32
        slots = [(255, slot, guid_at(fields, 324 + slot * 2)) for slot in range(39)]
        for bag in range(19, 23):
            container = self.objects.get(guid_at(fields, 324 + bag * 2), {}).get("fields", {})
            slots.extend((bag, slot, guid_at(container, 66 + slot * 2)) for slot in range(container.get(64, 0)))
        items = []
        for bag, slot, guid in slots:
            if not guid: continue
            item = self.objects.get(guid, {}).get("fields", {})
            items.append(dict(guid=guid, id=item.get(3), name=self.names.get((1, item.get(3))),
                              count=item.get(14), bag=bag, slot=slot, equipped=bag == 255 and slot < 19))
        return items

    def wait_until(self, predicate, *, timeout_s=10):
        """Poll until predicate(client) returns a truthy value; return that value."""
        end = time.monotonic() + timeout_s
        while True:
            self.refresh()
            result = predicate(self)
            if result: return result
            if time.monotonic() >= end: raise TimeoutError("No matching observation before the deadline")
            time.sleep(min(.1, max(0, end - time.monotonic())))

    def _wait_packet(self, opcodes, after, predicate=lambda op, data: True, *, timeout_s=10):
        return self.wait_until(lambda c: next(((op, data) for seq, op, data in c.events
                               if seq > after and op in opcodes and predicate(op, data)), None), timeout_s=timeout_s)

    @staticmethod
    def _guid(target):
        return int(target["guid"] if isinstance(target, dict) else target)

    def _move(self, opcode, position, flags=0):
        if self.guid is None or len(position) != 4 or not all(math.isfinite(v) for v in position):
            raise ValueError("Movement needs a known player GUID and finite x, y, z, orientation")
        self.send(opcode, packed_guid(self.guid) + pack("IHIffffI", flags, 0, self._clock(), *position, 0))
        self.position = list(position)
        if self.guid in self.objects: self.objects[self.guid]["position"] = self.position[:]

    def face(self, position):
        self.get_player()
        x, y, z, _ = self.position
        self._move(0x0DA, [x, y, z, math.atan2(position[1] - y, position[0] - x)])

    def walk_path(self, points, *, timeout_s=60, stop_when=None):
        """Walk ground waypoints supplied by your navigator. Return client position."""
        self.get_player()
        world, end, moving = self.world, time.monotonic() + timeout_s, False
        try:
            for point in points:
                if len(point) != 3 or not all(math.isfinite(v) for v in point): raise ValueError("Expected finite 3D waypoints")
                while math.dist(self.position[:3], point) > .05:
                    player = self.get_player()
                    if stop_when is not None and stop_when(self): return self.position[:]
                    if self.world != world: raise RuntimeError("Map change or teleport interrupted movement")
                    if player["health"] == 0 and not player["fields"].get(150, 0) & 16: raise RuntimeError("Player is dead")
                    speed = self.run_speed
                    if speed is None or not math.isfinite(speed) or speed <= 0: raise RuntimeError("No usable run speed observed")
                    if time.monotonic() >= end: raise TimeoutError("Movement deadline exceeded")
                    current = self.position[:]
                    distance = math.dist(current[:3], point)
                    if distance <= .05: break
                    duration = min(.1, distance / speed, end - time.monotonic())
                    if duration <= 0: raise TimeoutError("Movement deadline exceeded")
                    time.sleep(duration)
                    self.refresh()
                    if self.world != world: raise RuntimeError("Map change or teleport interrupted movement")
                    if stop_when is not None and stop_when(self): return self.position[:]
                    fields = self.objects[self.guid]["fields"]
                    if fields.get(24, 0) == 0 and not fields.get(150, 0) & 16:
                        raise RuntimeError("Player is dead")
                    fraction = min(1, speed * duration / distance)
                    position = [current[i] + (point[i] - current[i]) * fraction for i in range(3)]
                    position.append(math.atan2(point[1] - current[1], point[0] - current[0]))
                    self._move(0x0EE if moving else 0x0B5, position, 1)
                    moving = True
            return self.position[:]
        finally:
            if self.world == world: self._move(0x0B7, self.position)

    def _target(self, target):
        self.refresh()
        if isinstance(target, dict) and target.get("world", self.world) != self.world:
            raise RuntimeError("Target belongs to an earlier map/session")
        obj = self.objects.get(self._guid(target))
        if obj is None: raise RuntimeError("Target is no longer visible")
        return self._entity(obj)

    def move_to(self, position, *, within=2, timeout_s=60):
        """Plan a ground route and walk it. Navigator must include start and destination."""
        self.get_player()
        destination = list(position[:3])
        if len(destination) != 3 or not all(math.isfinite(v) for v in destination) or within < 0:
            raise ValueError("Expected finite XYZ and nonnegative arrival distance")
        if math.dist(self.position[:3], destination) <= within: return self.position[:]
        end = time.monotonic() + timeout_s
        points = self._path(destination)
        self.walk_path(points, timeout_s=_remaining(end),
                       stop_when=lambda c: math.dist(c.position[:3], destination) <= within)
        if math.dist(self.position[:3], destination) > within: raise RuntimeError("Ground path did not reach destination")
        return self.position[:]

    def _path(self, destination):
        if self.navigator is None: raise ValueError("Set navigator(map_id, start_xyz, destination_xyz) for movement")
        start = self.position[:3]
        points = [list(p) for p in self.navigator(self.map_id, start[:], list(destination))]
        if not points or any(len(p) != 3 or not all(math.isfinite(v) for v in p) for p in points):
            raise ValueError("Navigator returned an empty or invalid ground path")
        if math.dist(points[0], start) > 2 or math.dist(points[-1], destination) > 1:
            raise ValueError("Navigator returned a partial path; refusing a straight-line shortcut")
        return points

    def approach(self, target, *, within=3, timeout_s=60):
        """Follow a visible entity, replanning when it moves; also works for corpses."""
        if within <= 0: raise ValueError("within must be positive")
        end = time.monotonic() + timeout_s
        while True:
            _remaining(end)
            self.get_player()
            entity = self._target(target)
            if entity["distance"] is None: raise RuntimeError("Target has no observed position")
            if entity["distance"] <= within: return entity
            destination = entity["position"][:3]
            def arrived_or_moved(c):
                current = c._target(entity)
                return current["distance"] <= within or math.dist(current["position"][:3], destination) > 2
            points = self._path(destination)
            self.walk_path(points, timeout_s=_remaining(end), stop_when=arrived_or_moved)

    def go_to_npc(self, entry_id, *, position=None, radius=100, timeout_s=60):
        """Find/approach a living NPC; optional known location handles out-of-sight NPCs."""
        end = time.monotonic() + timeout_s
        targets = self.nearby(entry_id, radius=radius, alive=True)
        if not targets and position is not None:
            self.move_to(position, timeout_s=_remaining(end))
            targets = self.nearby(entry_id, radius=radius, alive=True)
        if not targets: raise RuntimeError(f"NPC entry {entry_id} is not visible")
        return self.approach(targets[0], timeout_s=_remaining(end))

    def attack(self, target):
        """Select a target and request melee auto-attack; does not fight to completion."""
        guid = pack("Q", self._guid(target))
        self.send(0x13D, guid)
        self.send(0x141, guid)

    def stop(self):
        if self.position is not None: self._move(0x0B7, self.position)
        self.send(0x142)  # ATTACKSTOP
        self.send(0x26D)  # CANCEL_AUTO_REPEAT_SPELL
        self.send(0x12F, pack("I", 0))  # CANCEL_CAST

    def cast(self, spell_id, target=None, *, object_target=False, timeout_s=30):
        """Wait for this cast's SPELL_GO or CAST_FAILED. SPELL_GO does not imply a hit."""
        self.get_player()
        self.cast_count = (self.cast_count + 1) % 256
        sequence, after = self.cast_count, self.sequence
        targets = pack("I", (0x800 if object_target else 2) if target is not None else 0)
        if target is not None: targets += packed_guid(self._guid(target))
        self.send(0x12E, pack("BIB", sequence, spell_id, 0) + targets)
        def matches(op, data):
            r = Reader(data)
            if op == 0x132:  # SPELL_GO starts with two packed GUIDs.
                caster, unit = r.guid(), r.guid()
                if self.guid not in (caster, unit): return False
            return r.read("BI") == (sequence, spell_id)
        op, data = self._wait_packet({0x130, 0x132}, after, matches, timeout_s=timeout_s)
        if op == 0x130: raise CastError(spell_id, data[5])

    def fight(self, target, *, rotation=None, melee=True, distance=3, loot=True, timeout_s=60):
        """Fight to observed death, then optionally loot. A vanished target is an error.

        rotation(client, target) returns a spell ID, (spell_id, target), or None.
        Use client.get_player() as the target for a self buff. Spells are attempted
        at most once per 1.5 seconds; the callback chooses known/affordable spells.
        Set melee=False and distance to the desired range for a caster.
        """
        if not melee and rotation is None: raise ValueError("Ranged combat needs a rotation callback")
        end, next_cast = time.monotonic() + timeout_s, 0
        entity = self._target(target)
        world = self.world
        try:
            entity = self.approach(entity, within=distance, timeout_s=_remaining(end))
            if melee and entity["health"]: self.attack(entity)
            while True:
                _remaining(end)
                player, entity = self.get_player(), self._target(entity)
                if player["health"] == 0: raise RuntimeError("Player died during combat")
                if entity["health"] is None: raise RuntimeError("Target health has not been observed")
                if entity["health"] == 0: break
                if entity["distance"] > distance:
                    entity = self.approach(entity, within=distance, timeout_s=_remaining(end))
                self.face(entity["position"])
                if rotation is not None and time.monotonic() >= next_cast:
                    action = rotation(self, entity)
                    if action is not None:
                        spell_id, spell_target = action if isinstance(action, tuple) else (action, entity)
                        try: self.cast(spell_id, spell_target, timeout_s=min(30, _remaining(end)))
                        except CastError as e:
                            # Retry transient range/facing, cooldown and resource failures only.
                            if self._target(entity)["health"] != 0 and e.code not in (61, 67, 85, 97, 105, 134): raise
                        next_cast = time.monotonic() + 1.5
                time.sleep(min(.2, _remaining(end)))
        finally:
            if self.world == world: self.stop()
        if loot and entity["fields"].get(79, 0) & 1:  # UNIT_DYNAMIC_FLAGS: lootable.
            self.approach(entity, timeout_s=_remaining(end))
            self.loot(entity, timeout_s=min(10, _remaining(end)))
        return self._target(entity)

    def recover(self, *, health=.9, power=.8, consume=None, timeout_s=90):
        """Wait out of combat for health and mana/focus/energy. consume(client) is optional."""
        if not 0 < health <= 1 or not 0 <= power <= 1: raise ValueError("Recovery thresholds must be fractions")
        def ready(c):
            player = c.get_player()
            if player["health"] == 0: raise RuntimeError("Cannot recover while dead")
            if player["in_combat"]: raise RuntimeError("Cannot recover during combat")
            if not player["max_health"]: raise RuntimeError("Maximum health has not been observed")
            enough_power = player["power_type"] not in (0, 2, 3) or player["power"] >= player["max_power"] * power
            return player if player["health"] >= player["max_health"] * health and enough_power else None
        player = ready(self)
        if player: return player
        end = time.monotonic() + timeout_s
        self.stop()
        if consume is not None: consume(self)
        return self.wait_until(ready, timeout_s=_remaining(end))

    def _quest_complete(self, quest_id):
        quest = self.quest_log().get(quest_id)
        if quest is None: raise RuntimeError(f"Quest {quest_id} is not in the active quest log")
        if quest["failed"]: raise RuntimeError(f"Quest {quest_id} has failed")
        return quest["complete"]

    def hunt(self, entry_ids, *, count=1, quest_id=None, rotation=None, patrol=None,
             radius=80, recover_at=.6, melee=True, distance=3, timeout_s=300):
        """Kill matching NPCs until quest completion, or count kills without a quest.

        Optional patrol is a list of ground destinations for finding targets.
        Without it, wait for nearby respawns. Quest objectives come from the server.
        """
        entries = {entry_ids} if isinstance(entry_ids, int) else set(entry_ids)
        if not entries or count < 1 or not 0 < recover_at <= 1: raise ValueError("Invalid hunt targets/count/recovery threshold")
        end, killed, waypoint = time.monotonic() + timeout_s, 0, 0
        patrol = list(patrol or [])
        while quest_id is not None or killed < count:
            _remaining(end)
            if quest_id is not None and self._quest_complete(quest_id): break
            player = self.get_player()
            if player["health"] == 0: raise RuntimeError("Player died while hunting")
            if player["health"] < (player["max_health"] or 0) * recover_at and not player["in_combat"]:
                self.recover(timeout_s=_remaining(end))
            candidates = [t for t in self.nearby(radius=radius, alive=True) if t["entry_id"] in entries]
            if candidates:
                self.fight(candidates[0], rotation=rotation, melee=melee, distance=distance, timeout_s=_remaining(end))
                killed += 1
            elif patrol:
                self.move_to(patrol[waypoint % len(patrol)], timeout_s=_remaining(end))
                waypoint += 1
                time.sleep(min(.2, _remaining(end)))
            else:
                time.sleep(min(.5, _remaining(end)))
        return killed

    def complete_quest(self, quest_id, npc_entry, *, targets=None, objective=None, finisher_entry=None,
                       giver_position=None, finisher_position=None, reward_index=None, rotation=None, timeout_s=300):
        """Accept, perform objectives and turn in one caller-selected quest.

        targets supplies kill/loot NPC entry IDs. For other objectives supply
        objective(client, remaining_seconds). A completed quest is skipped.
        """
        end = time.monotonic() + timeout_s
        if quest_id in self.completed_quests(timeout_s=min(10, _remaining(end))): return quest_id
        if quest_id not in self.quest_log():
            npc = self.go_to_npc(npc_entry, position=giver_position, timeout_s=_remaining(end))
            self.accept_quest(npc, quest_id, timeout_s=min(10, _remaining(end)))
        if not self._quest_complete(quest_id):
            if objective is not None: objective(self, _remaining(end))
            elif targets is not None: self.hunt(targets, quest_id=quest_id, rotation=rotation, timeout_s=_remaining(end))
            else: raise ValueError("Supply targets or an objective callback for this quest")
        if not self._quest_complete(quest_id): raise RuntimeError(f"Quest {quest_id} objectives are still incomplete")
        location = finisher_position if finisher_position is not None else giver_position if finisher_entry in (None, npc_entry) else None
        npc = self.go_to_npc(finisher_entry or npc_entry, position=location,
                             timeout_s=_remaining(end))
        self.turn_in_quest(npc, quest_id, reward_index=reward_index, timeout_s=min(10, _remaining(end)))
        return quest_id

    def quest_route(self, steps, *, rotation=None, timeout_s=900):
        """Run ordinary dicts of complete_quest arguments in order, within one deadline."""
        end, completed = time.monotonic() + timeout_s, []
        for step in steps:
            options = dict(step)
            options.setdefault("rotation", rotation)
            options["timeout_s"] = min(options.get("timeout_s", timeout_s), _remaining(end))
            completed.append(self.complete_quest(**options))
        return completed

    def talk_to(self, target):
        return self.send(0x17B, pack("Q", self._guid(target)))

    def accept_quest(self, npc, quest_id, *, timeout_s=10):
        end = time.monotonic() + timeout_s
        if quest_id in self.quest_log(): return
        guid, after = self._guid(npc), self.sequence
        self.send(0x186, pack("QIB", guid, quest_id, 0))
        self._wait_packet({0x188}, after, lambda op, b: struct.unpack_from("<Q", b)[0] == guid
                          and struct.unpack_from("<I", b, 16)[0] == quest_id, timeout_s=max(0, end - time.monotonic()))
        self.send(0x189, pack("QII", guid, quest_id, 0))
        self.wait_until(lambda c: quest_id in c.quest_log(), timeout_s=max(0, end - time.monotonic()))

    def turn_in_quest(self, npc, quest_id, *, reward_index=None, timeout_s=10):
        """NPC must be in range. Multiple reward choices require an explicit index."""
        end = time.monotonic() + timeout_s
        self.refresh()
        guid, after = self._guid(npc), self.sequence
        matches = lambda op, b: struct.unpack_from("<QI", b) == (guid, quest_id)
        self.send(0x18A, pack("QI", guid, quest_id))
        op, data = self._wait_packet({0x18B, 0x18D}, after, matches, timeout_s=max(0, end - time.monotonic()))
        if op == 0x18B:
            after = self.sequence
            self.send(0x18C, pack("QI", guid, quest_id))
            _, data = self._wait_packet({0x18D}, after, matches, timeout_s=max(0, end - time.monotonic()))
        r = Reader(data)
        r.skip(12)
        r.string()
        r.string()
        r.skip(9)
        r.skip(r.read("I") * 8)
        choices = [r.read("III")[:2] for _ in range(r.read("I"))]
        if len(choices) > 1 and reward_index is None: raise ValueError(f"Choose a reward index: {choices}")
        reward_index = 0 if reward_index is None else reward_index
        if not 0 <= reward_index < max(1, len(choices)): raise ValueError("Invalid reward index")
        after = self.sequence
        self.send(0x18E, pack("QII", guid, quest_id, reward_index))
        self._wait_packet({0x191}, after, lambda op, b: struct.unpack_from("<I", b)[0] == quest_id,
                          timeout_s=max(0, end - time.monotonic()))

    def completed_quests(self, *, timeout_s=10):
        self.refresh()
        after = self.sequence
        self.send(0x500)
        _, data = self._wait_packet({0x501}, after, timeout_s=timeout_s)
        r = Reader(data)
        return {r.read("I") for _ in range(r.read("I"))}

    def loot(self, target, *, item_ids=None, take_money=True, timeout_s=10):
        """Request selected loot slots. Check inventory for the actual items received."""
        end = time.monotonic() + timeout_s
        self.refresh()
        guid, after = self._guid(target), self.sequence
        self.send(0x15D, pack("Q", guid))
        try:
            _, data = self._wait_packet({0x160}, after, lambda op, b: struct.unpack_from("<Q", b)[0] == guid, timeout_s=timeout_s)
            r = Reader(data)
            r.skip(8)
            if not r.read("B"): raise RuntimeError(f"Loot rejected: code {r.read('B')}")
            money, count = r.read("IB")
            if take_money and money: self.send(0x15E)
            for _ in range(count):
                slot, item, quantity, display, suffix, prop, status = r.read("B5IB")
                if status == 0 and (item_ids is None or item in item_ids): self.send(0x108, pack("B", slot))
        finally:
            after = self.sequence
            self.send(0x15F, pack("Q", guid))
        self._wait_packet({0x161}, after, lambda op, b: struct.unpack_from("<Q", b)[0] == guid,
                          timeout_s=_remaining(end))

    def equip(self, item, *, slot=None):
        if slot is None: return self.send(0x10A, pack("BB", item["bag"], item["slot"]))
        return self.send(0x10F, pack("QB", self._guid(item), slot))

    def use_item(self, item, spell_id, target=None):
        """Request an item's on-use spell by ID; inspect events/inventory for its effect."""
        self.cast_count = (self.cast_count + 1) % 256
        body = pack("BBBIQIBI", item["bag"], item["slot"], self.cast_count, spell_id,
                    self._guid(item), 0, 0, 2 if target is not None else 0)
        if target is not None: body += packed_guid(self._guid(target))
        return self.send(0x0AB, body)

    def release_spirit(self):
        return self.send(0x15A, pack("B", 0))

    def reclaim_corpse(self):
        self.get_player()
        return self.send(0x1D2, pack("Q", self.guid))


# The only generated source; Go owns the authenticated socket and protocol ACKs.
_BRIDGE_GO = r'''
package main

// Embedded by wow.py; uses the client's authenticated session and world loop.
import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/agent-wow/agent-wow/internal/config"
	"github.com/agent-wow/agent-wow/internal/realm"
	"github.com/agent-wow/agent-wow/pkg/auth"
	"github.com/agent-wow/agent-wow/pkg/char"
	"github.com/agent-wow/agent-wow/pkg/database"
	ms "github.com/agent-wow/agent-wow/pkg/modules/session"
	"github.com/agent-wow/agent-wow/pkg/world"
)

type packet struct {
	Opcode  uint16 `json:"opcode"`
	Payload []byte `json:"payload"`
}
type bridge struct {
	mu          sync.Mutex
	packets     []packet
	base, bytes int
	movement    []byte
	s           ms.Session
	acks        chan packet
	done        chan struct{}
	once        sync.Once
	err         error
	cancel      context.CancelFunc
	workers     sync.WaitGroup
}

func (b *bridge) Start(ctx context.Context, s ms.Session) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	b.s = s
	// Start's context is canceled once startup finishes; Stop owns gameplay lifetime.
	ctx, b.cancel = context.WithCancel(context.Background())
	b.workers.Add(1)
	go func() {
		defer b.workers.Done()
		for {
			select {
			case <-ctx.Done():
				return
			case p := <-b.acks:
				if err := s.SendPacket(ctx, uint32(p.Opcode), p.Payload); err != nil {
					b.mu.Lock()
					b.err = err
					b.mu.Unlock()
					b.once.Do(func() { close(b.done) })
					return
				}
			}
		}
	}()
	return nil
}

func guid(p []byte) (uint64, int) {
	if len(p) == 0 {
		return 0, 0
	}
	var g uint64
	n := 1
	for i := 0; i < 8; i++ {
		if p[0]&(1<<i) != 0 {
			if n >= len(p) {
				return 0, 0
			}
			g |= uint64(p[n]) << (8 * i)
			n++
		}
	}
	return g, n
}

func (b *bridge) Publish(op uint16, data []byte) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	p := append([]byte{}, data...)
	b.packets = append(b.packets, packet{op, p})
	b.bytes += len(p)
	if len(b.packets) > 65536 || b.bytes > 64<<20 {
		n := len(b.packets) / 2
		for _, old := range b.packets[:n] {
			b.bytes -= len(old.Payload)
		}
		b.packets = append([]packet(nil), b.packets[n:]...)
		b.base += n
	}
	var ack packet
	g, n := guid(p)
	if (op == 0x236 || op == 0x3e) && len(p) >= 20 {
		b.movement = make([]byte, 30)
		copy(b.movement[10:26], p[4:20])
	}
	switch {
	case op == 0x3e:
		ack = packet{0xdc, []byte{}}
	case op == 0xc7 && n > 0 && g == b.s.Identity.CharacterGUID:
		if len(p) < n+34 {
			return errors.New("truncated teleport")
		}
		b.movement = append([]byte(nil), p[n+4:]...)
		q := append([]byte{}, p[:n+4]...)
		q = binary.LittleEndian.AppendUint32(q, b.s.Clock())
		ack = packet{op, q}
	default:
		for _, pair := range [][2]uint16{{0xe2, 0xe3}, {0xe4, 0xe5}, {0xe6, 0xe7},
			{0x2da, 0x2db}, {0x2dc, 0x2dd}, {0x2de, 0x2df}, {0x381, 0x382}, {0x383, 0x384}, {0x45c, 0x45d}} {
			if op != pair[0] || n == 0 || g != b.s.Identity.CharacterGUID {
				continue
			}
			extra := 0
			if op == 0xe2 {
				extra = 1
			}
			if len(p) != n+8+extra || len(b.movement) < 30 {
				return errors.New("invalid speed change")
			}
			move := append([]byte(nil), b.movement...)
			binary.LittleEndian.PutUint32(move[6:10], b.s.Clock())
			q := append([]byte{}, p[:n+4]...)
			q = append(q, move...)
			q = append(q, p[len(p)-4:]...)
			ack = packet{pair[1], q}
			break
		}
	}
	if ack.Opcode != 0 {
		select {
		case b.acks <- ack:
		default:
			return errors.New("protocol ACK queue full")
		}
	}
	return nil
}
func (b *bridge) FlushPackets(context.Context) error { return nil }
func (b *bridge) InvokeJSON(context.Context, string, json.RawMessage) (json.RawMessage, error) {
	return nil, errors.New("use the bridge HTTP endpoints")
}
func (b *bridge) BeginLogout()                        {}
func (b *bridge) PrepareLogout(context.Context) error { return nil }
func (b *bridge) Resume()                             {}
func (b *bridge) Done() <-chan struct{}               { return b.done }
func (b *bridge) Err() error                          { b.mu.Lock(); defer b.mu.Unlock(); return b.err }
func (b *bridge) Stop() {
	if b.cancel != nil {
		b.cancel()
	}
}
func (b *bridge) Close() error { b.Stop(); b.workers.Wait(); return nil }

func run() error {
	if len(os.Args) != 3 {
		return errors.New("usage: bridge CHARACTER READY_FILE")
	}
	if err := config.Init(""); err != nil {
		return err
	}
	cfg := config.Get()
	db, err := database.NewClient(cfg.DataDir)
	if err != nil {
		return err
	}
	a, err := db.GetSession()
	closeErr := db.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	entry, endEntry := context.WithTimeout(ctx, 30*time.Second)
	defer endEntry()
	ac, err := auth.NewClient(net.JoinHostPort(cfg.AuthServer.Host, strconv.Itoa(cfg.AuthServer.Port)))
	if err != nil {
		return err
	}
	realms, err := ac.ListRealms(entry, a)
	if err != nil {
		return err
	}
	selection, selectedErr := realm.Read(cfg.RealmFilePath)
	if selectedErr != nil && !errors.Is(selectedErr, os.ErrNotExist) {
		return selectedErr
	}
	var candidates []auth.Realm
	for _, r := range realms {
		if r.Selectable() && (selectedErr != nil || r.ID == selection.ID && r.Name == selection.Name) {
			candidates = append(candidates, r)
		}
	}
	if len(candidates) != 1 {
		return errors.New("select exactly one realm with agent-wow realm set first")
	}
	r := candidates[0]
	if cfg.WorldServerAddress != "" {
		r.Address = cfg.WorldServerAddress
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	defer listener.Close()
	cc, err := char.Dial(entry, r, a)
	if err != nil {
		return err
	}
	defer cc.Close()
	characters, err := cc.List(entry)
	if err != nil {
		return err
	}
	var selected char.GUID
	for _, c := range characters {
		if strings.EqualFold(c.Name, os.Args[1]) {
			selected = c.GUID
		}
	}
	if selected == 0 {
		return fmt.Errorf("character %q is not on this account", os.Args[1])
	}
	b := &bridge{acks: make(chan packet, 128), done: make(chan struct{})}
	s, err := cc.EnterWorld(entry, selected, nil, world.Options{Modules: b, EntryTimeout: 15 * time.Second})
	if err != nil {
		return err
	}
	defer s.Close()
	sessionID := strconv.FormatInt(time.Now().UnixNano(), 36)
	mux := http.NewServeMux()
	mux.HandleFunc("/poll", func(w http.ResponseWriter, req *http.Request) {
		after, err := strconv.Atoi(req.URL.Query().Get("after"))
		b.mu.Lock()
		defer b.mu.Unlock()
		if err != nil || after < b.base || after > b.base+len(b.packets) {
			http.Error(w, "packet history lost; reconnect and replay from login", 409)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"packets": b.packets[after-b.base:],
			"next": b.base + len(b.packets), "clock": s.Clock(), "session": sessionID, "guid": strconv.FormatUint(uint64(selected), 10)})
	})
	mux.HandleFunc("/send", func(w http.ResponseWriter, req *http.Request) {
		if req.Method != "POST" {
			http.Error(w, "POST required", 405)
			return
		}
		var p packet
		if err := json.NewDecoder(http.MaxBytesReader(w, req.Body, 65536)).Decode(&p); err != nil {
			http.Error(w, "invalid packet", 400)
			return
		}
		if err := s.SendPacket(req.Context(), uint32(p.Opcode), p.Payload); err != nil {
			http.Error(w, err.Error(), 502)
			return
		}
		if p.Opcode >= 0xb5 && p.Opcode <= 0xc1 || p.Opcode == 0xda || p.Opcode == 0xee {
			g, n := guid(p.Payload)
			if g == uint64(selected) && n > 0 && len(p.Payload) >= n+30 {
				b.mu.Lock()
				b.movement = append([]byte(nil), p.Payload[n:]...)
				b.mu.Unlock()
			}
		}
		fmt.Fprint(w, "{}")
	})
	mux.HandleFunc("/logout", func(w http.ResponseWriter, req *http.Request) {
		if req.Method != "POST" {
			http.Error(w, "POST required", 405)
			return
		}
		if err := s.Logout(req.Context()); err != nil {
			http.Error(w, err.Error(), 502)
			return
		}
		fmt.Fprint(w, "{}")
	})
	server := &http.Server{ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 15 * time.Second,
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Origin") != "" || r.Header.Get("Sec-Fetch-Site") != "" {
				http.Error(w, "local scripts only", 403)
				return
			}
			mux.ServeHTTP(w, r)
		})}
	defer server.Close()
	ready, err := json.Marshal(map[string]any{"url": "http://" + listener.Addr().String(), "session": sessionID, "guid": strconv.FormatUint(uint64(selected), 10)})
	if err != nil {
		return err
	}
	if err := os.WriteFile(os.Args[2]+".tmp", ready, 0600); err != nil {
		return err
	}
	if err := os.Rename(os.Args[2]+".tmp", os.Args[2]); err != nil {
		return err
	}
	defer os.Remove(os.Args[2])
	serveErr := make(chan error, 1)
	go func() { serveErr <- server.Serve(listener) }()
	select {
	case <-s.Done():
		return s.Err()
	case <-ctx.Done():
		return nil
	case err := <-serveErr:
		return err
	}
}

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}
'''
