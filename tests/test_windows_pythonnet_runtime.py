"""Check Kinect-style managed pixel arrays with the real Windows pythonnet.

This is not a Kinect or driver test: the SDK frame is a fake, while the CLR
and Int16[] / Byte[] buffers are real. It is run against the *installed*
application's interpreter by the Windows installer workflow.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest


HAS_WINDOWS_PYTHONNET = sys.platform == "win32" and importlib.util.find_spec("clr") is not None


@unittest.skipUnless(HAS_WINDOWS_PYTHONNET, "нужен Windows с pythonnet")
class PythonnetPixelArrayTests(unittest.TestCase):
    def test_native_pointer_overload_uses_real_clr_intptr_and_marshal(self):
        import clr  # noqa: F401 - starts the real .NET runtime
        from System import Array, Byte, Int16
        from System.Runtime.InteropServices import Marshal
        from sensor_drivers.kinect_sdk_v1 import _SdkSession

        class PointerFrame:
            def __init__(self, managed):
                self.managed = managed
                self.PixelDataLength = len(managed)
                self.calls = []

            def CopyPixelDataTo(self, address, count):
                self.calls.append(count)
                self_type = str(address.GetType().FullName)
                if self_type != "System.IntPtr":
                    raise AssertionError(f"Ожидался System.IntPtr, получен {self_type}")
                # This is what the SDK's pointer overload does internally.
                Marshal.Copy(self.managed, 0, address, count)

        depth = PointerFrame(Array[Int16]([800, -1536, 0]))
        self.assertEqual(_SdkSession._copy_pixels(depth, 2, 3), b"\x20\x03\x00\xfa\x00\x00")
        self.assertEqual(depth.calls, [3])
        color = PointerFrame(Array[Byte]([0, 128, 255, 42]))
        self.assertEqual(_SdkSession._copy_pixels(color, 1, 4), bytes([0, 128, 255, 42]))
        self.assertEqual(color.calls, [4])

    def test_copy_depth_and_color_using_real_managed_arrays(self):
        import clr  # noqa: F401 - starts the real .NET runtime
        from sensor_drivers.kinect_sdk_v1 import _SdkSession

        class FakeSdkFrame:
            def __init__(self, values, expected_type):
                self.values = values
                self.PixelDataLength = len(values)
                self.expected_type = expected_type

            def CopyPixelDataTo(self, target):
                self_type = str(target.GetType().FullName)
                if self_type != self.expected_type:
                    raise AssertionError(f"Ожидался {self.expected_type}, получен {self_type}")
                for index, value in enumerate(self.values):
                    target[index] = value

        # 64000 is a packed depth value represented as signed Int16 (-1536).
        depth = FakeSdkFrame([800, -1536, 0], "System.Int16[]")
        self.assertEqual(_SdkSession._copy_pixels(depth, 2, 3), b"\x20\x03\x00\xfa\x00\x00")
        color = FakeSdkFrame([0, 128, 255, 42], "System.Byte[]")
        self.assertEqual(_SdkSession._copy_pixels(color, 1, 4), bytes([0, 128, 255, 42]))


if __name__ == "__main__":
    unittest.main()
