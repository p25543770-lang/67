"""Tests for the Microsoft Kinect SDK 1.8 adapter — no hardware required.

The .NET layer is replaced with a fake ``clr`` module and a fake
``Microsoft.Kinect`` namespace, so depth frames, missing SDK, absent sensor,
open failures and resource release are exercised deterministically.
"""
from __future__ import annotations

import array
import contextlib
import io
import importlib.machinery
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from sensor_drivers import kinect_sdk_v1 as sdk

try:  # NumPy — обязательная зависимость адаптера; в CI она ставится перед тестами.
    import numpy as np
except Exception:  # pragma: no cover - тесты без NumPy бессмысленны
    np = None

REQUIRES_NUMPY = unittest.skipIf(np is None, "NumPy не установлен: тесты адаптера Kinect SDK требуют NumPy")


def make_module(name, **attributes):
    module = types.ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


class FakeFrame:
    """Minimal stand-in for DepthImageFrame / ColorImageFrame."""

    def __init__(self, pixels, width=640, height=480):
        self._pixels = pixels
        self.Width = width
        self.Height = height
        self.PixelDataLength = len(pixels)
        self.disposed = False
        self.copied = False

    def CopyPixelDataTo(self, target):
        if len(target) != self.PixelDataLength:
            raise ValueError("incorrect managed array length")
        self.copied = True
        if isinstance(target, array.array):
            target[:] = array.array("h", self._pixels)
        else:
            target[:] = bytes(self._pixels)

    def Dispose(self):
        self.disposed = True


class FakeStream:
    def __init__(self, frames=None):
        self.frames = list(frames or [])
        self.enabled_with = "not-called"
        self.disabled = False
        self.timeouts: list[int] = []

    def Enable(self, image_format=None):
        self.enabled_with = image_format

    def Disable(self):
        self.disabled = True

    def OpenNextFrame(self, timeout_ms):
        self.timeouts.append(timeout_ms)
        return self.frames.pop(0) if self.frames else None


class FakeSensor:
    def __init__(self, depth_frames=None, color_frames=None, *, fail_start=False):
        self.DepthStream = FakeStream(depth_frames)
        self.ColorStream = FakeStream(color_frames)
        self.started = False
        self.stopped = False
        self.disposed = False
        self.IsRunning = False
        self.fail_start = fail_start

    def Start(self):
        if self.fail_start:
            raise RuntimeError("device busy")
        self.started = True
        self.IsRunning = True

    def Stop(self):
        self.stopped = True
        self.IsRunning = False

    def Dispose(self):
        self.disposed = True


def make_namespace(sensors):
    class DepthImageFormat:
        Resolution640x480Fps30 = "depth-640x480"

    class ColorImageFormat:
        RgbResolution640x480Fps30 = "color-640x480"

    class KinectSensor:
        KinectSensors = list(sensors)

    return make_module(
        "Microsoft.Kinect",
        KinectSensor=KinectSensor,
        DepthImageFormat=DepthImageFormat,
        ColorImageFormat=ColorImageFormat,
    )


def make_clr(*, fail_for=None, calls=None):
    """Fake pythonnet ``clr`` module that records AddReference calls."""
    module = make_module("clr")
    recorded = calls if calls is not None else []

    def AddReference(name, *args):
        recorded.append(name)
        if fail_for is not None and fail_for(name):
            raise RuntimeError(f"assembly not found: {name}")

    module.AddReference = AddReference
    module.calls = recorded
    return module


def depth_pixels(mm: int, width=640, height=480):
    """DepthImageFrame pixel layout: millimetres shifted left by 3 bits."""
    return np.full(width * height, mm << 3, dtype=np.int16)


def color_pixels(width=640, height=480):
    """BGRA bytes: red pixel (B=0, G=0, R=255, A=255)."""
    return bytes(bytearray([0, 0, 255, 255]) * (width * height))


