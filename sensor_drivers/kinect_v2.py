"""Kinect One / Kinect for Xbox One (v2) support status."""
from .base import PlannedAdapter


def create_adapter():
    return PlannedAdapter(
        sensor_id="kinect_v2",
        name="Kinect One (Kinect v2)",
        windows="Исторически ориентирован на Windows 8.1/10 x64 с Kinect for Windows SDK 2.0; Windows 11 и текущие драйверы требуют отдельной проверки.",
        driver="Для будущей интеграции потребуется совместимый Kinect for Windows v2 runtime/SDK и USB 3.0 контроллер; одного драйвера недостаточно.",
        limitations="В Depth Studio интеграция не реализована: датчик не будет обнаружен или подключён. Нужны отдельный адаптер, SDK-сборка и аппаратные тесты.",
    )
