"""按来源剖面约定解释 SEG-Y 道头与样本，维护绑定文件指纹的可恢复道索引。

约定详见 docs/source-contract.md：
- 卷头默认样本数/间隔位于二进制头第 21/17 字节（1 基），道头覆盖位于第 115/117 字节；
- 坐标 scalar 位于道头第 71 字节：负值表示除以其绝对值，零表示因子 1（不得把坐标清零）；
- 坐标单位位于道头第 89 字节，未知单位不得默认经纬度；
- 样本格式：1 IBM32、2 int32、3 int16、5 IEEE32、8 int8；
- 未知样本格式、越界道长、未声明的扩展文本头数量与不确定端序一律报告并停止，
  不通过寻找下一段看似合理的数值来跳过；
- 索引以（文件指纹 SHA-256，解释配置指纹）为主键，文件被替换后不得沿用旧偏移；
- 扫描、指纹与抽取均为流式读写，不把整文件装进内存。
"""

import hashlib
import json
import sqlite3
import struct
from datetime import datetime, timezone

TEXTUAL_HEADER_BYTES = 3200
BINARY_HEADER_BYTES = 400
TRACE_HEADER_BYTES = 240
VOLUME_HEADER_BYTES = TEXTUAL_HEADER_BYTES + BINARY_HEADER_BYTES

SAMPLE_FORMATS = {1: ('ibm32', 4), 2: ('int32', 4), 3: ('int16', 2), 5: ('ieee32', 4), 8: ('int8', 1)}

# 道头第 89 字节坐标单位代码；未知代码不得按经纬度处理。
COORDINATE_UNITS = {1: 'length', 2: 'seconds_of_arc', 3: 'decimal_degrees', 4: 'dms'}

DEFAULT_CONFIG = dict(byte_order='big', extended_headers='declared', max_samples=1 << 20)

TRACE_COLUMNS = ('ordinal', 'offset', 'byte_length', 'line', 'sequence',
                 'samples', 'samples_source', 'interval_us', 'interval_source',
                 'x_raw', 'y_raw', 'scalar', 'units_code')

_BATCH = 256
_CHUNK = 1 << 20


class TraceError(ValueError):
    """解释或索引失败的结构化原因；code 供调用方分流，details 记录现场。"""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def normalize_config(config=None):
    """合并默认解释配置并校验；未知配置项与非法取值一律报告。"""
    merged = dict(DEFAULT_CONFIG)
    if config is not None:
        if not isinstance(config, dict):
            raise TraceError('config_invalid', '解释配置必须是键值对象')
        unknown = sorted(key for key in config if key not in merged)
        if unknown:
            raise TraceError('config_invalid', '解释配置含未知项', keys=unknown)
        merged.update(config)
    extended = merged['extended_headers']
    if extended != 'declared':
        if isinstance(extended, bool) or not isinstance(extended, int) or extended < 0:
            raise TraceError('config_invalid', '扩展文本头数量必须是非负整数或 declared',
                             value=extended)
    max_samples = merged['max_samples']
    if isinstance(max_samples, bool) or not isinstance(max_samples, int) or max_samples <= 0:
        raise TraceError('config_invalid', '最大道样本数必须是正整数', value=max_samples)
    return merged


def require_byte_order(config):
    """端序必须显式声明；不嗅探、不猜测。"""
    if config.get('byte_order') not in ('big', 'little'):
        raise TraceError('uncertain_byte_order', '端序未显式声明，按约定报告而不猜测',
                         byte_order=config.get('byte_order'))


def config_fingerprint(config):
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def fingerprint(path, chunk_bytes=_CHUNK):
    """流式计算文件指纹与大小，不把整文件读进内存。"""
    digest = hashlib.sha256()
    size = 0
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def ibm32_to_float(chunk, byte_order='big'):
    """IBM 370 浮点：1 位符号、7 位余 64 阶码（16 进制阶）、24 位尾数。"""
    bits = int.from_bytes(chunk, byte_order)
    fraction = bits & 0xFFFFFF
    if fraction == 0:
        return 0.0
    sign = -1.0 if bits & 0x80000000 else 1.0
    exponent = (bits >> 24) & 0x7F
    return sign * (fraction / 16777216.0) * (16.0 ** (exponent - 64))