class AdapterEnvironment:
    """Context manager: fake .NET world plus an on-disk Microsoft.Kinect.dll."""

    def __init__(self, *, sensors=None, depth_frames=None, color_frames=None, with_assembly=True,
                 add_reference=None, fail_start=False):
        self.sensors = sensors if sensors is not None else [
            FakeSensor(depth_frames, color_frames, fail_start=fail_start)
        ]
        self.namespace = make_namespace(self.sensors)
        if add_reference is None and not with_assembly:
            # Без SDK сборку нельзя загрузить ни по имени, ни с диска.
            add_reference = lambda _name: True  # noqa: E731
        self.clr = make_clr(fail_for=add_reference)
        self.calls = self.clr.calls
        self.with_assembly = with_assembly
        self._tempdir = None

    def __enter__(self):
        class FakeArray:
            @staticmethod
            def CreateInstance(element_type, count):
                if element_type == "Int16":
                    return array.array("h", [0]) * count
                if element_type == "Byte":
                    return bytearray(count)
                raise ValueError("unexpected managed array type")

        system = make_module("System", Array=FakeArray, Int16="Int16", Byte="Byte")
        modules = {"clr": self.clr, "Microsoft.Kinect": self.namespace, "System": system}
        self._patch_modules = mock.patch.dict(sys.modules, modules)
        self._patch_modules.start()
        env: dict[str, str] = {}
        if self.with_assembly:
            self._tempdir = tempfile.TemporaryDirectory()
            assembly = os.path.join(self._tempdir.name, "Microsoft.Kinect.dll")
            with open(assembly, "wb") as handle:
                handle.write(b"fake assembly")
            env[sdk.SDK_ASSEMBLY_ENV] = assembly
            self.assembly = assembly
        else:
            self.assembly = None
        self.adapter = sdk.KinectSdkV1Adapter(env=env, sleep=lambda _seconds: None)
        return self

    def __exit__(self, *exc_info):
        self.adapter.close()
        self._patch_modules.stop()
        if self._tempdir is not None:
            self._tempdir.cleanup()


@REQUIRES_NUMPY
class CaptureTests(unittest.TestCase):
    def test_depth_frame_is_converted_to_metres_and_points(self):
        depth = FakeFrame(depth_pixels(2000))
        color = FakeFrame(color_pixels())
        with AdapterEnvironment(depth_frames=[depth], color_frames=[color]) as env:
            frame = env.adapter.capture()

        self.assertIsNotNone(frame)
        self.assertEqual(frame.depth_mm.shape, (480, 640))
        self.assertEqual(int(frame.depth_mm[0, 0]), 2000)
        # 160x120 подвыборка; все пиксели валидны и лежат на 2 м.
        self.assertEqual(frame.points.shape, (160 * 120, 3))
        self.assertAlmostEqual(float(frame.points[0, 2]), -2.0, places=3)
        self.assertEqual(frame.color_rgb.shape, (480, 640, 3))
        # BGRA → RGB: красный пиксель остаётся красным.
        self.assertEqual(list(frame.color_rgb[0, 0]), [255, 0, 0])
        # Кадры .NET освобождаются, сенсор открыт и потоки включены.
        self.assertTrue(depth.copied)
        self.assertTrue(color.copied)
        self.assertTrue(depth.disposed)
        self.assertTrue(color.disposed)
        sensor = env.sensors[0]
        self.assertTrue(sensor.started)
        self.assertEqual(sensor.DepthStream.enabled_with, "depth-640x480")
        self.assertEqual(sensor.ColorStream.enabled_with, "color-640x480")
        # Опрос кадров без .NET-событий: OpenNextFrame(0).
        self.assertTrue(sensor.DepthStream.timeouts)
        self.assertTrue(all(value == 0 for value in sensor.DepthStream.timeouts))
        self.assertIn("Microsoft.Kinect", env.calls)

    def test_packed_depth_is_exact_integer_millimetres(self):
        raw = np.array([999 << 3, (1001 << 3) | 3, 1501 << 3, 1999 << 3], dtype=np.uint16)
        frame = sdk.depth_and_color_to_frame(raw, 4, 1, None, None, None, captured_at=5.0)
        np.testing.assert_array_equal(frame.depth_mm, [[999, 1001, 1501, 1999]])
        self.assertEqual(frame.captured_at, 5.0)

    def test_missing_rgb_does_not_delay_or_retimestamp_depth_by_hundreds_of_ms(self):
        clock = [10.0]
        with AdapterEnvironment(depth_frames=[FakeFrame(depth_pixels(1200))], color_frames=[]) as env:
            env.adapter._clock = lambda: clock[0]
            env.adapter._sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
            frame = env.adapter.capture()
        self.assertEqual(frame.captured_at, 10.0)  # before the short RGB wait
        self.assertLessEqual(clock[0] - frame.captured_at, 0.02)
        self.assertIn("RGB", frame.warning)

    def test_zero_depth_pixels_are_not_turned_into_points(self):
        pixels = np.zeros(640 * 480, dtype=np.int16)
        pixels[0] = 1500 << 3  # один валидный пиксель
        with AdapterEnvironment(depth_frames=[FakeFrame(pixels)]) as env:
            frame = env.adapter.capture()
        self.assertEqual(int(frame.depth_mm[0, 0]), 1500)
        self.assertEqual(int(frame.depth_mm[1, 0]), 0)
        self.assertGreaterEqual(len(frame.points), 1)
        self.assertTrue(np.all(frame.points[:, 2] <= 0))

    def test_rgb_failure_keeps_depth_frame(self):
        with AdapterEnvironment(depth_frames=[FakeFrame(depth_pixels(1200))], color_frames=[]) as env:
            env.adapter.color_timeout = 0.01
            frame = env.adapter.capture()
        self.assertIsNone(frame.color_rgb)
        self.assertEqual(int(frame.depth_mm[10, 10]), 1200)
        self.assertIn("не вернул RGB-кадр", frame.warning)
        self.assertIn("Проверьте", frame.warning)

    def test_rgb_copy_failure_preserves_exact_warning_and_logs_it(self):
        bad_color = FakeFrame(color_pixels())
        bad_color.CopyPixelDataTo = mock.Mock(side_effect=RuntimeError("RGB buffer failure"))
        output = io.StringIO()
        with AdapterEnvironment(depth_frames=[FakeFrame(depth_pixels(1200))], color_frames=[bad_color]) as env:
            with contextlib.redirect_stdout(output):
                frame = env.adapter.capture()
        self.assertIsNone(frame.color_rgb)
        self.assertEqual(int(frame.depth_mm[0, 0]), 1200)
        self.assertIn("RGB buffer failure", frame.warning)
        self.assertIn("Проверьте USB и SDK", frame.warning)
        self.assertIn("RGB buffer failure", output.getvalue())
        self.assertTrue(bad_color.disposed)

    def test_frame_timeout_is_reported_and_sensor_stays_open(self):
        with AdapterEnvironment(depth_frames=[]) as env:
            env.adapter.frame_timeout = 0.001
            with self.assertRaises(sdk.KinectSdkUnavailable) as raised:
                env.adapter.capture()
            self.assertEqual(raised.exception.code, "frame_timeout")
            sensor = env.sensors[0]
            self.assertTrue(sensor.started)
            self.assertFalse(sensor.disposed)
            env.adapter.close()
            self.assertTrue(sensor.stopped)
            self.assertTrue(sensor.disposed)


