# -*- coding: utf-8 -*-
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.
#
#  Author: Mauro Soria

from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any, Callable, Generator

from lib.connection.requester import AsyncRequester, BaseRequester, Requester
from lib.connection.response import BaseResponse
from lib.core.data import blacklists, options
from lib.core.dictionary import Dictionary
from lib.core.exceptions import RequestException
from lib.core.logger import logger
from lib.core.scanner import AsyncScanner, BaseScanner, Scanner
from lib.core.settings import (
    DEFAULT_TEST_PREFIXES,
    DEFAULT_TEST_SUFFIXES,
    WILDCARD_TEST_POINT_MARKER,
)
from lib.parse.url import clean_path
from lib.utils.common import get_readable_size, lstrip_once
from lib.utils.crawl import Crawler


class BaseFuzzer:
    def __init__(
        self,
        requester: BaseRequester,
        dictionary: Dictionary,
        *,
        match_callbacks: tuple[Callable[[BaseResponse], Any], ...],
        not_found_callbacks: tuple[Callable[[BaseResponse], Any], ...],
        error_callbacks: tuple[Callable[[RequestException], Any], ...],
    ) -> None:
        self._scanned = set()
        self._requester = requester
        self._dictionary = dictionary
        self._base_path: str = ""
        self.exc: Exception | None = None
        self.match_callbacks = match_callbacks
        self.not_found_callbacks = not_found_callbacks
        self.error_callbacks = error_callbacks

        self.scanners: dict[str, dict[str, Scanner]] = {
            "default": {},
            "prefixes": {},
            "suffixes": {},
        }

    def set_base_path(self, path: str) -> None:
        self._base_path = path

    def get_scanners_for(self, path: str) -> Generator[BaseScanner, None, None]:
        # Clean the path, so can check for extensions/suffixes
        path = clean_path(path)

        for prefix in self.scanners["prefixes"]:
            if path.startswith(prefix):
                yield self.scanners["prefixes"][prefix]

        for suffix in self.scanners["suffixes"]:
            if path.endswith(suffix):
                yield self.scanners["suffixes"][suffix]

        for scanner in self.scanners["default"].values():
            yield scanner

    @staticmethod
    def is_excluded(resp: BaseResponse) -> bool:
        """Validate the response by different filters"""

        if resp.status in options["exclude_status_codes"]:
            return True

        if (
            options["include_status_codes"]
            and resp.status not in options["include_status_codes"]
        ):
            return True

        if (
            resp.status in blacklists
            and any(
                resp.path.endswith(lstrip_once(suffix, "/"))
                for suffix in blacklists.get(resp.status)
            )
        ):
            return True

        if get_readable_size(resp.length).rstrip() in options["exclude_sizes"]:
            return True

        if resp.length < options["minimum_response_size"]:
            return True

        if resp.length > options["maximum_response_size"] > 0:
            return True

        if any(text in resp.content for text in options["exclude_texts"]):
            return True

        if options["exclude_regex"] and re.search(options["exclude_regex"], resp.content):
            return True

        if (
            options["exclude_redirect"]
            and (
                options["exclude_redirect"] in resp.redirect
                or re.search(options["exclude_redirect"], resp.redirect)
            )
        ):
            return True

        return False


