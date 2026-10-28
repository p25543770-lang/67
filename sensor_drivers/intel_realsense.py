"""Intel RealSense support status."""
from .base import PlannedAdapter


def create_adapter():
    return PlannedAdapter(
        sensor_id="intel_realsense",
        name="Intel RealSense",
        windows="Для будущей интеграции целевая среда — Windows 10/11 x64; ограничения зависят от конкретной модели и USB-контроллера.",
        driver="Потребуется подходящая для модели версия Intel RealSense SDK 2.0 (librealsense) и корректный USB-драйвер; USB 3.x обычно предпочтителен.",
        limitations="В Depth Studio интеграция не реализована: RealSense не обнаруживается и не подключается. Нужны адаптер API, тесты моделей/прошивок и проверка на физическом устройстве.",
    )