def decode_samples(raw, format_code, byte_order='big'):
    """按声明格式解码样本；未知格式与字节不对齐一律报告。"""
    if format_code not in SAMPLE_FORMATS:
        raise TraceError('unknown_sample_format', '未知样本格式', format_code=format_code)
    name, width = SAMPLE_FORMATS[format_code]
    if len(raw) % width:
        raise TraceError('truncated', '样本字节数不是格式宽度的整数倍',
                         bytes_available=len(raw), sample_width=width)
    count = len(raw) // width
    if name == 'ibm32':
        return [ibm32_to_float(raw[index * 4:(index + 1) * 4], byte_order)
                for index in range(count)]
    code = {'int32': 'i', 'int16': 'h', 'ieee32': 'f', 'int8': 'b'}[name]
    order = '>' if byte_order == 'big' else '<'
    return list(struct.unpack(order + str(count) + code, raw))


def interpret_coordinates(x_raw, y_raw, scalar, units_code):
    """解释坐标：scalar 负值取除、零取因子 1；单位未知不得默认经纬度。"""
    if scalar > 0:
        factor = float(scalar)
    elif scalar < 0:
        factor = 1.0 / abs(scalar)
    else:
        factor = 1.0
    units = COORDINATE_UNITS.get(units_code)
    result = dict(x=x_raw * factor, y=y_raw * factor, x_raw=x_raw, y_raw=y_raw,
                  scalar=scalar, scalar_factor=factor,
                  units_code=units_code, units=units, units_known=units is not None)
    if scalar == 0:
        result['scalar_note'] = '坐标比例因子为 0，按约定取因子 1，坐标值未清零'
    return result


def parse_volume_header(path, size, config):
    """读取卷头默认值；未知格式、未声明扩展头数量与截断一律报告。"""
    if size < VOLUME_HEADER_BYTES:
        raise TraceError('short_header', 'SEG-Y 卷头不足 3600 字节', file_size=size)
    order = '>' if config['byte_order'] == 'big' else '<'
    with open(path, 'rb') as handle:
        handle.seek(TEXTUAL_HEADER_BYTES)
        binary = handle.read(BINARY_HEADER_BYTES)
    if len(binary) < BINARY_HEADER_BYTES:
        raise TraceError('short_header', 'SEG-Y 二进制卷头不足 400 字节', file_size=size)
    interval_default, = struct.unpack_from(order + 'H', binary, 16)
    samples_default, = struct.unpack_from(order + 'H', binary, 20)
    format_code, = struct.unpack_from(order + 'H', binary, 24)
    fixed_flag, = struct.unpack_from(order + 'H', binary, 302)
    extended_declared, = struct.unpack_from(order + 'h', binary, 304)
    if format_code not in SAMPLE_FORMATS:
        raise TraceError('unknown_sample_format', '未知样本格式', format_code=format_code)
    if config['extended_headers'] == 'declared':
        if extended_declared < 0:
            raise TraceError('extended_text_headers_unknown',
                             '扩展文本头数量未在卷头声明，按约定报告而不猜测首道位置',
                             declared=extended_declared)
        extended_count = extended_declared
    else:
        extended_count = config['extended_headers']
    first_trace_offset = VOLUME_HEADER_BYTES + extended_count * TEXTUAL_HEADER_BYTES
    if first_trace_offset > size:
        raise TraceError('truncated', '扩展文本头越过文件末尾',
                         first_trace_offset=first_trace_offset, file_size=size)
    name, width = SAMPLE_FORMATS[format_code]
    return dict(format_code=format_code, format_name=name, sample_width=width,
                samples_default=samples_default, interval_default=interval_default,
                fixed_length=fixed_flag != 0, extended_count=extended_count,
                first_trace_offset=first_trace_offset, byte_order=config['byte_order'])


