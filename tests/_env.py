"""Изоляция тестов от боевых данных и настроек. Импортировать ДО promptpilot.

`promptpilot.config` читает окружение один раз, на импорте, а модули тестов
делят процесс: кто импортировал первым, тот и задал `PP_DATA_DIR` всем
остальным. Без этого файла порядок импорта решал, по какой базе пойдут тесты,
а `setUp` в них делает `DELETE FROM tasks` — то есть неудачный порядок стёр бы
живую очередь.

Окружение — не только переменные шелла: `config._load_dotenv()` подмешивает
`~/.promptpilot/.env` рабочей установки. Поэтому значения, от которых зависит
поведение проверок, задаются здесь явно: иначе тесты зелены или красны в
зависимости от того, что человек настроил у себя (так и вышло с `PP_TG_CHAT_ID`
— настроенный дефолтный чат уронил проверку «серия без адресата молчит»).
`setdefault` оставляет последнее слово за тем, кто запускает прогон руками.
"""

import os
import tempfile
from pathlib import Path

DATA_DIR = tempfile.mkdtemp(prefix="pp-tests-")
os.environ.setdefault("PP_DATA_DIR", DATA_DIR)
# «Дефолтного чата нет» — исходное состояние установки, от него и проверки.
os.environ.setdefault("PP_TG_CHAT_ID", "0")


def assert_isolated():
    """Убедиться, что тесты работают не с ~/.promptpilot. Звать после импорта."""
    from promptpilot import config
    real = (Path.home() / ".promptpilot").resolve()
    if Path(config.DB_DIR).resolve() == real:
        raise RuntimeError(
            "тесты видят боевую базу ~/.promptpilot — запускайте их с PP_DATA_DIR, "
            "указывающим на временный каталог")
