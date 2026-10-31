import unittest

from brain.domain.agents import AgentName
from brain.domain.compatibility import (
    CompatibilityEvaluator,
    Placement,
    Verdict,
    default_rules,
    estimate_memory_bytes,
)
from brain.domain.errors import InvalidRequest
from tests.fakes import GIB, make_gpu, make_hardware

MEASURED_DISK_SIZE = 2_620_788_260
MEASURED_LOADED_SIZE = 3_309_609_941


def evaluate(size_gib: float | None, hardware, *, installed=False, cloud=False, **kwargs):
    evaluator = CompatibilityEvaluator(default_rules())
    return evaluator.evaluate(
        agent_name="test:1",
        is_cloud=cloud,
        size_bytes=int(size_gib * GIB) if size_gib is not None else None,
        is_installed=installed,
        hardware=hardware,
        **kwargs,
    )


class EstimateTests(unittest.TestCase):
    def test_estimate_is_within_five_percent_of_a_real_measured_load(self):
        estimate = estimate_memory_bytes(MEASURED_DISK_SIZE)
        self.assertAlmostEqual(estimate / MEASURED_LOADED_SIZE, 1.0, delta=0.05)

    def test_tiny_models_do_not_get_a_large_fixed_overhead(self):
        self.assertLess(estimate_memory_bytes(46_000_000), 80_000_000)


class VerdictTests(unittest.TestCase):
    def test_small_model_runs_well_on_a_good_cpu(self):
        result = evaluate(1, make_hardware(ram_gib=8, cores=8))
        self.assertEqual(result.verdict, Verdict.RUNS_WELL)
        self.assertEqual(result.placement, Placement.CPU)
        self.assertTrue(result.available)

    def test_model_larger_than_memory_is_unavailable(self):
        result = evaluate(16, make_hardware(ram_gib=8))
        self.assertEqual(result.verdict, Verdict.UNAVAILABLE)
        self.assertFalse(result.available)
        self.assertIn("memory", [finding.rule for finding in result.findings])

    def test_container_limit_is_named_in_the_explanation(self):
        result = evaluate(4, make_hardware(ram_gib=2, limited_by_cgroup=True))
        self.assertEqual(result.verdict, Verdict.UNAVAILABLE)
        self.assertTrue(any("container limit" in finding.message for finding in result.findings))

    def test_fits_in_ram_but_too_many_gb_per_core_is_limited(self):
        result = evaluate(3, make_hardware(ram_gib=8, cores=4))
        self.assertEqual(result.verdict, Verdict.LIMITED)
        self.assertIn("cpu_speed", [finding.rule for finding in result.findings])

    def test_missing_avx2_lowers_the_speed_threshold_on_x86(self):
        size = 1.2
        with_avx2 = evaluate(size, make_hardware(cores=4, features=("avx2",)))
        without = evaluate(size, make_hardware(cores=4, features=()))
        self.assertEqual(with_avx2.verdict, Verdict.RUNS_WELL)
        self.assertEqual(without.verdict, Verdict.LIMITED)

    def test_arm_needs_no_avx2(self):
        result = evaluate(1, make_hardware(cores=4, architecture="aarch64", features=("asimd",)))
        self.assertEqual(result.verdict, Verdict.RUNS_WELL)

    def test_low_free_memory_warns_but_does_not_block(self):
        result = evaluate(1, make_hardware(ram_gib=8, free_ram_gib=0.5))
        self.assertEqual(result.verdict, Verdict.LIMITED)
        self.assertIn("free_memory", [finding.rule for finding in result.findings])

    def test_not_enough_disk_blocks_a_new_install(self):
        result = evaluate(5, make_hardware(ram_gib=64, disk_free_gib=2, cores=64))
        self.assertEqual(result.verdict, Verdict.UNAVAILABLE)
        self.assertIn("disk", [finding.rule for finding in result.findings])

    def test_disk_does_not_matter_once_installed(self):
        result = evaluate(5, make_hardware(ram_gib=64, disk_free_gib=2, cores=64), installed=True)
        self.assertEqual(result.verdict, Verdict.RUNS_WELL)

    def test_32_bit_arm_is_unavailable(self):
        result = evaluate(0.1, make_hardware(architecture="armv7l"))
        self.assertEqual(result.verdict, Verdict.UNAVAILABLE)

    def test_cloud_is_always_unavailable_however_big_the_machine(self):
        result = evaluate(0.001, make_hardware(ram_gib=1024, cores=128), cloud=True)
        self.assertEqual(result.verdict, Verdict.UNAVAILABLE)
        self.assertIn("local_only", [finding.rule for finding in result.findings])

    def test_unknown_size_is_reported_as_unknown_not_as_fine(self):
        result = evaluate(None, make_hardware())
        self.assertEqual(result.verdict, Verdict.UNKNOWN)
        self.assertTrue(result.available)


