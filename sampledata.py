import struct

def samples():
    text = b'C01 SYNTHETIC SURVEY'.ljust(3200, b' ')
    binary = bytearray(400)
    struct.pack_into('>HHH', binary, 16, 2000, 3, 5)
    struct.pack_into('>H', binary, 300, 0x0100)
    struct.pack_into('>Hh', binary, 302, 0, 1)
    traces = bytearray()
    for sequence, count in [(1, 3), (2, 2)]:
        header = bytearray(240)
        struct.pack_into('>i', header, 0, sequence)
        struct.pack_into('>h', header, 70, -100)
        struct.pack_into('>ii', header, 72, 12345, 54321)
        struct.pack_into('>h', header, 88, 1)
        struct.pack_into('>HH', header, 114, count, 2000)
        traces.extend(header)
        traces.extend(struct.pack('>' + 'f' * count, *[float(value) for value in range(count)]))
    document = text + binary + b'C01 EXTENDED SYNTHETIC'.ljust(3200, b' ') + traces
    return {'sample.sgy': bytes(document), 'truncated.sgy': bytes(document[:-3]),
            'invalid.sgy': bytes(document[:3500])}
