import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("nvidia_stats", Path(__file__).resolve().parents[1] / "nvidia_stats.py")
assert SPEC and SPEC.loader
NVIDIA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NVIDIA)


def device(address="00000000:01:00.0", name="RTX", load="20 %", used="1178 MiB", temperature="51 C", critical="98 C"):
    return f'''<gpu id="{address}"><product_name>{name}</product_name><utilization><gpu_util>{load}</gpu_util></utilization>
    <fb_memory_usage><used>{used}</used><total>8192 MiB</total></fb_memory_usage>
    <temperature><gpu_temp>{temperature}</gpu_temp><gpu_temp_max_threshold>{critical}</gpu_temp_max_threshold>
    <gpu_target_temperature>83 C</gpu_target_temperature><gpu_temp_tlimit>42 C</gpu_temp_tlimit></temperature></gpu>'''


def wrap(*gpus):
    return '<nvidia_smi_log>' + ''.join(gpus) + '</nvidia_smi_log>'


class NvidiaTest(unittest.TestCase):
    def test_driver_sample_and_mib_conversion(self):
        gpu = NVIDIA.parse_devices(wrap(device()))['0000:01:00.0']
        self.assertEqual(gpu['name'], 'RTX')
        self.assertEqual(gpu['busy'], 20)
        self.assertEqual(gpu['vramUsed'], 1178 * 1048576)
        self.assertEqual(gpu['vramTotal'], 8192 * 1048576)
        self.assertEqual(gpu['thermals'], [{'temperature': 51, 'critical': 98, 'label': 'GPU'}])
        self.assertEqual(gpu['error'], '')

    def test_unsupported_and_invalid_values_stay_unknown(self):
        for value in ['N/A', '[Not Supported]', 'NaN', '-1', '1e1000', 'bad']:
            gpu = NVIDIA.parse_devices(wrap(device(load=value, used=value, temperature=value, critical=value)))['0000:01:00.0']
            self.assertIsNone(gpu['busy'])
            self.assertIsNone(gpu['vramUsed'])
            self.assertIsNone(gpu['thermals'][0]['temperature'])
            self.assertIsNone(gpu['thermals'][0]['critical'])
        gpu = NVIDIA.parse_devices(wrap(device(load='101 %', used='9000 MiB', critical='N/A')))['0000:01:00.0']
        self.assertIsNone(gpu['busy'])
        self.assertIsNone(gpu['vramTotal'])
        self.assertIsNone(gpu['thermals'][0]['critical'])

    def test_bus_id_normalization_and_mapping(self):
        self.assertEqual(NVIDIA.pci_address('00000000:01:00.0'), '0000:01:00.0')
        self.assertEqual(NVIDIA.pci_address('00000000:0A:00.0'), '0000:0a:00.0')
        self.assertIsNone(NVIDIA.pci_address('GPU-1234'))
        devices = NVIDIA.parse_devices(wrap(device(address='00000000:09:00.0', load='99 %'), device()))
        self.assertEqual(set(devices), {'0000:09:00.0', '0000:01:00.0'})
        self.assertEqual(devices['0000:09:00.0']['busy'], 99)
        self.assertEqual(devices['0000:01:00.0']['busy'], 20)

    def test_empty_request_never_runs_nvidia_smi(self):
        with patch.object(NVIDIA.subprocess, 'run') as run:
            self.assertEqual(NVIDIA.collect([]), {})
        run.assert_not_called()

    def test_requested_ids_passed_with_i(self):
        xml = wrap(device(address='00000000:09:00.0', load='99 %'), device())
        with patch.object(NVIDIA.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, xml)) as run:
            devices = NVIDIA.collect(['0000:01:00.0', '0000:09:00.0'])
        self.assertEqual(run.call_args.args[0][-2:], ['-i', '00000000:01:00.0,00000000:09:00.0'])
        self.assertEqual(run.call_args.kwargs['timeout'], 1.5)
        self.assertEqual(devices['0000:09:00.0']['busy'], 99)

    def test_missing_and_failed_devices_get_error(self):
        wanted = ['0000:01:00.0', '0000:09:00.0']
        errors = [FileNotFoundError(), subprocess.TimeoutExpired('nvidia-smi', 1.5), subprocess.CalledProcessError(1, 'nvidia-smi')]
        for error in errors:
            with patch.object(NVIDIA.subprocess, 'run', side_effect=error):
                devices = NVIDIA.collect(wanted)
            for address in wanted:
                self.assertIsNone(devices[address]['busy'])
                self.assertEqual(devices[address]['thermals'], [])
                self.assertEqual(devices[address]['error'], NVIDIA.ERROR)
        self.assertEqual(NVIDIA.ERROR, 'NVIDIA readings unavailable. Check nvidia-smi.')
        for xml in ['broken', '<nvidia_smi_log/>', wrap(device())]:
            with patch.object(NVIDIA.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, xml)):
                devices = NVIDIA.collect(wanted)
            self.assertEqual(devices['0000:09:00.0']['error'], NVIDIA.ERROR)

    def test_legacy_output_shape_is_unchanged(self):
        keys = ['gpuBusy', 'vramUsed', 'vramTotal', 'gpuTemp', 'gpuThermals', 'gpuError']
        xml = wrap(device(address='00000000:09:00.0', load='99 %'), device())
        with patch.object(NVIDIA.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, xml)) as run:
            stats = NVIDIA.legacy_stats()
        self.assertEqual(run.call_args.args[0], ['nvidia-smi', '-q', '-x'])
        self.assertEqual(list(stats), keys)
        self.assertEqual(stats['gpuBusy'], 20)
        self.assertEqual(stats['gpuTemp'], 51)
        self.assertEqual(stats['gpuThermals'], [{'temperature': 51, 'critical': 98, 'label': 'GPU'}])
        self.assertEqual(stats['gpuError'], '')
        with patch.object(NVIDIA.subprocess, 'run', side_effect=FileNotFoundError()):
            stats = NVIDIA.legacy_stats()
        self.assertEqual(stats, {'gpuBusy': None, 'vramUsed': None, 'vramTotal': None, 'gpuTemp': None,
                                 'gpuThermals': [], 'gpuError': 'NVIDIA telemetry unavailable; check nvidia-smi.'})
        json.dumps(stats, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