def parse_trace_header(header, byte_order='big'):
    order = '>' if byte_order == 'big' else '<'
    sequence, = struct.unpack_from(order + 'i', header, 0)
    line, = struct.unpack_from(order + 'i', header, 8)
    scalar, = struct.unpack_from(order + 'h', header, 70)
    x_raw, y_raw = struct.unpack_from(order + 'ii', header, 72)
    units_code, = struct.unpack_from(order + 'h', header, 88)
    samples, interval = struct.unpack_from(order + 'HH', header, 114)
    return dict(sequence=sequence, line=line, scalar=scalar, x_raw=x_raw, y_raw=y_raw,
                units_code=units_code, samples=samples, interval=interval)


def present_trace(row):
    """索引行转为预览/抽取输出；坐标只按声明单位解释，不假定经纬度。"""
    return dict(ordinal=row['ordinal'], offset=row['offset'], byte_length=row['byte_length'],
                line=row['line'], sequence=row['sequence'],
                samples=dict(value=row['samples'], source=row['samples_source']),
                sample_interval_us=dict(value=row['interval_us'], source=row['interval_source']),
                coordinates=interpret_coordinates(row['x_raw'], row['y_raw'],
                                                  row['scalar'], row['units_code']),
                status='readable')


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


class TraceIndex:
    """绑定文件指纹与解释配置的可恢复道索引；扫描按批提交，中断后可续扫。"""

    def __init__(self, database):
        self.connection = sqlite3.connect(str(database), timeout=5)
        self.connection.executescript(
            'CREATE TABLE IF NOT EXISTS runs('
            'file_digest TEXT NOT NULL, config_digest TEXT NOT NULL,'
            'config_json TEXT NOT NULL, volume_json TEXT NOT NULL,'
            'file_size INTEGER NOT NULL, first_trace_offset INTEGER NOT NULL,'
            'next_offset INTEGER NOT NULL, next_ordinal INTEGER NOT NULL,'
            'trace_count INTEGER NOT NULL, status TEXT NOT NULL,'
            'reason TEXT, detail_json TEXT, updated_utc TEXT NOT NULL,'
            'PRIMARY KEY(file_digest, config_digest));'
            'CREATE TABLE IF NOT EXISTS traces('
            'file_digest TEXT NOT NULL, config_digest TEXT NOT NULL,'
            'ordinal INTEGER NOT NULL, offset INTEGER NOT NULL, byte_length INTEGER NOT NULL,'
            'line INTEGER NOT NULL, sequence INTEGER NOT NULL,'
            'samples INTEGER NOT NULL, samples_source TEXT NOT NULL,'
            'interval_us INTEGER NOT NULL, interval_source TEXT NOT NULL,'
            'x_raw INTEGER NOT NULL, y_raw INTEGER NOT NULL,'
            'scalar INTEGER NOT NULL, units_code INTEGER NOT NULL,'
            'PRIMARY KEY(file_digest, config_digest, ordinal));'
            'CREATE INDEX IF NOT EXISTS traces_line'
            ' ON traces(file_digest, config_digest, line, sequence);')

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def build(self, path, config=None, limit=None):
        """建立或恢复道索引；limit 仅供增量演练，到达 limit 道后停在建中状态。"""
        config = normalize_config(config)
        require_byte_order(config)
        if limit is not None and limit < 1:
            raise TraceError('config_invalid', '增量构建上限必须是正整数', value=limit)
        digest, size = fingerprint(path)
        cdigest = config_fingerprint(config)
        row = self._run(digest, cdigest)
        if row and row['status'] == 'complete':
            return self._summary(row, recovered=True)
        if row and row['status'] == 'failed':
            self._drop(digest, cdigest)
            row = None
        if row is None:
            try:
                volume = parse_volume_header(path, size, config)
            except TraceError as error:
                self._insert_failed(digest, cdigest, config, size, error)
                raise
            row = self._insert_run(digest, cdigest, config, size, volume)
        try:
            complete = self._scan(path, row, config, limit)
        except TraceError as error:
            self._mark(digest, cdigest, 'failed', error)
            raise
        if not complete:
            return self._summary(self._run(digest, cdigest), recovered=False)
        current, _ = fingerprint(path)
        if current != digest:
            error = TraceError('file_changed', '索引期间文件被替换，放弃本次偏移',
                               file_digest=digest, current_digest=current)
            self._mark(digest, cdigest, 'failed', error)
            raise error
        self._mark(digest, cdigest, 'complete', None)
        return self._summary(self._run(digest, cdigest), recovered=False)

    def status(self, path, config=None):
        """按当前文件指纹与解释配置查询索引运行状态；未建过返回 None。"""
        config = normalize_config(config)
        digest, _ = fingerprint(path)
        row = self._run(digest, config_fingerprint(config))
        return self._summary(row, recovered=True) if row else None

    def preview(self, path, config=None, line=None, first=None, last=None, samples=0):
        """列出已索引道；delivery.verified 区分已验证完整交付与仅可读的道。"""
        config = normalize_config(config)
        require_byte_order(config)
        digest, _ = fingerprint(path)
        cdigest = config_fingerprint(config)
        try:
            self.build(path, config)
        except TraceError:
            pass  # 未验证交付仍允许预览已索引的可读道，状态在 delivery 中区分
        row = self._run(digest, cdigest)
        volume = json.loads(row['volume_json']) or None
        entries = []
        with open(path, 'rb') as handle:
            for item in self._select(digest, cdigest, line, first, last):
                entry = present_trace(item)
                if samples and volume is not None:
                    handle.seek(item['offset'] + TRACE_HEADER_BYTES)
                    raw = handle.read(min(samples, item['samples']) * volume['sample_width'])
                    entry['samples_preview'] = decode_samples(raw, volume['format_code'],
                                                              config['byte_order'])
                entries.append(entry)
        self._verify_unchanged(path, digest)
        return dict(file_digest=digest, config_digest=cdigest,
                    delivery=dict(verified=row['status'] == 'complete', status=row['status'],
                                  reason=row['reason'],
                                  details=json.loads(row['detail_json'] or 'null'),
                                  trace_count=row['trace_count'], file_size=row['file_size']),
                    volume=volume, traces=entries)

    def extract(self, path, line, first, last, config=None):
        """抽取指定测线道段并回到原始字节；抽取前后复核文件指纹。"""
        config = normalize_config(config)
        require_byte_order(config)
        if first > last:
            raise TraceError('range_invalid', '道段起止无效', first=first, last=last)
        digest, _ = fingerprint(path)
        cdigest = config_fingerprint(config)
        try:
            self.build(path, config)
        except TraceError:
            pass  # 允许从未验证交付抽取已索引道段，结果带 delivery_verified 标记
        row = self._run(digest, cdigest)
        selected = self._select(digest, cdigest, line, first, last)
        if not selected:
            raise TraceError('trace_range_empty', '指定测线道段没有已索引的道',
                             line=line, first=first, last=last)
        chunks = []
        with open(path, 'rb') as handle:
            for item in selected:
                handle.seek(item['offset'])
                data = handle.read(item['byte_length'])
                if len(data) != item['byte_length']:
                    raise TraceError('file_changed', '抽取期间文件被替换，放弃旧偏移',
                                     offset=item['offset'])
                chunks.append(data)
        self._verify_unchanged(path, digest)
        raw = b''.join(chunks)
        return dict(line=line, first=first, last=last,
                    file_digest=digest, config_digest=cdigest,
                    delivery_verified=row['status'] == 'complete',
                    range_complete=len(selected) == last - first + 1,
                    traces=[present_trace(item) for item in selected],
                    byte_count=len(raw), sha256=hashlib.sha256(raw).hexdigest(), raw=raw)

    def _run(self, digest, cdigest):
        row = self.connection.execute(
            'SELECT file_digest,config_digest,config_json,volume_json,file_size,'
            'first_trace_offset,next_offset,next_ordinal,trace_count,status,reason,'
            'detail_json,updated_utc FROM runs WHERE file_digest=? AND config_digest=?',
            (digest, cdigest)).fetchone()
        if row is None:
            return None
        keys = ('file_digest', 'config_digest', 'config_json', 'volume_json', 'file_size',
                'first_trace_offset', 'next_offset', 'next_ordinal', 'trace_count',
                'status', 'reason', 'detail_json', 'updated_utc')
        return dict(zip(keys, row))

    def _summary(self, row, recovered):
        return dict(file_digest=row['file_digest'], config_digest=row['config_digest'],
                    status=row['status'], verified=row['status'] == 'complete',
                    reason=row['reason'], trace_count=row['trace_count'],
                    file_size=row['file_size'],
                    first_trace_offset=row['first_trace_offset'], recovered=recovered)

    def _drop(self, digest, cdigest):
        with self.connection:
            self.connection.execute('DELETE FROM traces WHERE file_digest=? AND config_digest=?',
                                    (digest, cdigest))
            self.connection.execute('DELETE FROM runs WHERE file_digest=? AND config_digest=?',
                                    (digest, cdigest))

    def _insert_failed(self, digest, cdigest, config, size, error):
        with self.connection:
            self.connection.execute(
                'INSERT OR REPLACE INTO runs(file_digest,config_digest,config_json,volume_json,'
                'file_size,first_trace_offset,next_offset,next_ordinal,trace_count,status,'
                'reason,detail_json,updated_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (digest, cdigest, json.dumps(config, ensure_ascii=False, sort_keys=True),
                 '{}', size, 0, 0, 0, 0, 'failed', error.code,
                 json.dumps(error.details, ensure_ascii=False, sort_keys=True), _utcnow()))

    def _insert_run(self, digest, cdigest, config, size, volume):
        first = volume['first_trace_offset']
        with self.connection:
            self.connection.execute(
                'INSERT INTO runs(file_digest,config_digest,config_json,volume_json,file_size,'
                'first_trace_offset,next_offset,next_ordinal,trace_count,status,reason,'
                'detail_json,updated_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (digest, cdigest, json.dumps(config, ensure_ascii=False, sort_keys=True),
                 json.dumps(volume, ensure_ascii=False, sort_keys=True), size, first,
                 first, 0, 0, 'building', None, None, _utcnow()))
        return self._run(digest, cdigest)

    def _mark(self, digest, cdigest, status, error):
        with self.connection:
            self.connection.execute(
                'UPDATE runs SET status=?, reason=?, detail_json=?, trace_count=next_ordinal,'
                'updated_utc=? WHERE file_digest=? AND config_digest=?',
                (status, error.code if error else None,
                 json.dumps(error.details, ensure_ascii=False, sort_keys=True) if error else None,
                 _utcnow(), digest, cdigest))

    def _flush(self, digest, cdigest, pending, next_offset, next_ordinal):
        with self.connection:
            self.connection.executemany(
                'INSERT INTO traces(file_digest,config_digest,ordinal,offset,byte_length,line,'
                'sequence,samples,samples_source,interval_us,interval_source,x_raw,y_raw,'
                'scalar,units_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                [(digest, cdigest, item['ordinal'], item['offset'], item['byte_length'],
                  item['line'], item['sequence'], item['samples'], item['samples_source'],
                  item['interval_us'], item['interval_source'], item['x_raw'], item['y_raw'],
                  item['scalar'], item['units_code']) for item in pending])
            self.connection.execute(
                'UPDATE runs SET next_offset=?, next_ordinal=?, trace_count=?, updated_utc=?'
                ' WHERE file_digest=? AND config_digest=?',
                (next_offset, next_ordinal, next_ordinal, _utcnow(), digest, cdigest))

    def _scan(self, path, run, config, limit):
        """逐道读 240 字节道头并按道长跳过样本区；失败即停，不重同步。"""
        volume = json.loads(run['volume_json'])
        byte_order = config['byte_order']
        width = volume['sample_width']
        size = run['file_size']
        offset = run['next_offset']
        ordinal = run['next_ordinal']
        digest, cdigest = run['file_digest'], run['config_digest']
        pending = []
        scanned = 0
        complete = False
        try:
            with open(path, 'rb') as handle:
                while True:
                    handle.seek(offset)
                    header = handle.read(TRACE_HEADER_BYTES)
                    if not header:
                        complete = True
                        break
                    if len(header) < TRACE_HEADER_BYTES:
                        raise TraceError('truncated', '道头不足 240 字节',
                                         ordinal=ordinal, offset=offset,
                                         available=len(header))
                    fields = parse_trace_header(header, byte_order)
                    if volume['fixed_length']:
                        samples, samples_source = volume['samples_default'], 'binary'
                    else:
                        samples, samples_source = fields['samples'], 'trace'
                    if not 0 < samples <= config['max_samples']:
                        raise TraceError('trace_length_out_of_bounds',
                                         '道长越界，按约定报告而不寻找下一道',
                                         ordinal=ordinal, offset=offset, samples=samples,
                                         max_samples=config['max_samples'])
                    if fields['interval']:
                        interval, interval_source = fields['interval'], 'trace'
                    else:
                        interval, interval_source = volume['interval_default'], 'binary'
                    byte_length = TRACE_HEADER_BYTES + samples * width
                    if offset + byte_length > size:
                        raise TraceError('truncated', '道数据越过文件末尾',
                                         ordinal=ordinal, offset=offset,
                                         needed=byte_length, available=size - offset)
                    pending.append(dict(ordinal=ordinal, offset=offset, byte_length=byte_length,
                                        line=fields['line'], sequence=fields['sequence'],
                                        samples=samples, samples_source=samples_source,
                                        interval_us=interval, interval_source=interval_source,
                                        x_raw=fields['x_raw'], y_raw=fields['y_raw'],
                                        scalar=fields['scalar'], units_code=fields['units_code']))
                    offset += byte_length
                    ordinal += 1
                    scanned += 1
                    if len(pending) >= _BATCH:
                        self._flush(digest, cdigest, pending, offset, ordinal)
                        pending.clear()
                    if limit is not None and scanned >= limit:
                        break
        except TraceError:
            self._flush(digest, cdigest, pending, offset, ordinal)
            raise
        self._flush(digest, cdigest, pending, offset, ordinal)
        return complete

    def _select(self, digest, cdigest, line, first, last):
        sql = ('SELECT ordinal,offset,byte_length,line,sequence,samples,samples_source,'
               'interval_us,interval_source,x_raw,y_raw,scalar,units_code FROM traces'
               ' WHERE file_digest=? AND config_digest=?')
        params = [digest, cdigest]
        if line is not None:
            sql += ' AND line=?'
            params.append(line)
            if first is not None:
                sql += ' AND sequence>=?'
                params.append(first)
            if last is not None:
                sql += ' AND sequence<=?'
                params.append(last)
        else:
            if first is not None:
                sql += ' AND ordinal>=?'
                params.append(first)
            if last is not None:
                sql += ' AND ordinal<=?'
                params.append(last)
        sql += ' ORDER BY ordinal'
        return [dict(zip(TRACE_COLUMNS, row))
                for row in self.connection.execute(sql, params)]

    def _verify_unchanged(self, path, digest):
        current, _ = fingerprint(path)
        if current != digest:
            raise TraceError('file_changed', '文件在操作期间被替换，拒绝沿用旧偏移',
                             file_digest=digest, current_digest=current)