@REQUIRES_NUMPY
class SensorLifecycleTests(unittest.TestCase):
    def test_close_releases_streams_and_sensor_and_is_idempotent(self):
        with AdapterEnvironment(depth_frames=[FakeFrame(depth_pixels(1000))]) as env:
            env.adapter.capture()
            sensor = env.sensors[0]
            env.adapter.close()
            self.assertTrue(sensor.stopped)
            self.assertTrue(sensor.DepthStream.disabled)
            self.assertTrue(sensor.ColorStream.disabled)
            self.assertTrue(sensor.disposed)
            # Повторное закрытие не должно падать и не трогает сенсор снова.
            sensor.stopped = False
            env.adapter.close()
            self.assertFalse(sensor.stopped)

    def test_open_failure_surfaces_error_and_releases_resources(self):
        with AdapterEnvironment(depth_frames=[], fail_start=True) as env:
            with self.assertRaises(sdk.KinectSdkUnavailable) as raised:
                env.adapter.capture()
            self.assertEqual(raised.exception.code, "open_failed")
            sensor = env.sensors[0]
            self.assertFalse(sensor.started)
            self.assertTrue(sensor.DepthStream.disabled)
            self.assertTrue(sensor.ColorStream.disabled)
            self.assertTrue(sensor.disposed)

    def test_capture_recovers_after_a_frame_timeout(self):
        with AdapterEnvironment(depth_frames=[]) as env:
            env.adapter.frame_timeout = 0.001
            with self.assertRaises(sdk.KinectSdkUnavailable) as raised:
                env.adapter.capture()
            self.assertEqual(raised.exception.code, "frame_timeout")
            # Сенсор остаётся открытым: следующий вызов получает кадр без переоткрытия.
            sensor = env.sensors[0]
            sensor.DepthStream.frames.append(FakeFrame(depth_pixels(1500)))
            frame = env.adapter.capture()
            self.assertEqual(int(frame.depth_mm[0, 0]), 1500)
            self.assertEqual(sensor.DepthStream.timeouts.count(0) > 1, True)

    def test_frame_rate_is_limited(self):
        slept: list[float] = []
        clock = [0.0]

        def fake_sleep(seconds):
            slept.append(seconds)
            clock[0] += seconds

        with AdapterEnvironment(
            depth_frames=[FakeFrame(depth_pixels(1000)), FakeFrame(depth_pixels(1000))],
            color_frames=[FakeFrame(color_pixels()), FakeFrame(color_pixels())],
        ) as env:
            env.adapter._sleep = fake_sleep
            env.adapter._clock = lambda: clock[0]
            env.adapter.max_fps = 10.0
            env.adapter.capture()
            env.adapter.capture()
            env.adapter.close()
        # Второй кадр не должен читаться чаще, чем 1/10 с.
        self.assertTrue(any(seconds >= 0.09 for seconds in slept), slept)


