import tempfile
import unittest
from pathlib import Path

from brain.domain.hardware import Deployment
from brain.infrastructure.hardware.cpu import CpuProbe
from brain.infrastructure.hardware.environment import EnvironmentProbe
from brain.infrastructure.hardware.gpu import DrmProbe, JetsonProbe, NvidiaProbe
from brain.infrastructure.hardware.inspector import CachingInspector, SystemInspector, UnifiedMemoryAdjuster
from brain.infrastructure.hardware.memory import MemoryProbe
from brain.infrastructure.hardware.peripherals import PeripheralProbe
from brain.infrastructure.hardware.sensors import BatteryProbe, HwmonProbe, ThermalZoneProbe
from brain.infrastructure.hardware.storage import ModelDirectoryLocator
from tests.fakes import GIB, FakeInspector, make_gpu, make_hardware, write_tree

MEMINFO = "MemTotal:       264241152 kB\nMemFree: 1000 kB\nMemAvailable:   200000000 kB\n"


class TreeTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)


class MemoryProbeTests(TreeTestCase):
    def test_bare_metal_reports_the_host(self):
        write_tree(self.root, {"proc/meminfo": MEMINFO})
        memory = MemoryProbe(self.root).read()
        self.assertEqual(memory.total_bytes, 264241152 * 1024)
        self.assertFalse(memory.limited_by_cgroup)

    def test_cgroup_v2_limit_replaces_the_host_total(self):
        """A 2 GiB pod on a big server must not be offered agents that only get OOM-killed."""
        write_tree(
            self.root,
            {
                "proc/meminfo": MEMINFO,
                "proc/self/cgroup": "0::/\n",
                "sys/fs/cgroup/memory.max": str(2 * GIB),
                "sys/fs/cgroup/memory.current": str(GIB),
                "sys/fs/cgroup/memory.stat": f"inactive_file {GIB // 2}\nanon 1\n",
            },
        )
        memory = MemoryProbe(self.root).read()
        self.assertEqual(memory.total_bytes, 2 * GIB)
        self.assertTrue(memory.limited_by_cgroup)
        self.assertEqual(memory.host_total_bytes, 264241152 * 1024)
        # Reclaimable page cache is not real usage: 2 GiB - (1 GiB - 0.5 GiB).
        self.assertEqual(memory.available_bytes, int(1.5 * GIB))

    def test_tightest_ancestor_wins_for_nested_cgroups(self):
        write_tree(
            self.root,
            {
                "proc/meminfo": MEMINFO,
                "proc/self/cgroup": "0::/system.slice/brain.service\n",
                "sys/fs/cgroup/system.slice/brain.service/memory.max": "max",
                "sys/fs/cgroup/system.slice/memory.max": str(4 * GIB),
            },
        )
        self.assertEqual(MemoryProbe(self.root).read().total_bytes, 4 * GIB)

    def test_cgroup_v1_limit(self):
        write_tree(
            self.root,
            {
                "proc/meminfo": MEMINFO,
                "sys/fs/cgroup/memory/memory.limit_in_bytes": str(3 * GIB),
                "sys/fs/cgroup/memory/memory.usage_in_bytes": str(GIB),
                "sys/fs/cgroup/memory/memory.stat": "total_inactive_file 0\n",
            },
        )
        memory = MemoryProbe(self.root).read()
        self.assertEqual(memory.total_bytes, 3 * GIB)
        self.assertEqual(memory.available_bytes, 2 * GIB)

    def test_cgroup_v1_huge_value_means_no_limit(self):
        write_tree(
            self.root,
            {
                "proc/meminfo": MEMINFO,
                "sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712",
            },
        )
        self.assertFalse(MemoryProbe(self.root).read().limited_by_cgroup)

    def test_limit_above_host_memory_is_ignored(self):
        write_tree(
            self.root,
            {
                "proc/meminfo": "MemTotal: 1048576 kB\nMemAvailable: 524288 kB\n",
                "proc/self/cgroup": "0::/\n",
                "sys/fs/cgroup/memory.max": str(100 * GIB),
            },
        )
        memory = MemoryProbe(self.root).read()
        self.assertEqual(memory.total_bytes, GIB)
        self.assertFalse(memory.limited_by_cgroup)


