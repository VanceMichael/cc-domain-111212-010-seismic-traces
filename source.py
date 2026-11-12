import struct

def inspect_bytes(data):
    if len(data) < 3600:
        raise ValueError('SEG-Y 卷头不足 3600 字节')
    binary = data[3200:3600]
    interval, count, format_code = struct.unpack_from('>HHH', binary, 16)
    return [{'sample_interval': interval, 'samples_default': count,
             'format_code': format_code, 'extended_text_headers': struct.unpack_from('>h', binary, 304)[0],
             'fixed_length': struct.unpack_from('>H', binary, 302)[0]}]