class MissingComponentsTests(unittest.TestCase):
    def test_missing_sdk_points_to_kinect_sdk_1_8_and_does_not_install_anything(self):
        with AdapterEnvironment(with_assembly=False) as env:
            adapter = env.adapter
            self.assertFalse(adapter.dependencies_available())
            self.assertIn("Kinect for Windows SDK 1.8", adapter.dependency_error())
            self.assertIn(sdk.SDK_DOWNLOAD_URL, adapter.dependency_error())
            info = adapter.public_info({})
            self.assertEqual(info["state"], "driver_missing")
            self.assertFalse(info["verifiedOnHardware"])
            requirements = {item["id"]: item for item in info["requirements"]}
            self.assertEqual(requirements["kinect_sdk_runtime"]["kind"], "driver")
            self.assertFalse(requirements["kinect_sdk_runtime"]["satisfied"])
            with self.assertRaises(sdk.KinectSdkUnavailable) as raised:
                adapter.capture()
            self.assertEqual(raised.exception.code, "sdk_missing")
            # Пробовали загрузить сборку по имени; на диск ничего не писали и не скачивали.
            self.assertEqual(env.calls, ["Microsoft.Kinect"])

    def test_broken_dotnet_runtime_is_not_reported_as_a_missing_module(self):
        with tempfile.TemporaryDirectory() as tempdir:
            assembly = os.path.join(tempdir, "Microsoft.Kinect.dll")
            with open(assembly, "wb") as handle:
                handle.write(b"fake assembly")
            adapter = sdk.KinectSdkV1Adapter(env={sdk.SDK_ASSEMBLY_ENV: assembly})
            failure = ImportError("Failed to create a default .NET runtime")
            with mock.patch.object(sdk, "_module_available", lambda name: True), mock.patch.object(
                sdk.importlib, "import_module", side_effect=failure
            ):
                with self.assertRaises(sdk.KinectSdkUnavailable) as raised:
                    adapter._import_clr()
            self.assertEqual(raised.exception.code, "pythonnet_load_failed")
            self.assertIn("Failed to create a default .NET runtime", str(raised.exception))
            self.assertIn(".NET Framework", raised.exception.hint)

    def test_pythonnet_missing_is_reported_as_a_python_component(self):
        with tempfile.TemporaryDirectory() as tempdir:
            assembly = os.path.join(tempdir, "Microsoft.Kinect.dll")
            with open(assembly, "wb") as handle:
                handle.write(b"fake assembly")
            adapter = sdk.KinectSdkV1Adapter(env={sdk.SDK_ASSEMBLY_ENV: assembly})
            with mock.patch.object(sdk, "_module_available", lambda name: name != "clr"):
                self.assertFalse(adapter.dependencies_available())
                error = adapter.dependency_error()
                requirements = {item["id"]: item for item in adapter.requirements()}
            self.assertIn("pythonnet", error)
            self.assertEqual(requirements["pythonnet"]["kind"], "python")
            self.assertFalse(requirements["pythonnet"]["satisfied"])
            # Драйвер устройства при этом на месте.
            self.assertTrue(requirements["kinect_sdk_runtime"]["satisfied"])

    def test_no_sensor_connected_is_explained(self):
        with AdapterEnvironment(sensors=[]) as env:
            adapter = env.adapter
            self.assertTrue(adapter.dependencies_available())
            with self.assertRaises(sdk.KinectSdkUnavailable) as raised:
                adapter.capture()
            self.assertEqual(raised.exception.code, "no_sensor")
            self.assertIn("не обнаружен", str(raised.exception))

    @REQUIRES_NUMPY
    def test_assembly_reference_fallback_uses_the_sdk_directory(self):
        with AdapterEnvironment(
            depth_frames=[FakeFrame(depth_pixels(1000))],
            add_reference=lambda name: name == "Microsoft.Kinect",
        ) as env:
            frame = env.adapter.capture()
            self.assertEqual(int(frame.depth_mm[0, 0]), 1000)
            # Первая попытка по имени, затем — полный путь к Microsoft.Kinect.dll.
            self.assertEqual(env.calls[0], "Microsoft.Kinect")
            self.assertEqual(env.calls[1], env.assembly)
            self.assertIn(os.path.dirname(env.assembly), sys.path)
        if os.path.dirname(env.assembly) in sys.path:
            sys.path.remove(os.path.dirname(env.assembly))


