"""Decoder for Origins TCG `LatestMatch_*.replay` files.

Grammar (reverse-engineered, build 0.6.3):
  file      := u8 version, then (fieldId u8, tagged value)* until EOF
  value     := tag u8, payload
  tags:  01 bool(u8)  02 u8  04 i16le  05 i32le  07 u16le
         0e string (varint len + utf8)
         0f array: elemTag u8, count u16le, elements (raw for 05/0e/others, tagged objects for 11)
         10 object: kind u8, fieldCount u8, then fieldCount x (fieldId u8, tagged value)
"""
import struct


class Reader:
    def __init__(self, data: bytes):
        self.b = data
        self.i = 0

    def u8(self):
        v = self.b[self.i]; self.i += 1; return v

    def varint(self):
        shift = 0; v = 0
        while True:
            c = self.u8(); v |= (c & 0x7F) << shift; shift += 7
            if not c & 0x80: return v

    def take(self, n):
        v = self.b[self.i:self.i + n]; self.i += n; return v

    def string(self):
        n = self.varint(); return self.take(n).decode("utf-8", "replace")

    def raw_elem(self, tag):
        if tag == 0x01: return bool(self.u8())
        if tag == 0x02: return self.u8()
        if tag == 0x03: return struct.unpack("<b", self.take(1))[0]
        if tag == 0x04: return struct.unpack("<h", self.take(2))[0]
        if tag == 0x05: return struct.unpack("<i", self.take(4))[0]
        if tag == 0x07: return struct.unpack("<H", self.take(2))[0]
        if tag == 0x0E: return self.string()
        if tag == 0x11:
            t = self.u8(); assert t == 0x10, f"array object elem tag {t:#x} at {self.i}"
            return self.obj()
        raise ValueError(f"unknown elem tag {tag:#x} at {self.i}")

    def value(self):
        tag = self.u8()
        if tag == 0x0F:
            et = self.u8(); n = struct.unpack("<H", self.take(2))[0]
            return [self.raw_elem(et) for _ in range(n)]
        if tag == 0x10: return self.obj()
        return self.raw_elem(tag)

    def obj(self):
        kind = self.u8(); n = self.u8()
        out = {"_kind": kind}
        for _ in range(n):
            fid = self.u8(); out[fid] = self.value()
        return out


def parse(data: bytes):
    r = Reader(data)
    version = r.u8()
    top = {"_version": version}
    while r.i < len(r.b):
        fid = r.u8(); top[fid] = r.value()
    return top


if __name__ == "__main__":
    import sys, json, pprint
    d = parse(open(sys.argv[1], "rb").read())
    def short(o, depth=0):
        if isinstance(o, dict): return {k: short(v, depth+1) for k, v in o.items()}
        if isinstance(o, list):
            return [short(v, depth+1) for v in o] if len(o) <= 3 or depth < 2 else f"<list len={len(o)} first={short(o[0], depth+1)}>"
        return o
    pprint.pprint(short(d), width=140, sort_dicts=False)
