import struct

def _ibm_bits(value):
    """Python 浮点转 IBM 370 浮点比特（余 64 阶码、16 进制阶、24 位尾数）。"""
    if value == 0:
        return 0
    sign = 0
    if value < 0:
        sign = 0x80000000
        value = -value
    exponent = 64
    fraction = float(value)
    while fraction >= 1.0:
        fraction /= 16.0
        exponent += 1
    while fraction < 0.0625:
        fraction *= 16.0
        exponent -= 1
    mantissa = int(fraction * 16777216.0 + 0.5)
    if mantissa >= 16777216:
        mantissa >>= 4
        exponent += 1
    return sign | (exponent << 24) | mantissa

def encode_samples(values, format_code=5, byte_order='big'):
    order = '>' if byte_order == 'big' else '<'
    if format_code == 1:
        return b''.join(struct.pack(order + 'I', _ibm_bits(value)) for value in values)
    code = {2: 'i', 3: 'h', 5: 'f', 8: 'b'}[format_code]
    return struct.pack(order + str(len(values)) + code, *values)

def survey(traces, format_code=5, interval=2000, default_count=None, fixed=0,
           extended=1, extended_declared=None, byte_order='big'):
    """按来源约定构造一份 SEG-Y 字节串。

    traces 为 dict 列表，键：line（测线号，道头第 9-12 字节）、sequence（测线内道序号）、
    values（样本值）、scalar、x、y、units、interval（道头间隔，0 表示沿用卷头默认）、
    declared_samples（道头声明样本数，缺省取 len(values)，可用于构造越界道长）。
    卷头布局见 docs/source-contract.md：默认样本数/间隔在第 21/17 字节（1 基）。
    """
    order = '>' if byte_order == 'big' else '<'
    text = b'C01 SYNTHETIC SURVEY'.ljust(3200, b' ')
    binary = bytearray(400)
    if default_count is None:
        default_count = len(traces[0]['values']) if traces else 0
    struct.pack_into(order + 'H', binary, 16, interval)
    struct.pack_into(order + 'H', binary, 20, default_count)
    struct.pack_into(order + 'H', binary, 24, format_code)
    struct.pack_into(order + 'H', binary, 300, 0x0100)
    struct.pack_into(order + 'H', binary, 302, fixed)
    struct.pack_into(order + 'h', binary, 304,
                     extended if extended_declared is None else extended_declared)
    body = bytearray()
    for _ in range(extended):
        body.extend(b'C01 EXTENDED SYNTHETIC'.ljust(3200, b' '))
    for position, spec in enumerate(traces, 1):
        values = spec['values']
        header = bytearray(240)
        struct.pack_into(order + 'i', header, 0, spec.get('sequence', position))
        struct.pack_into(order + 'i', header, 4, position)
        struct.pack_into(order + 'i', header, 8, spec.get('line', 1))
        struct.pack_into(order + 'h', header, 70, spec.get('scalar', 1))
        struct.pack_into(order + 'ii', header, 72, spec.get('x', 0), spec.get('y', 0))
        struct.pack_into(order + 'h', header, 88, spec.get('units', 1))
        struct.pack_into(order + 'H', header, 114, spec.get('declared_samples', len(values)))
        struct.pack_into(order + 'H', header, 116, spec.get('interval', interval))
        body.extend(header)
        body.extend(encode_samples(values, format_code, byte_order))
    return bytes(text + binary + body)

def samples():
    document = survey([
        dict(sequence=1, values=[0.0, 1.0, 2.0], scalar=-100, x=12345, y=54321),
        dict(sequence=2, values=[0.0, 1.0], scalar=-100, x=12345, y=54321),
    ])
    lines = survey([
        dict(line=7, sequence=1, values=[1.0, 2.0, 3.0], scalar=-100, x=100000, y=200000),
        dict(line=7, sequence=2, values=[1.5] * 5, scalar=-100, x=100100, y=200050),
        dict(line=7, sequence=3, values=[2.5, 3.5], scalar=-100, x=100200, y=200100),
        dict(line=7, sequence=4, values=[4.5] * 4, scalar=-100, x=100300, y=200150),
        dict(line=8, sequence=1, values=[5.5] * 4, scalar=0, x=4356789, y=795432, units=3),
        dict(line=8, sequence=2, values=[6.5] * 4, scalar=0, x=4356790, y=795433, units=3),
        dict(line=8, sequence=3, values=[7.5, 8.5, 9.5], scalar=50, x=10, y=20,
             units=99, interval=0),
    ])
    ibm = survey([dict(sequence=1, values=[1.0, -2.0, 0.5, 0.0])], format_code=1, extended=0)
    return {'sample.sgy': document,
            'lines.sgy': lines,
            'ibm.sgy': ibm,
            'truncated.sgy': document[:-3],
            'invalid.sgy': document[:3500]}
