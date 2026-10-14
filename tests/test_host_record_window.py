"""Authored saved-host windows, not real host captures."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import convert_host_record_window as target
from scripts import convert_ardupilot_battery_csv as legacy

ROOT = Path(__file__).resolve().parents[1]


def inspect(time, name):
    return dict(daemon_version='27.3.1', capture_time_us=time,
        inspect=dict(Id=name, State=dict(Status='running', Running=True, Paused=False,
            Restarting=False, OOMKilled=False, Dead=False, ExitCode=0, Error='',
            StartedAt='2026-01-01T00:00:00Z'), Future={'preserve': name}))


class HostWindowTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('MUSUBI_TELEMETRY_READER'), 'actual common reader required')
    def test_large_nvdla_explicit_interval_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); source=p/'tegra.jsonl'
            records=[dict(exporter_version='l4t-36.4.3',capture_time_us=100,report=value)
                for value in ('NVDLA0 25%@700 NVDLA1 0%', 'NVDLA1 512 extra token')]
            raw=(b'x'*30000+b'\n')*560+b''.join(json.dumps(r).encode()+b'\n' for r in records)
            source.write_bytes(raw)
            self.assertGreater(len(raw),16*1024**2)
            for start,expected in ((560,[700000000,None]),(561,[512000000])):
                with self.assertRaises(ValueError):
                    target.convert_path(source,start_record=start,stop_record=start+1,source_format=legacy.TEGRA_FORMAT)
                text,report=target.convert_path(source,start_record=start,stop_record=start+1,
                    source_format=legacy.TEGRA_FORMAT,tegrastats_nvdla=True)
                fields=[o['fields'] for o in self.common(text,p/f'{start}.csv')]
                selected=[f for f in fields if f['tegrastats_record_kind']=='NVDLA']
                self.assertEqual([f['nvdla_frequency_hz'] for f in selected],expected)
                self.assertEqual([f['nvdla_utilization_fraction'] for f in selected], [.25,0] if start==560 else [None])
                self.assertTrue(all(f['source_record_index']==start for f in fields))
                self.assertTrue(all(f['source_file_sha256']=='sha256:'+hashlib.sha256(raw).hexdigest() for f in fields))
                self.assertEqual(report['outside_selection'],'NOT_DECODED_OR_VALIDATED')
                self.assertTrue(report['tegrastats_nvdla'])

    def test_nvdla_selection_cli_and_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); source=p/'tegra.jsonl'; out=p/'output'
            source.write_text(json.dumps(dict(exporter_version='l4t-36.4.3',capture_time_us=10,
                report='NVDLA0 700'))+'\n')
            for options in (dict(tegrastats_nvdla=1,source_format=legacy.TEGRA_FORMAT),
                            dict(tegrastats_nvdla=True,source_format=legacy.DOCKER_FORMAT)):
                with self.assertRaises(ValueError): target.convert_path(source,start_record=0,stop_record=1,**options)
            command=[os.sys.executable,str(ROOT/'scripts/convert_host_record_window.py'),str(source),str(out),
                '--source-format',legacy.TEGRA_FORMAT,'--start-record','0','--stop-record','1','--tegrastats-nvdla']
            subprocess.run(command,check=True,capture_output=True)
            self.assertTrue(json.loads((out/'report.json').read_text())['tegrastats_nvdla'])
            source.write_text(source.read_text().replace('NVDLA0 700','NVDLA0 101%'))
            with self.assertRaises(ValueError): target.convert_path(source,start_record=0,stop_record=1,
                source_format=legacy.TEGRA_FORMAT,tegrastats_nvdla=True)

    def common(self, text, path):
        path.write_text(text)
        run = subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'], str(ROOT /
            'profiles/declared/electrical-schema-reuse/json-profile.toml'),
            str(path), '--allow-equal-time'], capture_output=True, check=True)
        result = json.loads(run.stdout)
        self.assertEqual(result['platform_domain'], 'Unknown')
        for obs in result['observations']:
            self.assertEqual(obs['clock_basis'], 'Unknown')
            self.assertIsNone(obs['anchor_unix_us'])
        return result['observations']

    @unittest.skipUnless(os.environ.get('MUSUBI_TELEMETRY_READER'), 'actual common reader required')
    def test_long_file_two_intervals_common_and_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); source = p/'host.jsonl'
            # Excluded lines are framing-checked, not interpreted as JSON.
            prefix = (b'x'*30000+b'\n')*560
            records = [inspect(10,'first'), inspect(10,'second'), inspect(20,'third')]
            raw = prefix + b''.join(json.dumps(r).encode()+b'\r\n' for r in records)
            source.write_bytes(raw)
            self.assertGreater(len(raw), 16*1024**2)
            with self.assertRaises(ValueError): legacy.convert(raw.decode(), legacy.DOCKER_STATE_FORMAT)
            for start, stop in ((560,562),(562,563)):
                with patch.object(Path, 'read_bytes', side_effect=AssertionError('no whole-file read')):
                    text, report = target.convert_path(source, start_record=start, stop_record=stop,
                        source_format=legacy.DOCKER_STATE_FORMAT,
                        expected_size=len(raw),expected_sha256=hashlib.sha256(raw).hexdigest())
                self.assertEqual(report['input_expectation']['expected_size_bytes'],len(raw))
                observations = self.common(text,p/f'{start}.csv')
                self.assertEqual([o['fields']['source_record_index'] for o in observations], list(range(start,stop)))
                self.assertTrue(all(o['fields']['source_file_sha256']=='sha256:'+hashlib.sha256(raw).hexdigest() for o in observations))
                rows = list(csv.DictReader(io.StringIO(text)))
                original = legacy.convert(''.join(json.dumps(r)+'\n' for r in records[start-560:stop-560]), legacy.DOCKER_STATE_FORMAT)
                stripped = io.StringIO(newline=''); writer = csv.DictWriter(stripped, [k for k in rows[0] if k not in target.EXTRA], lineterminator='\n')
                writer.writeheader(); writer.writerows({k:v for k,v in row.items() if k not in target.EXTRA} for row in rows)
                self.assertEqual(stripped.getvalue(), original)
                self.assertEqual(report['excluded_records'],563-(stop-start))
                self.assertEqual(json.loads(bytes.fromhex(rows[0]['source_record_hex'][4:])),records[start-560])

    @unittest.skipUnless(os.environ.get('MUSUBI_TELEMETRY_READER'), 'actual common reader required')
    def test_tegrastats_multiline_common_and_source_indices(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); source=p/'tegra.jsonl'
            records=[dict(exporter_version='l4t-36.4.3',capture_time_us=t,
                report='RAM 100/1000MB (lfb 2x4MB) CPU [10%@1000,off] CPU@40C') for t in (100,100)]
            source.write_text(''.join(json.dumps(r)+'\n' for r in records))
            text,report=target.convert_path(source,start_record=0,stop_record=2,source_format=legacy.TEGRA_FORMAT)
            rows=list(csv.DictReader(io.StringIO(text))); observations=self.common(text,p/'tegra.csv')
            self.assertGreater(len(rows),2); self.assertEqual(len(rows),len(observations))
            self.assertEqual({r['source_record_index'] for r in rows},{'0','1'})
            self.assertEqual(sum(bool(r['source_record_hex']) for r in rows),2)
            self.assertEqual(report['selected_records'],2)

    def test_refusals_and_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'host.jsonl'; p.write_text(json.dumps(inspect(10,'a'))+'\n'+json.dumps(inspect(9,'b'))+'\n')
            with self.assertRaisesRegex(ValueError,'decreasing'): target.convert_path(p,start_record=0,stop_record=2,source_format=legacy.DOCKER_STATE_FORMAT)
            for start,stop in ((True,1),(0,0),(0,3)):
                with self.assertRaises(ValueError): target.convert_path(p,start_record=start,stop_record=stop,source_format=legacy.DOCKER_STATE_FORMAT)
            with self.assertRaises(ValueError): target.convert_path(p,start_record=0,stop_record=1,source_format='vda-state-3.0')
            p.write_bytes(b'bad\x00\n'+json.dumps(inspect(10,'a')).encode())
            with self.assertRaises(ValueError): target.convert_path(p,start_record=1,stop_record=2,source_format=legacy.DOCKER_STATE_FORMAT)

    @unittest.skipUnless(os.environ.get('MUSUBI_TELEMETRY_READER'), 'actual common reader required')
    def test_stats_cli_fresh_output_and_explicit_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); source=p/'stats.jsonl'; out=p/'result'
            record=dict(exporter_version='27.3.1',os_type='linux',capture_time_us=1,
                stats=dict(ID='saved',CPUPerc='150.00%',MemPerc='25.00%',MemUsage='1MiB / 4MiB',
                    NetIO='1kB / 2MB',BlockIO='0B / 1.5GB',PIDs='3'),future=[1,2])
            source.write_text(json.dumps(record)+'\n')
            command=[os.sys.executable,str(ROOT/'scripts/convert_host_record_window.py'),str(source),str(out),
                '--start-record','0','--stop-record','1','--source-format',legacy.DOCKER_FORMAT,
                '--expected-size',str(source.stat().st_size),
                '--expected-sha256',hashlib.sha256(source.read_bytes()).hexdigest()]
            subprocess.run(command,check=True,capture_output=True)
            original=(out/'observations.csv').read_bytes()
            fields=self.common(original.decode(),p/'common.csv')[0]['fields']
            self.assertEqual(fields['docker_cpu_equivalents_reported'],1.5)
            self.assertEqual(fields['docker_memory_fraction_reported'],.25)
            self.assertNotEqual(subprocess.run(command,capture_output=True).returncode,0)
            self.assertEqual((out/'observations.csv').read_bytes(),original)
            from scripts import convert_vda_load_report as selection
            for bound in ('MAX_INPUT_BYTES','MAX_RECORD_BYTES','MAX_SELECTED_BYTES'):
                with patch.object(selection,bound,1),self.assertRaises(ValueError):
                    target.convert_path(source,start_record=0,stop_record=1,source_format=legacy.DOCKER_FORMAT)
            with patch.object(target,'CSV_LIMIT',1),self.assertRaises(ValueError):
                target.convert_path(source,start_record=0,stop_record=1,source_format=legacy.DOCKER_FORMAT)
            with patch.object(target,'REPORT_LIMIT',1),self.assertRaises(ValueError):
                target.convert_path(source,start_record=0,stop_record=1,source_format=legacy.DOCKER_FORMAT)


if __name__=='__main__': unittest.main()