class ConversionTests(unittest.TestCase):
    def test_adapter_does_not_claim_hardware_verification(self):
        adapter = sdk.KinectSdkV1Adapter(env={})
        self.assertFalse(adapter.verified_on_hardware)
        self.assertTrue(adapter.implemented)
        info = adapter.public_info({})
        self.assertFalse(info["verifiedOnHardware"])
        self.assertIn("не проверен", adapter.limitations)

    def test_sdk_assembly_candidates_cover_program_files_variants(self):
        env = {
            "ProgramW6432": "C:\\Program Files",
            "ProgramFiles": "C:\\Program Files",
            "ProgramFiles(x86)": "C:\\Program Files (x86)",
        }
        candidates = sdk.sdk_assembly_candidates(env)
        self.assertEqual(len(candidates), 2, candidates)
        for candidate in candidates:
            self.assertTrue(candidate.endswith(os.path.join("v1.8", "Assemblies", "Microsoft.Kinect.dll")))

    def test_pixels_to_bytes_accepts_supported_buffers(self):
        self.assertEqual(sdk._pixels_to_bytes(b"abc"), b"abc")
        self.assertEqual(sdk._pixels_to_bytes(bytearray(b"de")), b"de")
        self.assertEqual(sdk._pixels_to_bytes(memoryview(b"fg")), b"fg")
        self.assertEqual(sdk._pixels_to_bytes(None), b"")

    @REQUIRES_NUMPY
    def test_pixels_to_bytes_keeps_depth_values_above_255(self):
        # Глубина 8000 мм сдвинута на 3 бита -> 64000, это Int16 -1536 внутри short[].
        raw = sdk._pixels_to_bytes([64000, 0, 4000], 2)
        self.assertEqual(len(raw), 6)
        self.assertEqual(np.frombuffer(raw, dtype="<u2").tolist(), [64000, 0, 4000])
        negative = sdk._pixels_to_bytes([-1536, 800], 2)
        self.assertEqual(np.frombuffer(negative, dtype="<u2").tolist(), [64000, 800])

    def test_pixels_to_bytes_reads_color_as_single_bytes(self):
        self.assertEqual(sdk._pixels_to_bytes([0, 128, 255], 1), bytes([0, 128, 255]))

    def test_dispose_never_raises(self):
        class Broken:
            def Dispose(self):
                raise RuntimeError("boom")

        sdk._dispose(Broken())
        sdk._dispose(object())

    @REQUIRES_NUMPY
    def test_depth_pixels_use_high_bits(self):
        raw = np.array([8000, 0, 2047 << 3], dtype=np.uint16)
        metres = sdk.depth_pixels_to_metres(raw)
        self.assertAlmostEqual(float(metres[0]), 1.0, places=4)
        self.assertEqual(float(metres[1]), 0.0)
        self.assertAlmostEqual(float(metres[2]), 2.047, places=4)

    @REQUIRES_NUMPY
    def test_short_depth_buffer_is_rejected(self):
        with self.assertRaises(RuntimeError):
            sdk.depth_and_color_to_frame(np.zeros(10, dtype=np.uint16), 640, 480, None, None, None)


if __name__ == "__main__":
    unittest.main()
