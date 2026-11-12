"""Один экземпляр бота (SPEC 2.6, критерий 5): настоящие процессы, мьютекс и lock-файл."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from rcbot.infrastructure.win.single_instance import AlreadyRunning, SingleInstance

WINDOWS = sys.platform == "win32"

HOLDER = """
import os
import sys
from pathlib import Path
from rcbot.infrastructure.win.single_instance import AlreadyRunning, SingleInstance

lock = SingleInstance(Path(sys.argv[1]))
try:
    lock.acquire()
except AlreadyRunning as error:
    print("ALREADY", error, flush=True)
    sys.exit(3)
print("LOCKED", os.getpid(), flush=True)
sys.stdin.readline()
lock.release()
print("RELEASED", flush=True)
"""


class Proc:
    """Подпроцесс, который занимает замок и держит его, пока ему не пришлют строку."""

    def __init__(self, lock_path: Path) -> None:
        self.popen = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(lock_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

    def first_line(self) -> str:
        assert self.popen.stdout is not None
        return self.popen.stdout.readline().strip()

    def let_go(self) -> str:
        assert self.popen.stdin is not None
        assert self.popen.stdout is not None
        self.popen.stdin.write("go\n")
        self.popen.stdin.flush()
        line = self.popen.stdout.readline().strip()
        self.popen.wait(timeout=60)
        return line

    def kill(self) -> None:
        self.popen.kill()
        self.popen.wait(timeout=60)

    def cleanup(self) -> None:
        if self.popen.poll() is None:
            self.popen.kill()
            self.popen.wait(timeout=60)
        for stream in (self.popen.stdin, self.popen.stdout, self.popen.stderr):
            if stream is not None:
                stream.close()


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "rcbot.lock"


@pytest.fixture
def spawn() -> Iterator[type[Proc]]:
    started: list[Proc] = []

    class Tracked(Proc):
        def __init__(self, lock: Path) -> None:
            super().__init__(lock)
            started.append(self)

    yield Tracked
    for proc in started:
        proc.cleanup()


def acquire_eventually(lock_path: Path, seconds: float = 10.0) -> SingleInstance:
    """ОС освобождает замок убитого процесса не мгновенно: ждём с небольшим запасом."""
    deadline = time.monotonic() + seconds
    while True:
        lock = SingleInstance(lock_path)
        try:
            lock.acquire()
            return lock
        except AlreadyRunning:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


# --- один процесс ---


def test_второй_захват_в_том_же_процессе_отклоняется(lock_path: Path) -> None:
    first = SingleInstance(lock_path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning, match="rcbot уже запущен"):
            SingleInstance(lock_path).acquire()
        # Неудачная попытка не должна освободить чужой замок
        with pytest.raises(AlreadyRunning):
            SingleInstance(lock_path).acquire()
    finally:
        first.release()
    again = SingleInstance(lock_path)
    again.acquire()
    again.release()


def test_замок_создаёт_каталог_и_работает_как_контекстный_менеджер(tmp_path: Path) -> None:
    path = tmp_path / "ещё" / "нет" / "rcbot.lock"
    with SingleInstance(path):
        assert path.exists()
        with pytest.raises(AlreadyRunning):
            SingleInstance(path).acquire()
    with SingleInstance(path):
        pass


def test_освобождение_идемпотентно_и_безопасно_без_захвата(lock_path: Path) -> None:
    lock = SingleInstance(lock_path)
    lock.release()  # ничего не захвачено
    lock.acquire()
    lock.release()
    lock.release()
    lock.acquire()  # один и тот же объект можно использовать повторно
    lock.release()


def test_замок_освобождается_при_исключении_в_теле(lock_path: Path) -> None:
    with pytest.raises(RuntimeError), SingleInstance(lock_path):
        raise RuntimeError("сбой в работе бота")
    lock = SingleInstance(lock_path)
    lock.acquire()
    lock.release()


def test_pid_записывается_в_lock_файл_и_перезаписывается_целиком(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("99999999999999999999 мусор от прошлого запуска", encoding="utf-8")
    lock = SingleInstance(lock_path)
    lock.acquire()
    lock.release()
    # Читаем после release: на Windows занятый диапазон файла другим дескриптором не читается
    assert lock_path.read_text(encoding="ascii") == str(os.getpid())


@pytest.mark.skipif(not WINDOWS, reason="именованный мьютекс есть только в Windows")
def test_имя_мьютекса_зависит_от_пути_каталога(tmp_path: Path) -> None:
    a = SingleInstance(tmp_path / "a" / "rcbot.lock")
    b = SingleInstance(tmp_path / "b" / "rcbot.lock")
    same_as_a = SingleInstance(tmp_path / "a" / ".." / "a" / "rcbot.lock")
    assert a._mutex_name != b._mutex_name
    assert a._mutex_name == same_as_a._mutex_name
    assert a._mutex_name.startswith("Local\\rcbot-")
    assert len(a._mutex_name.removeprefix("Local\\rcbot-")) == 12


@pytest.mark.skipif(not WINDOWS, reason="именованный мьютекс есть только в Windows")
def test_разные_каталоги_бота_не_мешают_друг_другу(tmp_path: Path) -> None:
    """Два независимых каталога (например, тесты и боевой бот) работают одновременно."""
    first = SingleInstance(tmp_path / "a" / "rcbot.lock")
    second = SingleInstance(tmp_path / "b" / "rcbot.lock")
    first.acquire()
    try:
        second.acquire()
        second.release()
    finally:
        first.release()


@pytest.mark.skipif(not WINDOWS, reason="на Windows файл блокируется обязательно")
def test_занятый_lock_файл_без_мьютекса_отклоняет_и_не_оставляет_мьютекс(lock_path: Path) -> None:
    """Страховка lock-файла: мьютекс получен, файл занят чужим → отказ и мьютекс отпущен."""
    import msvcrt

    lock_path.parent.mkdir(parents=True)
    foreign = lock_path.open("a+b")
    foreign.seek(0)
    msvcrt.locking(foreign.fileno(), msvcrt.LK_NBLCK, 1)
    try:
        lock = SingleInstance(lock_path)
        with pytest.raises(AlreadyRunning, match="lock-файл занят"):
            lock.acquire()
    finally:
        foreign.seek(0)
        msvcrt.locking(foreign.fileno(), msvcrt.LK_UNLCK, 1)
        foreign.close()
    # Мьютекс не «протёк»: после снятия чужой блокировки захват проходит
    retry = SingleInstance(lock_path)
    retry.acquire()
    retry.release()


# --- несколько процессов ---


def test_второй_процесс_не_запускается_пока_замок_держит_первый(
    lock_path: Path, spawn: type[Proc]
) -> None:
    holder = spawn(lock_path)
    assert holder.first_line().startswith("LOCKED")
    with pytest.raises(AlreadyRunning):
        SingleInstance(lock_path).acquire()
    assert holder.let_go() == "RELEASED"
    assert holder.popen.returncode == 0


def test_процесс_отказывается_если_замок_держит_этот_процесс(
    lock_path: Path, spawn: type[Proc]
) -> None:
    mine = SingleInstance(lock_path)
    mine.acquire()
    try:
        other = spawn(lock_path)
        first = other.first_line()
        assert first.startswith("ALREADY"), first + (other.popen.stderr.read() or "")  # type: ignore[union-attr]
        other.popen.wait(timeout=60)
        assert other.popen.returncode == 3
    finally:
        mine.release()


def test_после_освобождения_замок_снова_доступен_и_pid_принадлежит_держателю(
    lock_path: Path, spawn: type[Proc]
) -> None:
    holder = spawn(lock_path)
    line = holder.first_line()
    assert line.startswith("LOCKED ")
    holder_pid = int(line.split()[1])  # venv-лаунчер порождает дочерний python: Popen.pid не тот
    assert holder.let_go() == "RELEASED"

    # pid остаётся в файле после освобождения: по нему видно, кто держал замок последним
    assert lock_path.read_text(encoding="ascii") == str(holder_pid)
    lock = SingleInstance(lock_path)
    lock.acquire()
    lock.release()
    assert lock_path.read_text(encoding="ascii") == str(os.getpid())


def test_после_смерти_держателя_замок_доступен_снова(lock_path: Path, spawn: type[Proc]) -> None:
    """Критерий 5: после сбоя или перезагрузки замок не остаётся «залипшим»."""
    holder = spawn(lock_path)
    assert holder.first_line().startswith("LOCKED")
    with pytest.raises(AlreadyRunning):
        SingleInstance(lock_path).acquire()

    holder.kill()

    lock = acquire_eventually(lock_path)
    lock.release()


def test_второй_процесс_берёт_замок_сразу_после_первого(lock_path: Path, spawn: type[Proc]) -> None:
    first = spawn(lock_path)
    assert first.first_line().startswith("LOCKED")
    first.let_go()
    second = spawn(lock_path)
    assert second.first_line().startswith("LOCKED")
    assert second.let_go() == "RELEASED"
