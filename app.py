import argparse
import base64
import json
from pathlib import Path
import sys
import tempfile

from source import inspect_bytes
from sampledata import samples
from storage import Archive
from traces import TraceError, TraceIndex
from transport import ReplaySource, fetch

MAX_INPUT = 16*1024*1024

def read_input(path):
    with Path(path).open('rb') as handle:
        data = handle.read(MAX_INPUT+1)
    if len(data) > MAX_INPUT:
        raise ValueError('输入超过接收限制')
    return data

def demo():
    fixtures = samples()
    name = next(key for key in fixtures if key.startswith('sample.'))
    data = fixtures[name]
    routes = {'GET /sample':[{'base64':base64.b64encode(data).decode()}]}
    with tempfile.TemporaryDirectory() as directory, ReplaySource(routes) as server:
        status, headers, raw = fetch(server.url + '/sample')
        if status != 200:
            raise ValueError('来源请求失败')
        rows = inspect_bytes(raw)
        with Archive(Path(directory)/'archive.sqlite3') as archive:
            first = archive.save('local-demo', raw, rows)
            repeated = archive.save('local-demo', raw, rows)
            return dict(input=name, bytes=len(raw), captures=len(archive.list()),
                        same_capture=first==repeated, rows=rows, requests=len(server.requests))

def trace_demo():
    """道索引与抽取演示：变长道、负比例、标量零、文件截断与原始字节回读。"""
    fixtures = samples()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        lines_path = root/'lines.sgy'
        lines_path.write_bytes(fixtures['lines.sgy'])
        truncated_path = root/'truncated.sgy'
        truncated_path.write_bytes(fixtures['truncated.sgy'])
        with TraceIndex(root/'trace-index.sqlite3') as index:
            built = index.build(lines_path)
            preview = index.preview(lines_path, samples=2)
            extracted = index.extract(lines_path, line=7, first=2, last=3)
            with lines_path.open('rb') as handle:
                handle.seek(extracted['traces'][0]['offset'])
                original = handle.read(extracted['byte_count'])
            truncated = index.preview(truncated_path, samples=2)
        negative = preview['traces'][0]['coordinates']
        zero_scalar = next(item for item in preview['traces']
                           if item['line'] == 8)['coordinates']
        return dict(
            delivery=dict(verified=built['verified'], traces=built['trace_count']),
            variable_lengths=[item['samples']['value'] for item in preview['traces']],
            sample_sources=sorted({item['samples']['source'] for item in preview['traces']}),
            interval_sources=sorted({item['sample_interval_us']['source']
                                     for item in preview['traces']}),
            negative_scalar=dict(scalar=negative['scalar'],
                                 factor=negative['scalar_factor'], x=negative['x']),
            zero_scalar=dict(scalar=zero_scalar['scalar'],
                             factor=zero_scalar['scalar_factor'],
                             x=zero_scalar['x'], units=zero_scalar['units']),
            extracted=dict(line=7, first=2, last=3, bytes=extracted['byte_count'],
                           sha256=extracted['sha256'],
                           roundtrip=extracted['raw'] == original,
                           delivery_verified=extracted['delivery_verified']),
            truncated=dict(verified=truncated['delivery']['verified'],
                           reason=truncated['delivery']['reason'],
                           readable=[item['ordinal'] for item in truncated['traces']]))

def parse_config(text):
    if text is None:
        return None
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('解释配置必须是 JSON 对象')
    return value

def main(argv=None):
    parser = argparse.ArgumentParser(description='来源检查与原件留存')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'capture'):
        command = commands.add_parser(name)
        command.add_argument('path')
        if name == 'capture':
            command.add_argument('--database', required=True)
            command.add_argument('--source', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--database', required=True)
    fixtures = commands.add_parser('fixtures')
    fixtures.add_argument('directory')
    commands.add_parser('demo')
    commands.add_parser('trace-demo')
    build = commands.add_parser('index')
    build.add_argument('path')
    build.add_argument('--database', required=True)
    build.add_argument('--config')
    preview = commands.add_parser('preview')
    preview.add_argument('path')
    preview.add_argument('--database', required=True)
    preview.add_argument('--config')
    preview.add_argument('--line', type=int)
    preview.add_argument('--first', type=int)
    preview.add_argument('--last', type=int)
    preview.add_argument('--samples', type=int, default=0)
    extract = commands.add_parser('extract')
    extract.add_argument('path')
    extract.add_argument('--database', required=True)
    extract.add_argument('--config')
    extract.add_argument('--line', type=int, required=True)
    extract.add_argument('--first', type=int, required=True)
    extract.add_argument('--last', type=int, required=True)
    extract.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'demo':
            result = demo()
        elif args.command == 'trace-demo':
            result = trace_demo()
        elif args.command == 'fixtures':
            destination = Path(args.directory)
            destination.mkdir(parents=True, exist_ok=True)
            for name, raw in samples().items():
                path = destination/name
                if path.exists():
                    raise ValueError('不覆盖已有夹具文件：'+name)
                path.write_bytes(raw)
            result = dict(files=sorted(samples()))
        elif args.command == 'list':
            with Archive(args.database) as archive:
                result = archive.list()
        elif args.command == 'index':
            with TraceIndex(args.database) as index:
                result = index.build(args.path, parse_config(args.config))
        elif args.command == 'preview':
            with TraceIndex(args.database) as index:
                result = index.preview(args.path, parse_config(args.config),
                                       line=args.line, first=args.first,
                                       last=args.last, samples=args.samples)
        elif args.command == 'extract':
            output = Path(args.output)
            if output.exists():
                raise ValueError('不覆盖已有抽取输出：'+str(output))
            with TraceIndex(args.database) as index:
                result = index.extract(args.path, args.line, args.first, args.last,
                                       parse_config(args.config))
            output.write_bytes(result.pop('raw'))
        else:
            raw = read_input(args.path)
            rows = inspect_bytes(raw)
            if args.command == 'inspect':
                result = rows
            else:
                with Archive(args.database) as archive:
                    result = dict(capture_id=archive.save(args.source,raw,rows), rows=len(rows))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        payload = dict(error=type(error).__name__, reason=str(error))
        if isinstance(error, TraceError):
            payload['code'] = error.code
            payload['details'] = error.details
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