class CpuProbeTests(TreeTestCase):
    def probe(self, *, cores=8, machine="x86_64"):
        return CpuProbe(self.root, machine=lambda: machine, core_count=lambda: cores)

    def test_reads_x86_details(self):
        write_tree(
            self.root,
            {
                "proc/cpuinfo": "processor: 0\nmodel name\t: Fancy CPU\nflags\t: fpu sse avx avx2 fma\ncpu MHz\t: 3600.0\n",
            },
        )
        cpu = self.probe().read()
        self.assertEqual(cpu.model, "Fancy CPU")
        self.assertEqual(cpu.features, ("avx", "avx2", "fma"))
        self.assertEqual(cpu.max_frequency_mhz, 3600.0)

    def test_reads_raspberry_pi_style_cpuinfo(self):
        write_tree(
            self.root,
            {
                "proc/cpuinfo": "processor: 0\nFeatures\t: fp asimd\n\nModel\t: Raspberry Pi 5 Model B Rev 1.0\n",
                "sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq": "2400000",
            },
        )
        cpu = self.probe(cores=4, machine="aarch64").read()
        self.assertEqual(cpu.model, "Raspberry Pi 5 Model B Rev 1.0")
        self.assertEqual(cpu.features, ("asimd",))
        self.assertEqual(cpu.max_frequency_mhz, 2400.0)

    def test_container_cpu_quota_lowers_effective_cores(self):
        write_tree(
            self.root,
            {"proc/self/cgroup": "0::/\n", "sys/fs/cgroup/cpu.max": "250000 100000"},
        )
        cpu = self.probe(cores=64).read()
        self.assertEqual(cpu.logical_cores, 64)
        self.assertEqual(cpu.effective_cores, 2.5)

    def test_unlimited_quota(self):
        write_tree(self.root, {"proc/self/cgroup": "0::/\n", "sys/fs/cgroup/cpu.max": "max 100000"})
        self.assertEqual(self.probe(cores=4).read().effective_cores, 4.0)


class GpuProbeTests(TreeTestCase):
    def test_nvidia_output_is_parsed(self):
        output = "NVIDIA RTX 4090, 24564, 24000, 41, 3, 570.1\nTesla K80, 12206, [N/A], [N/A], [N/A], 470\n"
        gpus = NvidiaProbe(run=lambda _argv: output).read()
        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[0].vram_total_bytes, 24564 * 1024**2)
        self.assertEqual(gpus[0].temperature_c, 41)
        self.assertIsNone(gpus[1].vram_free_bytes)

    def test_no_nvidia_smi_means_no_gpu(self):
        self.assertEqual(NvidiaProbe(run=lambda _argv: None).read(), ())

    def test_amd_needs_the_rocm_driver_to_count(self):
        write_tree(
            self.root,
            {
                "sys/class/drm/card0/device/vendor": "0x1002",
                "sys/class/drm/card0/device/mem_info_vram_total": str(16 * GIB),
                "sys/class/drm/card0/device/mem_info_vram_used": str(GIB),
            },
        )
        gpu = DrmProbe(self.root).read()[0]
        self.assertFalse(gpu.supported)
        write_tree(self.root, {"dev/kfd": ""})
        gpu = DrmProbe(self.root).read()[0]
        self.assertTrue(gpu.supported)
        self.assertEqual(gpu.vram_free_bytes, 15 * GIB)

    def test_intel_is_listed_but_not_used(self):
        write_tree(self.root, {"sys/class/drm/card1/device/vendor": "0x8086"})
        gpu = DrmProbe(self.root).read()[0]
        self.assertEqual(gpu.vendor, "Intel")
        self.assertFalse(gpu.supported)

    def test_connector_entries_are_not_mistaken_for_cards(self):
        write_tree(self.root, {"sys/class/drm/card0-HDMI-A-1/device/vendor": "0x1002"})
        self.assertEqual(DrmProbe(self.root).read(), ())

    def test_jetson_shares_ram_with_the_gpu(self):
        write_tree(self.root, {"etc/nv_tegra_release": "# R36", "proc/device-tree/model": "Jetson Orin\x00"})
        gpu = JetsonProbe(self.root).read()[0]
        self.assertEqual(gpu.name, "Jetson Orin")
        self.assertTrue(gpu.unified_memory)
        profile = make_hardware(ram_gib=16, gpus=(gpu,))
        adjusted = UnifiedMemoryAdjuster(FakeInspector(profile)).snapshot()
        self.assertEqual(adjusted.gpus[0].vram_total_bytes, 16 * GIB)
        self.assertEqual(adjusted.memory.total_bytes, 16 * GIB)


class SensorTests(TreeTestCase):
    def test_thermal_zone(self):
        write_tree(
            self.root,
            {
                "sys/class/thermal/thermal_zone0/type": "cpu-thermal",
                "sys/class/thermal/thermal_zone0/temp": "48500",
                "sys/class/thermal/cooling_device0/type": "fan",
            },
        )
        sensors = ThermalZoneProbe(self.root).read()
        self.assertEqual([(sensor.label, sensor.value) for sensor in sensors], [("cpu-thermal", 48.5)])

    def test_hwmon_scales_each_kind(self):
        write_tree(
            self.root,
            {
                "sys/class/hwmon/hwmon0/name": "nct6775",
                "sys/class/hwmon/hwmon0/temp1_input": "41000",
                "sys/class/hwmon/hwmon0/temp1_label": "CPU",
                "sys/class/hwmon/hwmon0/fan2_input": "1200",
                "sys/class/hwmon/hwmon0/in0_input": "1050",
                "sys/class/hwmon/hwmon0/power1_input": "15000000",
                "sys/class/hwmon/hwmon0/weird_input": "1",
            },
        )
        found = {sensor.kind: sensor for sensor in HwmonProbe(self.root).read()}
        self.assertEqual(found["temperature"].value, 41)
        self.assertEqual(found["temperature"].label, "nct6775 CPU")
        self.assertEqual(found["fan"].value, 1200)
        self.assertEqual(found["voltage"].value, 1.05)
        self.assertEqual(found["power"].value, 15)
        self.assertEqual(len(found), 4)

    def test_battery(self):
        write_tree(self.root, {"sys/class/power_supply/BAT0/capacity": "87", "sys/class/power_supply/AC/online": "1"})
        self.assertEqual([sensor.value for sensor in BatteryProbe(self.root).read()], [87.0])

    def test_nothing_present_is_not_an_error(self):
        self.assertEqual(ThermalZoneProbe(self.root).read(), ())
        self.assertEqual(HwmonProbe(self.root).read(), ())


