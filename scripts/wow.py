"""Small, synchronous WoW 3.3.5a helpers for agent-written controllers.

Copy this file beside your script and construct Client(send, poll, clock).
send(opcode, payload) writes one packet; nonblocking poll() drains (opcode, bytes)
pairs; clock() returns the session's client_time_ms. Adapt these three callbacks
to your packet bridge. The bridge owns login, packet history and protocol ACKs.
Use one controller per character. Movement takes ground waypoints from your
own navigator. This file uses only the Python standard library.
"""
from collections import deque
import json
import math
import os
import struct
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


class Client:
    def __init__(self, send, poll, clock, *, guid=None):
        self._send, self._poll, self._clock = send, poll, clock
        self.guid = int(guid) if guid is not None else None
        self.objects, self.names, self.spells = {}, {}, set()
        self.position = self.map_id = self.run_speed = None
        self.events, self.sequence, self.world, self.cast_count = deque(maxlen=256), 0, 0, 0
        self._queried = set()

    def send(self, opcode, payload=b""):
        """Return the bridge's transport result; this does not confirm a game action."""
        return self._send(opcode, payload)

    def refresh(self):
        """Drain the bridge. Callbacks and helpers run on the controller's thread."""
        for opcode, payload in self._poll():
            self._packet(opcode, payload)

    def _packet(self, opcode, data):
        self.sequence += 1
        self.events.append((self.sequence, opcode, data))
        r = Reader(data)
        if opcode in (0x236, 0x03E):  # LOGIN_VERIFY_WORLD, NEW_WORLD
            self.map_id = r.read("I")
            self.position = list(r.read("ffff"))
            self.objects.clear()
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
        elif opcode == 0x0DD:  # MONSTER_MOVE: retain the last reported position.
            guid = r.guid()
            r.skip(1)
            position = list(r.read("fff")) + [0]
            if guid in self.objects: self.objects[guid]["position"] = position
        elif 0x0B5 <= opcode <= 0x0C1 or opcode in (0x0DA, 0x0EE, 0x0C7):
            guid = r.guid()
            if opcode == 0x0C7: r.skip(4)  # TELEPORT_ACK has an order counter.
            _, position = _movement(r)
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
        player.update(position=self.position[:], map_id=self.map_id, run_speed=self.run_speed)
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

    def walk_path(self, points, *, timeout_s=60):
        """Walk ground waypoints supplied by your navigator. Return client position."""
        self.get_player()
        world, end, moving = self.world, time.monotonic() + timeout_s, False
        try:
            for point in points:
                if len(point) != 3 or not all(math.isfinite(v) for v in point): raise ValueError("Expected finite 3D waypoints")
                while math.dist(self.position[:3], point) > .05:
                    player = self.get_player()
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
                    fraction = min(1, speed * duration / distance)
                    position = [current[i] + (point[i] - current[i]) * fraction for i in range(3)]
                    position.append(math.atan2(point[1] - current[1], point[0] - current[0]))
                    self._move(0x0EE if moving else 0x0B5, position, 1)
                    moving = True
            return self.position[:]
        finally:
            self._move(0x0B7, self.position)

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
        if op == 0x130: raise RuntimeError(f"Cast {spell_id} rejected: code {data[5]}")

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
            self.send(0x15F, pack("Q", guid))

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