class GpuTests(unittest.TestCase):
    def test_model_fitting_in_vram_runs_on_gpu_even_with_weak_cpu(self):
        result = evaluate(6, make_hardware(ram_gib=16, cores=2, gpus=(make_gpu(12),)))
        self.assertEqual(result.placement, Placement.GPU)
        self.assertEqual(result.verdict, Verdict.RUNS_WELL)

    def test_model_larger_than_vram_splits_and_is_limited(self):
        result = evaluate(20, make_hardware(ram_gib=32, gpus=(make_gpu(8),)))
        self.assertEqual(result.placement, Placement.HYBRID)
        self.assertEqual(result.verdict, Verdict.LIMITED)

    def test_vram_and_ram_are_counted_together_for_capacity(self):
        result = evaluate(20, make_hardware(ram_gib=16, gpus=(make_gpu(10),)))
        self.assertTrue(result.available)

    def test_unsupported_gpu_adds_nothing(self):
        result = evaluate(20, make_hardware(ram_gib=8, gpus=(make_gpu(24, supported=False),)))
        self.assertEqual(result.verdict, Verdict.UNAVAILABLE)

    def test_unified_memory_is_not_counted_twice(self):
        # On a 16 GiB Jetson the GPU reports 16 GiB too, but that is the same 16 GiB.
        result = evaluate(20, make_hardware(ram_gib=16, gpus=(make_gpu(16, unified=True),)))
        self.assertEqual(result.verdict, Verdict.UNAVAILABLE)


class MountedTests(unittest.TestCase):
    def test_real_numbers_replace_the_estimate_when_mounted(self):
        result = evaluate(
            2,
            make_hardware(ram_gib=8, free_ram_gib=0.1),
            installed=True,
            mounted_memory_bytes=3 * GIB,
            mounted_vram_bytes=0,
        )
        self.assertEqual(result.estimated_memory_bytes, 3 * GIB)
        self.assertNotIn("free_memory", [finding.rule for finding in result.findings])

    def test_placement_comes_from_what_actually_happened(self):
        hardware = make_hardware(ram_gib=32, gpus=(make_gpu(8),))
        result = evaluate(4, hardware, installed=True, mounted_memory_bytes=5 * GIB, mounted_vram_bytes=2 * GIB)
        self.assertEqual(result.placement, Placement.HYBRID)


class AgentNameTests(unittest.TestCase):
    def test_tag_defaults_to_latest(self):
        self.assertEqual(AgentName.parse("mistral"), "mistral:latest")

    def test_keeps_tag_case(self):
        self.assertEqual(AgentName.parse(" qwen3:4b-q4_K_M "), "qwen3:4b-q4_K_M")

    def test_registry_paths_are_allowed(self):
        name = AgentName.parse("hf.co/user/model:Q4")
        self.assertEqual((name.model, name.tag), ("hf.co/user/model", "Q4"))

    def test_cloud_tags_are_recognized(self):
        for raw in ("gpt-oss:120b-cloud", "qwen3:cloud"):
            self.assertTrue(AgentName.parse(raw).is_cloud, raw)
        for raw in ("qwen3:4b", "cloud-model:4b", "mycloud:latest"):
            self.assertFalse(AgentName.parse(raw).is_cloud, raw)

    def test_rejects_anything_that_could_escape_a_path_or_url(self):
        for raw in ("../../etc/passwd", "a b", "", "-rf", "a:b:c", "a//b", "x" * 201, "name?x=1", "a\nb"):
            with self.assertRaises(InvalidRequest, msg=raw):
                AgentName.parse(raw)
