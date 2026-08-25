"""Изоляция тестов от боевых данных. Импортировать ДО promptpilot.

`promptpilot.config` читает окружение один раз, на импорте, а модули тестов
делят процесс: кто импортировал первым, тот и задал `PP_DATA_DIR` всем
остальным. Без этого файла порядок импорта решал, по какой базе пойдут тесты,
а `setUp` в них делает `DELETE FROM tasks` — то есть неудачный порядок стёр бы
живую очередь.
"""

import os
import tempfile
from pathlib import Path

DATA_DIR = tempfile.mkdtemp(prefix="pp-tests-")
os.environ.setdefault("PP_DATA_DIR", DATA_DIR)


def assert_isolated():
    """Убедиться, что тесты работают не с ~/.promptpilot. Звать после импорта."""
    from promptpilot import config
    real = (Path.home() / ".promptpilot").resolve()
    if Path(config.DB_DIR).resolve() == real:
        raise RuntimeError(
            "тесты видят боевую базу ~/.promptpilot — запускайте их с PP_DATA_DIR, "
            "указывающим на временный каталог")
