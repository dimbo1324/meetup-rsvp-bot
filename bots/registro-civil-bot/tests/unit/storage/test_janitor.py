"""FileJanitor и purge: сроки и количество файлов на диске (SPEC 2.7)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rcbot.config.paths import Paths
from rcbot.infrastructure.files import FileJanitor, data_dirs, purge

NOW = datetime(2026, 11, 12, 15, 0, 0, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.fixture
def janitor(paths: Paths) -> FileJanitor:
    paths.ensure()
    return FileJanitor(paths)


def make(folder: Path, name: str, age: timedelta, size: int = 10) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


# --- prune: возраст ---


def test_prune_удаляет_файлы_строго_старше_срока(janitor: FileJanitor, paths: Paths) -> None:
    make(paths.shots, "old.png", 3 * DAY + timedelta(seconds=1))
    make(paths.shots, "edge.png", 3 * DAY)  # ровно на границе — остаётся
    make(paths.shots, "new.png", DAY)
    assert janitor.prune("shots", 3 * DAY, NOW) == 1
    assert names(paths.shots) == ["edge.png", "new.png"]


def test_prune_отсутствующая_папка_даёт_ноль(janitor: FileJanitor, paths: Paths) -> None:
    paths.discovery.rmdir()
    assert janitor.prune("discovery", 7 * DAY, NOW) == 0


def test_prune_неизвестное_имя_папки_отвергается(janitor: FileJanitor) -> None:
    with pytest.raises(KeyError):
        janitor.prune("data", DAY, NOW)


def test_prune_не_трогает_подкаталоги(janitor: FileJanitor, paths: Paths) -> None:
    sub = paths.quarantine / "вложенная"
    sub.mkdir()
    make(sub, "old.json", 100 * DAY)
    assert janitor.prune("quarantine", DAY, NOW) == 0
    assert (sub / "old.json").exists()


def test_prune_идемпотентен(janitor: FileJanitor, paths: Paths) -> None:
    make(paths.discovery, "d.json", 30 * DAY)
    assert janitor.prune("discovery", 7 * DAY, NOW) == 1
    assert janitor.prune("discovery", 7 * DAY, NOW) == 0


# --- prune: количество (карантин ≤ 20, снимки ≤ 20) ---


def test_prune_по_количеству_оставляет_двадцать_самых_новых(
    janitor: FileJanitor, paths: Paths
) -> None:
    for n in range(25):
        make(paths.quarantine, f"q{n:02d}.json", timedelta(hours=25 - n))  # q24 — самый новый
    assert janitor.prune("quarantine", 7 * DAY, NOW, max_files=20) == 5
    assert names(paths.quarantine) == [f"q{n:02d}.json" for n in range(5, 25)]


def test_prune_ровно_двадцать_файлов_ничего_не_удаляет(janitor: FileJanitor, paths: Paths) -> None:
    for n in range(20):
        make(paths.shots, f"s{n:02d}.png", timedelta(hours=n))
    assert janitor.prune("shots", 3 * DAY, NOW, max_files=20) == 0


def test_prune_двадцать_один_файл_удаляет_самый_старый(janitor: FileJanitor, paths: Paths) -> None:
    for n in range(21):
        make(paths.shots, f"s{n:02d}.png", timedelta(hours=n))  # s20 — самый старый
    assert janitor.prune("shots", 3 * DAY, NOW, max_files=20) == 1
    assert not (paths.shots / "s20.png").exists()


def test_prune_возраст_и_количество_вместе_считаются_без_двойного_удаления(
    janitor: FileJanitor, paths: Paths
) -> None:
    for n in range(4):
        make(paths.shots, f"old{n}.png", 10 * DAY)  # просрочены
    for n in range(22):
        make(paths.shots, f"new{n:02d}.png", timedelta(hours=n))
    removed = janitor.prune("shots", 3 * DAY, NOW, max_files=20)
    assert removed == 4 + 2
    assert len(names(paths.shots)) == 20
    assert not any(n.startswith("old") for n in names(paths.shots))


def test_prune_без_max_files_количество_не_ограничено(janitor: FileJanitor, paths: Paths) -> None:
    for n in range(30):
        make(paths.shots, f"s{n}.png", timedelta(hours=1))
    assert janitor.prune("shots", 3 * DAY, NOW) == 0


def test_prune_max_files_ноль_удаляет_все_непросроченные(
    janitor: FileJanitor, paths: Paths
) -> None:
    make(paths.shots, "a.png", timedelta(hours=1))
    assert janitor.prune("shots", 3 * DAY, NOW, max_files=0) == 1


# --- clear ---


def test_clear_очищает_папку_вместе_с_подкаталогами_и_считает_элементы(
    janitor: FileJanitor, paths: Paths
) -> None:
    make(paths.tmp, "a.tmp", DAY)
    make(paths.tmp, "b.tmp", DAY)
    make(paths.tmp / "sub" / "deeper", "c.tmp", DAY)
    assert janitor.clear("tmp") == 3  # два файла и один подкаталог верхнего уровня
    assert paths.tmp.exists() and list(paths.tmp.iterdir()) == []


def test_clear_отсутствующей_и_пустой_папки(janitor: FileJanitor, paths: Paths) -> None:
    assert janitor.clear("tmp") == 0
    paths.tmp.rmdir()
    assert janitor.clear("tmp") == 0


def test_clear_не_трогает_соседние_папки(janitor: FileJanitor, paths: Paths) -> None:
    keep = make(paths.shots, "s.png", DAY)
    make(paths.tmp, "t", DAY)
    janitor.clear("tmp")
    assert keep.exists()


# --- prune_logs ---


def test_prune_logs_не_трогает_текущий_лог_даже_старый(janitor: FileJanitor, paths: Paths) -> None:
    current = make(paths.logs, "rcbot.log", 100 * DAY)
    assert janitor.prune_logs(14 * DAY, NOW) == 0
    assert current.exists()


def test_prune_logs_удаляет_ротированные_старше_срока_и_оставляет_свежие(
    janitor: FileJanitor, paths: Paths
) -> None:
    make(paths.logs, "rcbot.log", timedelta(0))
    make(paths.logs, "rcbot.log.1", DAY)
    make(paths.logs, "rcbot.log.4", 15 * DAY)
    make(paths.logs, "rcbot.log.5", 30 * DAY)
    assert janitor.prune_logs(14 * DAY, NOW) == 2
    assert names(paths.logs) == ["rcbot.log", "rcbot.log.1"]


def test_prune_logs_граница_срока_строгая(janitor: FileJanitor, paths: Paths) -> None:
    make(paths.logs, "rcbot.log.2", 14 * DAY)
    make(paths.logs, "rcbot.log.3", 14 * DAY + timedelta(seconds=1))
    assert janitor.prune_logs(14 * DAY, NOW) == 1
    assert names(paths.logs) == ["rcbot.log.2"]


def test_prune_logs_чужие_файлы_в_папке_логов_не_трогает(
    janitor: FileJanitor, paths: Paths
) -> None:
    other = make(paths.logs, "notes.txt", 100 * DAY)
    assert janitor.prune_logs(14 * DAY, NOW) == 0
    assert other.exists()


def test_prune_logs_без_папки_даёт_ноль(janitor: FileJanitor, paths: Paths) -> None:
    for item in paths.logs.iterdir():
        item.unlink()
    paths.logs.rmdir()
    assert janitor.prune_logs(14 * DAY, NOW) == 0


# --- folder_size ---


def test_folder_size_суммирует_рекурсивно(janitor: FileJanitor, paths: Paths) -> None:
    base = janitor.folder_size()
    make(paths.data, "a.bin", DAY, size=1000)
    make(paths.shots, "b.png", DAY, size=234)
    make(paths.home / "deep" / "er", "c", DAY, size=66)
    assert janitor.folder_size() == base + 1300


def test_folder_size_пропускает_каталог_git(janitor: FileJanitor, paths: Paths) -> None:
    base = janitor.folder_size()
    make(paths.home / ".git" / "objects", "pack", DAY, size=5000)
    assert janitor.folder_size() == base


def test_folder_size_пропускает_файлы_с_ошибкой_stat(
    janitor: FileJanitor, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    make(paths.data, "ok.bin", DAY, size=400)
    bad = make(paths.data, "bad.bin", DAY, size=9000)
    expected = janitor.folder_size() - 9000
    real_stat = Path.stat

    def stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        if self == bad:
            raise PermissionError(str(self))
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", stat)
    assert janitor.folder_size() == expected


# --- purge ---


def test_purge_удаляет_логи_временное_карантин_снимки_разведку_и_кеш(paths: Paths) -> None:
    paths.ensure()
    paths.cache.mkdir()
    for folder in (
        paths.logs,
        paths.tmp,
        paths.quarantine,
        paths.shots,
        paths.discovery,
        paths.cache,
    ):
        make(folder, "f.dat", DAY)
    removed = purge(paths)
    assert set(removed) == {
        paths.logs,
        paths.tmp,
        paths.quarantine,
        paths.shots,
        paths.discovery,
        paths.cache,
    }
    assert not any(p.exists() for p in removed)


def test_purge_не_трогает_базу_настройки_браузер_и_код(paths: Paths) -> None:
    paths.ensure()
    paths.browsers.mkdir()
    keep = [
        make(paths.data, "rcbot.sqlite3", DAY),
        make(paths.data, "rcbot.sqlite3-wal", DAY),
        make(paths.home, ".env", DAY),
        make(paths.browsers, "chromium/chrome.exe", DAY),
        make(paths.home, "alembic.ini", DAY),
    ]
    purge(paths)
    assert all(p.exists() for p in keep)
    assert paths.data.exists()  # сама папка data остаётся: в ней база


def test_purge_отсутствующие_папки_не_попадают_в_результат_и_вызов_повторяем(paths: Paths) -> None:
    assert purge(paths) == []
    paths.ensure()
    assert len(purge(paths)) == 5  # кеша нет
    assert purge(paths) == []


def test_purge_и_очистка_по_именам_используют_один_список_каталогов(
    janitor: FileJanitor, paths: Paths
) -> None:
    """DUP-12: всё, что `FileJanitor` чистит по имени, удаляет и `purge`."""
    for name, folder in data_dirs(paths).items():
        make(folder, f"{name}.dat", DAY)
        assert janitor.clear(name) == 1
        make(folder, f"{name}.dat", DAY)
    removed = purge(paths)
    assert set(data_dirs(paths).values()) <= set(removed)


def test_ротированные_логи_ищутся_по_имени_файла_лога_из_paths(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAG-13: имя лог-файла берётся из `Paths.log_file`, а не повторяется строкой."""
    monkeypatch.setattr(Paths, "log_file", property(lambda self: self.logs / "другой.log"))
    paths.ensure()
    old = make(paths.logs, "другой.log.1", 30 * DAY)
    current = make(paths.logs, "другой.log", 30 * DAY)
    foreign = make(paths.logs, "rcbot.log.1", 30 * DAY)
    assert FileJanitor(paths).prune_logs(timedelta(days=7), NOW) == 1
    assert not old.exists() and current.exists() and foreign.exists()