class PeripheralTests(TreeTestCase):
    def test_finds_what_a_raspberry_pi_exposes(self):
        write_tree(
            self.root,
            {"dev/gpiochip0": "", "dev/i2c-1": "", "dev/ttyUSB0": "", "dev/video0": "", "dev/sda": ""},
        )
        kinds = {(item.kind, item.path) for item in PeripheralProbe(self.root).read()}
        self.assertEqual(
            kinds,
            {("gpio", "/dev/gpiochip0"), ("i2c", "/dev/i2c-1"), ("serial", "/dev/ttyUSB0"), ("camera", "/dev/video0")},
        )


class EnvironmentTests(TreeTestCase):
    def test_kubernetes_wins_over_container(self):
        write_tree(self.root, {".dockerenv": ""})
        probe = EnvironmentProbe(self.root, environ={"KUBERNETES_SERVICE_HOST": "10.0.0.1"})
        self.assertEqual(probe.read().deployment, Deployment.KUBERNETES)

    def test_docker(self):
        write_tree(self.root, {".dockerenv": ""})
        self.assertEqual(EnvironmentProbe(self.root, environ={}).read().deployment, Deployment.CONTAINER)

    def test_virtual_machine(self):
        write_tree(self.root, {"proc/cpuinfo": "flags: fpu hypervisor avx2\n"})
        self.assertEqual(EnvironmentProbe(self.root, environ={}).read().deployment, Deployment.VIRTUAL_MACHINE)

    def test_bare_metal_raspberry_pi(self):
        write_tree(
            self.root,
            {
                "proc/device-tree/model": "Raspberry Pi 5 Model B Rev 1.0\x00",
                "etc/os-release": 'PRETTY_NAME="Debian GNU/Linux 12"\n',
            },
        )
        info = EnvironmentProbe(self.root, environ={}).read()
        self.assertEqual(info.deployment, Deployment.BARE_METAL)
        self.assertEqual(info.board, "Raspberry Pi 5 Model B Rev 1.0")
        self.assertEqual(info.operating_system, "Debian GNU/Linux 12")

    def test_placeholder_board_names_are_hidden(self):
        write_tree(self.root, {"sys/class/dmi/id/product_name": "To Be Filled By O.E.M.\n"})
        self.assertIsNone(EnvironmentProbe(self.root, environ={}).read().board)


class ModelDirectoryTests(TreeTestCase):
    def test_environment_setting_wins(self):
        locator = ModelDirectoryLocator(environ={"OLLAMA_MODELS": "/mnt/big"}, home=self.root)
        self.assertEqual(locator.locate(), Path("/mnt/big"))

    def test_system_service_location(self):
        service = self.root / "service"
        service.mkdir()
        locator = ModelDirectoryLocator(environ={}, home=self.root, service_home=service)
        self.assertEqual(locator.locate(), service / ".ollama/models")

    def test_user_location_when_there_is_no_service(self):
        locator = ModelDirectoryLocator(environ={}, home=self.root, service_home=self.root / "none")
        self.assertEqual(locator.locate(), self.root / ".ollama/models")


class _FixedProbe:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class _BrokenProbe:
    def read(self):
        raise RuntimeError("driver exploded")


class InspectorTests(unittest.TestCase):
    def test_one_broken_optional_probe_does_not_hide_the_others(self):
        base = make_hardware()
        inspector = SystemInspector(
            cpu=_FixedProbe(base.cpu),
            memory=_FixedProbe(base.memory),
            disk=_FixedProbe(base.disk),
            environment=_FixedProbe(base.environment),
            gpus=(_BrokenProbe(), _FixedProbe((make_gpu(8),)), _FixedProbe((make_gpu(4),))),
        )
        with self.assertLogs("brain.infrastructure.hardware.inspector", level="WARNING"):
            profile = inspector.snapshot()
        self.assertEqual(profile.vram_total_bytes, 12 * GIB)

    def test_cache_expires(self):
        now = [0.0]
        inner = FakeInspector()
        calls = []
        original = inner.snapshot
        inner.snapshot = lambda: (calls.append(1), original())[1]
        cached = CachingInspector(inner, ttl_seconds=2, monotonic=lambda: now[0])
        cached.snapshot()
        cached.snapshot()
        self.assertEqual(len(calls), 1)
        now[0] = 3
        cached.snapshot()
        self.assertEqual(len(calls), 2)
