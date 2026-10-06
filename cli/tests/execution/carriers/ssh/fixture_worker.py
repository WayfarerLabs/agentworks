"""Source-pure supervision of SSH fixture effects and borrowed resource lifetime."""

from __future__ import annotations

from collections.abc import Callable
from threading import Condition, Event, Thread
from types import TracebackType


class FixtureWorker:
    """Admit only after start returns; retain completion before caller cleanup."""

    def __init__(self, work: Callable[[Event], None]) -> None:
        self._work = work
        self._condition = Condition()
        self._admitted = False
        self._cancelled = False
        self._stop = Event()
        self._done = Event()
        self._errors: list[BaseException] = []
        self._worker = Thread(target=self._entry)

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def __enter__(self) -> FixtureWorker:
        # Entry is passive: caller cleanup is established before explicit admission.
        return self

    def start(self) -> None:
        self._worker.start()
        with self._condition:
            self._admitted = True
            self._condition.notify_all()

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        if error is not None:
            self._errors.append(error)
        self._settle()
        self._raise_errors()

    def run(self) -> None:
        """Finish bounded finite work, retaining constructor custody on interruption."""
        with self:
            self.start()
            self._done.wait()

    def _settle(self) -> None:
        while True:
            try:
                with self._condition:
                    self._cancelled = True
                    admitted = self._admitted
                    self._condition.notify_all()
                break
            except BaseException as error:
                self._errors.append(error)
        while True:
            try:
                self._stop.set()
                break
            except BaseException as error:
                self._errors.append(error)
        if admitted:
            while True:
                try:
                    if self._done.wait(0.05):
                        break
                except BaseException as error:
                    self._errors.append(error)

    def _raise_errors(self) -> None:
        if self._errors:
            raise BaseExceptionGroup("Owned fixture worker failed", self._errors)

    def _entry(self) -> None:
        try:
            with self._condition:
                while not self._admitted and not self._cancelled:
                    self._condition.wait()
                if not self._admitted:
                    return
            self._work(self._stop)
        except BaseException as error:
            self._errors.append(error)
        finally:
            self._done.set()


def fixture_call[T](work: Callable[[], T]) -> T:
    """Retain a bounded native command's constructor and result across control."""
    results: list[T] = []
    FixtureWorker(lambda _stop: results.append(work())).run()
    return results[0]