class Fuzzer(BaseFuzzer):
    def __init__(
        self,
        requester: Requester,
        dictionary: Dictionary,
        *,
        match_callbacks: tuple[Callable[[BaseResponse], Any], ...],
        not_found_callbacks: tuple[Callable[[BaseResponse], Any], ...],
        error_callbacks: tuple[Callable[[RequestException], Any], ...],
    ) -> None:
        super().__init__(
            requester,
            dictionary,
            match_callbacks=match_callbacks,
            not_found_callbacks=not_found_callbacks,
            error_callbacks=error_callbacks,
        )
        self._threads = []
        self._play_event = threading.Event()
        self._quit_event = threading.Event()
        self._pause_semaphore = threading.Semaphore(0)

    def setup_scanners(self) -> None:
        # Default scanners (wildcard testers)
        self.scanners["default"].update(
            {
                "index": Scanner(self._requester, path=self._base_path),
                "random": Scanner(
                    self._requester, path=self._base_path + WILDCARD_TEST_POINT_MARKER
                ),
            }
        )

        if options["exclude_response"]:
            self.scanners["default"]["custom"] = Scanner(
                self._requester, tested=self.scanners, path=options["exclude_response"]
            )

        for prefix in options["prefixes"] + DEFAULT_TEST_PREFIXES:
            self.scanners["prefixes"][prefix] = Scanner(
                self._requester,
                tested=self.scanners,
                path=f"{self._base_path}{prefix}{WILDCARD_TEST_POINT_MARKER}",
                context=f"/{self._base_path}{prefix}***",
            )

        for suffix in options["suffixes"] + DEFAULT_TEST_SUFFIXES:
            self.scanners["suffixes"][suffix] = Scanner(
                self._requester,
                tested=self.scanners,
                path=f"{self._base_path}{WILDCARD_TEST_POINT_MARKER}{suffix}",
                context=f"/{self._base_path}***{suffix}",
            )

        for extension in options["extensions"]:
            if "." + extension not in self.scanners["suffixes"]:
                self.scanners["suffixes"]["." + extension] = Scanner(
                    self._requester,
                    tested=self.scanners,
                    path=f"{self._base_path}{WILDCARD_TEST_POINT_MARKER}.{extension}",
                    context=f"/{self._base_path}***.{extension}",
                )

    def setup_threads(self) -> None:
        if self._threads:
            self._threads = []

        for _ in range(options["thread_count"]):
            new_thread = threading.Thread(target=self.thread_proc)
            new_thread.daemon = True
            self._threads.append(new_thread)

    def start(self) -> None:
        self.setup_scanners()
        self.setup_threads()
        self.play()

        for thread in self._threads:
            thread.start()

    def is_finished(self) -> bool:
        if self.exc:
            raise self.exc

        for thread in self._threads:
            if thread.is_alive():
                return False

        return True

    def play(self) -> None:
        self._play_event.set()

    def pause(self) -> None:
        self._play_event.clear()
        # Wait for all threads to stop
        for thread in self._threads:
            if thread.is_alive():
                self._pause_semaphore.acquire()

    def quit(self) -> None:
        self._quit_event.set()
        self.play()

    def scan(self, path: str, scanners: Generator[Scanner, None, None]) -> None:
        # Avoid scanned paths from being re-scanned
        if path in self._scanned:
            return
        else:
            self._scanned.add(path)

        response = self._requester.request(path)

        if self.is_excluded(response):
            for callback in self.not_found_callbacks:
                callback(response)
            return

        for tester in scanners:
            # Check if the response is unique, not wildcard
            if not tester.check(path, response):
                for callback in self.not_found_callbacks:
                    callback(response)
                return

        try:
            for callback in self.match_callbacks:
                callback(response)
        except Exception as e:
            self.exc = e

        if options["crawl"]:
            logger.info(f'THREAD-{threading.get_ident()}: crawling "/{path}"')
            for path_ in Crawler.crawl(response):
                if self._dictionary.is_valid(path_):
                    logger.info(
                        f'THREAD-{threading.get_ident()}: found new path "/{path_}" in /{path}'
                    )
                    self.scan(path_, self.get_scanners_for(path_))

    def thread_proc(self) -> None:
        logger.info(f'THREAD-{threading.get_ident()} started"')

        while True:
            try:
                path = next(self._dictionary)
                scanners = self.get_scanners_for(path)
                self.scan(self._base_path + path, scanners)

            except StopIteration:
                break

            except RequestException as e:
                for callback in self.error_callbacks:
                    callback(e)

                continue

            finally:
                time.sleep(options["delay"])

                if not self._play_event.is_set():
                    logger.info(f'THREAD-{threading.get_ident()} paused"')
                    self._pause_semaphore.release()
                    self._play_event.wait()
                    logger.info(f'THREAD-{threading.get_ident()} continued"')

                if self._quit_event.is_set():
                    break


