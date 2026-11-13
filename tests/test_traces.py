import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

import app
from sampledata import samples, survey
from traces import (TraceError, TraceIndex, decode_samples, ibm32_to_float,
                    interpret_coordinates, normalize_config)

class TraceTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = samples()
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root/'index.sqlite3'

    def tearDown(self):
        self.directory.cleanup()

    def write(self, name, data=None):
        path = self.root/name
        path.write_bytes(self.fixtures[name] if data is None else data)
        return path

    def test_variable_length_traces_and_value_sources(self):
        path = self.write('lines.sgy')
        with TraceIndex(self.database) as index:
            summary = index.build(path)
            self.assertTrue(summary['verified'])
            self.assertEqual(summary['trace_count'], 7)
            preview = index.preview(path)
        traces = preview['traces']
        self.assertEqual([t['samples']['value'] for t in traces],
                         [3, 5, 2, 4, 4, 4, 3])
        self.assertTrue(all(t['samples']['source'] == 'trace' for t in traces))
        intervals = [(t['sample_interval_us']['value'], t['sample_interval_us']['source'])
                     for t in traces]
        self.assertTrue(all(source == 'trace' for _, source in intervals[:-1]))
        self.assertEqual(intervals[-1], (2000, 'binary'))  # 道头间隔为 0 时回落卷头默认
        self.assertEqual(traces[0]['offset'], 3600 + 3200)
        self.assertEqual(traces[0]['byte_length'], 240 + 3 * 4)
        self.assertEqual(traces[1]['offset'], traces[0]['offset'] + 240 + 3 * 4)

    def test_fixed_length_uses_binary_defaults(self):
        document = survey([dict(sequence=1, values=[1.0, 2.0, 3.0],
                                declared_samples=99, interval=0),
                           dict(sequence=2, values=[4.0, 5.0, 6.0],
                                declared_samples=1, interval=0)],
                          fixed=1, default_count=3, extended=0)
        path = self.write('fixed.sgy', document)
        with TraceIndex(self.database) as index:
            preview = index.preview(path)
        self.assertEqual([t['samples'] for t in preview['traces']],
                         [dict(value=3, source='binary')] * 2)
        self.assertEqual([t['sample_interval_us'] for t in preview['traces']],
                         [dict(value=2000, source='binary')] * 2)
        self.assertEqual([t['byte_length'] for t in preview['traces']], [252, 252])

    def test_scalar_sign_and_zero(self):
        negative = interpret_coordinates(100000, 200000, -100, 1)
        self.assertAlmostEqual(negative['x'], 1000.0)
        self.assertAlmostEqual(negative['y'], 2000.0)
        self.assertEqual(negative['scalar_factor'], 0.01)
        zero = interpret_coordinates(4356789, 795432, 0, 3)
        self.assertEqual(zero['scalar_factor'], 1.0)
        self.assertEqual(zero['x'], 4356789.0)  # 标量零不得把坐标清零
        self.assertIn('scalar_note', zero)
        positive = interpret_coordinates(10, 20, 50, 99)
        self.assertEqual(positive['x'], 500.0)
        path = self.write('lines.sgy')
        with TraceIndex(self.database) as index:
            preview = index.preview(path)
        by_line = {(t['line'], t['sequence']): t['coordinates']
                   for t in preview['traces']}
        self.assertAlmostEqual(by_line[(7, 1)]['x'], 1000.0)
        self.assertEqual(by_line[(8, 1)]['x'], 4356789.0)

    def test_coordinates_never_assumed_latlon(self):
        path = self.write('lines.sgy')
        with TraceIndex(self.database) as index:
            preview = index.preview(path)
        by_line = {(t['line'], t['sequence']): t['coordinates']
                   for t in preview['traces']}
        for coords in by_line.values():
            self.assertNotIn('latitude', coords)
            self.assertNotIn('longitude', coords)
        self.assertEqual(by_line[(7, 1)]['units'], 'length')
        self.assertEqual(by_line[(8, 1)]['units'], 'decimal_degrees')
        unknown = by_line[(8, 3)]
        self.assertIsNone(unknown['units'])
        self.assertFalse(unknown['units_known'])

    def test_sample_formats_decode(self):
        cases = [(1, [1.0, -2.0, 0.5, 0.0]), (2, [70000, -80000, 3]),
                 (3, [100, -200, 300]), (5, [1.5, -2.5]), (8, [1, -2, 3])]
        with TraceIndex(self.database) as index:
            for format_code, values in cases:
                document = survey([dict(sequence=1, values=values)],
                                  format_code=format_code, extended=0)
                path = self.write('fmt%d.sgy' % format_code, document)
                preview = index.preview(path, samples=len(values))
                self.assertEqual(preview['traces'][0]['samples_preview'], values)
        ibm_path = self.write('ibm.sgy')
        with TraceIndex(self.database) as index:
            preview = index.preview(ibm_path, samples=4)
        self.assertEqual(preview['traces'][0]['samples_preview'], [1.0, -2.0, 0.5, 0.0])
        self.assertEqual(preview['volume']['format_name'], 'ibm32')

    def test_decode_samples_primitives(self):
        self.assertEqual(decode_samples(struct.pack('>3f', 1.0, 2.0, 3.0), 5),
                         [1.0, 2.0, 3.0])
        self.assertEqual(ibm32_to_float(bytes.fromhex('41100000')), 1.0)
        self.assertEqual(ibm32_to_float(bytes.fromhex('c1200000')), -2.0)
        self.assertEqual(ibm32_to_float(bytes.fromhex('40800000')), 0.5)
        self.assertEqual(ibm32_to_float(bytes.fromhex('00000000')), 0.0)
        with self.assertRaises(TraceError):
            decode_samples(b'\x00', 5)
        with self.assertRaises(TraceError):
            decode_samples(b'\x00\x00\x00\x00', 99)

    def test_unknown_sample_format_reported(self):
        document = bytearray(survey([dict(sequence=1, values=[1.0])], extended=0))
        struct.pack_into('>H', document, 3200 + 24, 7)
        path = self.write('unknown.sgy', bytes(document))
        with TraceIndex(self.database) as index:
            with self.assertRaises(TraceError) as caught:
                index.build(path)
            self.assertEqual(caught.exception.code, 'unknown_sample_format')
            preview = index.preview(path)
        self.assertFalse(preview['delivery']['verified'])
        self.assertEqual(preview['delivery']['reason'], 'unknown_sample_format')
        self.assertEqual(preview['traces'], [])

    def test_trace_length_out_of_bounds_and_no_resync(self):
        document = survey([dict(sequence=1, values=[1.0]),
                           dict(sequence=2, values=[2.0], declared_samples=0),
                           dict(sequence=3, values=[3.0])], extended=0)
        path = self.write('corrupt.sgy', document)
        with TraceIndex(self.database) as index:
            with self.assertRaises(TraceError) as caught:
                index.build(path)
            self.assertEqual(caught.exception.code, 'trace_length_out_of_bounds')
            self.assertEqual(caught.exception.details['ordinal'], 1)
            status = index.status(path)
            self.assertEqual(status['trace_count'], 1)  # 不跳过去找下一道
            preview = index.preview(path)
        self.assertEqual([t['ordinal'] for t in preview['traces']], [0])
        oversized = survey([dict(sequence=1, values=[1.0, 2.0], declared_samples=2000)],
                           extended=0)
        path = self.write('oversized.sgy', oversized)
        with TraceIndex(self.database) as index:
            with self.assertRaises(TraceError) as caught:
                index.build(path, {'max_samples': 1000})
            self.assertEqual(caught.exception.code, 'trace_length_out_of_bounds')

    def test_uncertain_byte_order_reported(self):
        path = self.write('lines.sgy')
        with TraceIndex(self.database) as index:
            for bad in (None, 'middle'):
                with self.assertRaises(TraceError) as caught:
                    index.build(path, {'byte_order': bad})
                self.assertEqual(caught.exception.code, 'uncertain_byte_order')
            with self.assertRaises(TraceError):
                index.preview(path, {'byte_order': None})

    def test_little_endian_source(self):
        document = survey([dict(sequence=1, values=[1.0, 2.0], scalar=-10, x=500, y=600)],
                          byte_order='little', extended=0)
        path = self.write('little.sgy', document)
        with TraceIndex(self.database) as index:
            with self.assertRaises(TraceError):
                index.build(path)  # 默认 big 读到未知格式，报告而不猜测
            preview = index.preview(path, {'byte_order': 'little'}, samples=2)
        self.assertEqual(preview['traces'][0]['samples_preview'], [1.0, 2.0])
        self.assertEqual(preview['traces'][0]['coordinates']['x'], 50.0)
        self.assertEqual(preview['volume']['byte_order'], 'little')

    def test_extended_headers_unknown_blocked_and_configured(self):
        document = survey([dict(sequence=1, values=[1.0])],
                          extended=2, extended_declared=-1)
        path = self.write('variable-ext.sgy', document)
        with TraceIndex(self.database) as index:
            with self.assertRaises(TraceError) as caught:
                index.build(path)
            self.assertEqual(caught.exception.code, 'extended_text_headers_unknown')
            summary = index.build(path, {'extended_headers': 2})
            self.assertTrue(summary['verified'])
            self.assertEqual(summary['first_trace_offset'], 3600 + 2 * 3200)

    def test_truncated_delivery_distinguishes_readable(self):
        path = self.write('truncated.sgy')
        with TraceIndex(self.database) as index:
            with self.assertRaises(TraceError) as caught:
                index.build(path)
            self.assertEqual(caught.exception.code, 'truncated')
            preview = index.preview(path, samples=5)
        self.assertFalse(preview['delivery']['verified'])
        self.assertEqual(preview['delivery']['reason'], 'truncated')
        self.assertEqual(len(preview['traces']), 1)
        self.assertEqual(preview['traces'][0]['status'], 'readable')
        self.assertEqual(preview['traces'][0]['samples_preview'], [0.0, 1.0, 2.0])
        complete = self.write('lines.sgy')
        with TraceIndex(self.database) as index:
            preview = index.preview(complete)
        self.assertTrue(preview['delivery']['verified'])
        self.assertTrue(all(t['status'] == 'readable' for t in preview['traces']))

    def test_index_recovers_and_resumes(self):
        path = self.write('lines.sgy')
        with TraceIndex(self.database) as index:
            partial = index.build(path, limit=2)
            self.assertEqual(partial['status'], 'building')
            self.assertEqual(partial['trace_count'], 2)
            done = index.build(path)
            self.assertTrue(done['verified'])
            self.assertEqual(done['trace_count'], 7)
        with TraceIndex(self.database) as reopened:
            recovered = reopened.build(path)
            self.assertTrue(recovered['recovered'])
            self.assertEqual(recovered['trace_count'], 7)
            self.assertEqual(len(reopened.preview(path)['traces']), 7)

    def test_index_bound_to_fingerprint_and_config(self):
        path = self.write('lines.sgy')
        with TraceIndex(self.database) as index:
            first = index.build(path)
            self.assertEqual(first['first_trace_offset'], 6800)
            replacement = survey([dict(line=7, sequence=1, values=[9.0, 9.0],
                                       scalar=1, x=1, y=2)], extended=2)
            path.write_bytes(replacement)  # 同路径替换：旧偏移不得沿用
            second = index.build(path)
            self.assertNotEqual(first['file_digest'], second['file_digest'])
            self.assertFalse(second['recovered'])
            self.assertEqual(second['first_trace_offset'], 10000)
            extracted = index.extract(path, line=7, first=1, last=1)
            self.assertEqual(extracted['traces'][0]['offset'], 10000)
            with path.open('rb') as handle:
                handle.seek(10000)
                self.assertEqual(extracted['raw'], handle.read(extracted['byte_count']))
            with self.assertRaises(TraceError) as caught:
                index.build(path, {'max_samples': 1})
            self.assertEqual(caught.exception.code, 'trace_length_out_of_bounds')
            self.assertTrue(index.status(path)['verified'])  # 默认配置的索引不受影响

    def test_extract_roundtrip_and_ranges(self):
        path = self.write('lines.sgy')
        with TraceIndex(self.database) as index:
            extracted = index.extract(path, line=7, first=2, last=3)
            raw = path.read_bytes()
            start = extracted['traces'][0]['offset']
            end = extracted['traces'][-1]['offset'] + extracted['traces'][-1]['byte_length']
            self.assertEqual(extracted['raw'], raw[start:end])
            self.assertEqual(extracted['sha256'],
                             hashlib.sha256(raw[start:end]).hexdigest())
            self.assertTrue(extracted['delivery_verified'])
            self.assertTrue(extracted['range_complete'])
            self.assertEqual([t['sequence'] for t in extracted['traces']], [2, 3])
            with self.assertRaises(TraceError) as caught:
                index.extract(path, line=99, first=1, last=1)
            self.assertEqual(caught.exception.code, 'trace_range_empty')
            truncated = self.write('truncated.sgy')
            partial = index.extract(truncated, line=1, first=1, last=1)
            self.assertFalse(partial['delivery_verified'])
            self.assertEqual(len(partial['traces']), 1)

    def test_config_validation(self):
        with self.assertRaises(TraceError) as caught:
            normalize_config({'endianness': 'big'})
        self.assertEqual(caught.exception.code, 'config_invalid')
        with self.assertRaises(TraceError):
            normalize_config({'extended_headers': -1})
        with self.assertRaises(TraceError):
            normalize_config({'max_samples': 0})
        self.assertEqual(normalize_config()['byte_order'], 'big')

    def test_cli_index_preview_extract(self):
        path = self.write('lines.sgy')
        database = str(self.database)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(['index', str(path), '--database', database]), 0)
        summary = json.loads(output.getvalue())
        self.assertTrue(summary['verified'])
        self.assertEqual(summary['trace_count'], 7)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(['preview', str(path), '--database', database,
                                       '--line', '7', '--samples', '2']), 0)
        preview = json.loads(output.getvalue())
        self.assertEqual(len(preview['traces']), 4)
        self.assertEqual(preview['traces'][0]['samples_preview'], [1.0, 2.0])
        output_path = self.root/'line7.segy'
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(['extract', str(path), '--database', database,
                                       '--line', '7', '--first', '2', '--last', '3',
                                       '--output', str(output_path)]), 0)
        meta = json.loads(output.getvalue())
        raw = path.read_bytes()
        start = meta['traces'][0]['offset']
        self.assertEqual(output_path.read_bytes(), raw[start:start + meta['byte_count']])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(['extract', str(path), '--database', database,
                                       '--line', '7', '--first', '2', '--last', '3',
                                       '--output', str(output_path)]), 2)
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(app.main(['preview', str(path), '--database', database,
                                       '--config', '{"byte_order": null}']), 2)
        self.assertEqual(json.loads(error.getvalue())['code'], 'uncertain_byte_order')

    def test_trace_demo_command(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(['trace-demo']), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload['delivery']['verified'])
        self.assertEqual(payload['variable_lengths'], [3, 5, 2, 4, 4, 4, 3])
        self.assertEqual(payload['negative_scalar']['factor'], 0.01)
        self.assertEqual(payload['zero_scalar']['factor'], 1.0)
        self.assertNotEqual(payload['zero_scalar']['x'], 0)
        self.assertEqual(payload['interval_sources'], ['binary', 'trace'])
        self.assertTrue(payload['extracted']['roundtrip'])
        self.assertTrue(payload['extracted']['delivery_verified'])
        self.assertFalse(payload['truncated']['verified'])
        self.assertEqual(payload['truncated']['reason'], 'truncated')
        self.assertEqual(payload['truncated']['readable'], [0])

if __name__ == '__main__':
    unittest.main()
