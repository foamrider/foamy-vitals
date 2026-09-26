import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("nvidia_stats", Path(__file__).resolve().parents[1] / "nvidia_stats.py")
assert SPEC and SPEC.loader
NVIDIA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NVIDIA)


def device(address="00000000:01:00.0", load="20 %", used="1178 MiB", temperature="51 C", critical="98 C"):
    return f'''<gpu id="{address}"><utilization><gpu_util>{load}</gpu_util></utilization>
    <fb_memory_usage><used>{used}</used><total>8192 MiB</total></fb_memory_usage>
    <temperature><gpu_temp>{temperature}</gpu_temp><gpu_temp_max_threshold>{critical}</gpu_temp_max_threshold>
    <gpu_target_temperature>83 C</gpu_target_temperature><gpu_temp_tlimit>42 C</gpu_temp_tlimit></temperature></gpu>'''


class NvidiaTest(unittest.TestCase):
    def test_driver_sample_and_mib_conversion(self):
        stats = NVIDIA.parse_stats('<nvidia_smi_log>' + device() + '</nvidia_smi_log>')
        self.assertEqual(stats['gpuBusy'], 20)
        self.assertEqual(stats['vramUsed'], 1178 * 1048576)
        self.assertEqual(stats['vramTotal'], 8192 * 1048576)
        self.assertEqual(stats['gpuThermals'], [{'temperature': 51, 'critical': 98, 'label': 'GPU'}])

    def test_unsupported_and_invalid_values_stay_unknown(self):
        for value in ['N/A', '[Not Supported]', 'NaN', '-1', '1e1000', 'bad']:
            stats = NVIDIA.parse_stats('<nvidia_smi_log>' + device(load=value, used=value, temperature=value, critical=value) + '</nvidia_smi_log>')
            self.assertIsNone(stats['gpuBusy'])
            self.assertIsNone(stats['vramUsed'])
            self.assertIsNone(stats['gpuTemp'])
            self.assertIsNone(stats['gpuThermals'][0]['critical'])
        stats = NVIDIA.parse_stats('<nvidia_smi_log>' + device(load='101 %', used='9000 MiB', critical='N/A') + '</nvidia_smi_log>')
        self.assertIsNone(stats['gpuBusy'])
        self.assertIsNone(stats['vramTotal'])
        self.assertIsNone(stats['gpuThermals'][0]['critical'])

    def test_multiple_gpus_keep_one_coherent_device(self):
        xml = '<nvidia_smi_log>' + device(address='00000000:09:00.0', load='99 %') + device() + '</nvidia_smi_log>'
        self.assertEqual(NVIDIA.parse_stats(xml)['gpuBusy'], 20)

    def test_probe_failure_timeout_and_malformed_output(self):
        errors = [FileNotFoundError(), subprocess.TimeoutExpired('nvidia-smi', 1.5), subprocess.CalledProcessError(1, 'nvidia-smi')]
        for error in errors:
            with patch.object(NVIDIA.subprocess, 'run', side_effect=error):
                stats = NVIDIA.collect()
                self.assertIsNone(stats['gpuBusy'])
                self.assertEqual(stats['gpuThermals'], [])
                self.assertTrue(stats['gpuError'])
        for xml in ['broken', '<nvidia_smi_log/>']:
            with patch.object(NVIDIA.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, xml)) as run:
                self.assertTrue(NVIDIA.collect()['gpuError'])
                self.assertEqual(run.call_args.kwargs['timeout'], 1.5)


if __name__ == '__main__':
    unittest.main()