class AsyncFuzzer(BaseFuzzer):
    def __init__(
        self,
        requester: AsyncRequester,
        dictionary: Dictionary,
        *,
        match_callbacks: tuple[Callable[[BaseResponse], Any], ...],
        not_found_callbacks: tuple[Callable[[BaseResponse], Any], ...],
        error_callbacks: tuple[Callable[[RequestException], Any], ...],
    ) -> None:
        super().__init__(
            requester,
            dictionary,
            match_callbacks=match_callbacks,
            not_found_callbacks=not_found_callbacks,
            error_callbacks=error_callbacks,
        )
        self._play_event = asyncio.Event()
        self._background_tasks = set()

    async def setup_scanners(self) -> None:
        # Default scanners (wildcard testers)
        self.scanners["default"].update(
            {
                "index": await AsyncScanner.create(
                    self._requester, path=self._base_path
                ),
                "random": await AsyncScanner.create(
                    self._requester, path=self._base_path + WILDCARD_TEST_POINT_MARKER
                ),
            }
        )

        if options["exclude_response"]:
            self.scanners["default"]["custom"] = await AsyncScanner.create(
                self._requester, tested=self.scanners, path=options["exclude_response"]
            )

        for prefix in options["prefixes"] + DEFAULT_TEST_PREFIXES:
            self.scanners["prefixes"][prefix] = await AsyncScanner.create(
                self._requester,
                tested=self.scanners,
                path=f"{self._base_path}{prefix}{WILDCARD_TEST_POINT_MARKER}",
                context=f"/{self._base_path}{prefix}***",
            )

        for suffix in options["suffixes"] + DEFAULT_TEST_SUFFIXES:
            self.scanners["suffixes"][suffix] = await AsyncScanner.create(
                self._requester,
                tested=self.scanners,
                path=f"{self._base_path}{WILDCARD_TEST_POINT_MARKER}{suffix}",
                context=f"/{self._base_path}***{suffix}",
            )

        for extension in options["extensions"]:
            if "." + extension not in self.scanners["suffixes"]:
                self.scanners["suffixes"]["." + extension] = await AsyncScanner.create(
                    self._requester,
                    tested=self.scanners,
                    path=f"{self._base_path}{WILDCARD_TEST_POINT_MARKER}.{extension}",
                    context=f"/{self._base_path}***.{extension}",
                )

    async def start(self) -> None:
        # In Python 3.9, initialize the Semaphore within the coroutine
        # to avoid binding to a different event loop.
        self.sem = asyncio.Semaphore(options["thread_count"])
        await self.setup_scanners()
        self.play()

        for _ in range(len(self._dictionary)):
            task = asyncio.create_task(self.task_proc())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        await asyncio.gather(*self._background_tasks)

    def is_finished(self) -> bool:
        if self.exc:
            raise self.exc

        return len(self._background_tasks) == 0

    def play(self) -> None:
        self._play_event.set()

    def pause(self) -> None:
        self._play_event.clear()

    def quit(self) -> None:
        for task in self._background_tasks:
            task.cancel()

    async def scan(self, path: str, scanners: Generator) -> None:
        # Avoid scanned paths from being re-scanned
        if path in self._scanned:
            return
        else:
            self._scanned.add(path)

        response = await self._requester.request(path)

        if self.is_excluded(response):
            for callback in self.not_found_callbacks:
                callback(response)
            return

        for tester in scanners:
            # Check if the response is unique, not wildcard
            if not tester.check(path, response):
                for callback in self.not_found_callbacks:
                    callback(response)
                return

        try:
            for callback in self.match_callbacks:
                callback(response)
        except Exception as e:
            self.exc = e

        if options["crawl"]:
            task = asyncio.current_task()
            logger.info(f'{task.get_name()}: crawling "/{path}"')
            for path_ in Crawler.crawl(response):
                if self._dictionary.is_valid(path_):
                    logger.info(
                        f'{task.get_name()}: found new path "/{path_}" in /{path}'
                    )
                    await self.scan(path_, self.get_scanners_for(path_))

    async def task_proc(self) -> None:
        async with self.sem:
            await self._play_event.wait()

            try:
                path = next(self._dictionary)
                scanners = self.get_scanners_for(path)
                await self.scan(self._base_path + path, scanners)
            except StopIteration:
                pass
            except RequestException as e:
                for callback in self.error_callbacks:
                    callback(e)
            finally:
                await asyncio.sleep(options["delay"])
