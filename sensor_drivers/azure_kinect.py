"""Azure Kinect DK support status."""
from .base import PlannedAdapter


def create_adapter():
    return PlannedAdapter(
        sensor_id="azure_kinect",
        name="Azure Kinect DK",
        windows="Для будущей интеграции ориентируйтесь на Windows 10 x64 и конкретную версию Azure Kinect Sensor SDK; проверьте актуальную доступность SDK и прошивки.",
        driver="Нужны Azure Kinect Sensor SDK, совместимая прошивка и USB 3.0 соединение; установка SDK сама по себе не добавляет интеграцию в приложение.",
        limitations="В Depth Studio интеграция не реализована. Семейство Azure Kinect снято с рынка, доступность SDK/поддержки и совместимость конкретной системы требуют проверки.",
    )
