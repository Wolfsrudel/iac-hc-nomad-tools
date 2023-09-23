#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import base64
import dataclasses
import datetime
import itertools
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from http import client as http_client
from typing import Any, Callable, Dict, Iterable, List, Optional, Pattern, Set, Tuple

import click
import requests

from . import nomadlib
from .common import (
    _complete_set_namespace,
    common_options,
    complete_job,
    completor,
    composed,
    mynomad,
    namespace_option,
    nomad_find_namespace,
)
from .nomad_smart_start_job import nomad_smart_start_job

log = logging.getLogger(__name__)

###############################################################################

args = argparse.Namespace()


@dataclasses.dataclass
class Argsstream:
    out: bool
    err: bool
    alloc: bool


args_stream: Argsstream

args_lines_start_ns: int = 0


def _init_colors() -> Dict[str, str]:
    tputdict = {
        "bold": "bold",
        "black": "setaf 0",
        "red": "setaf 1",
        "green": "setaf 2",
        "orange": "setaf 3",
        "blue": "setaf 4",
        "magenta": "setaf 5",
        "cyan": "setaf 6",
        "white": "setaf 7",
        "reset": "sgr0",
    }
    empty = {k: "" for k in tputdict.keys()}
    if not sys.stdout.isatty() or not sys.stderr.isatty():
        return empty
    tputscript = "\n".join(tputdict.values()).replace("\n", "\nlongname\nlongname\n")
    try:
        longname = subprocess.run(
            f"tput longname".split(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout
        ret = subprocess.run(
            "tput -S".split(),
            input=tputscript,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return empty
    retarr = ret.split(f"{longname}{longname}")
    if len(tputdict.keys()) != len(retarr):
        return empty
    return {k: v for k, v in zip(tputdict.keys(), retarr)}


COLORS = _init_colors()

###############################################################################


def ns2s(ns: int):
    return ns / 1000000000


def ns2dt(ns: int):
    return datetime.datetime.fromtimestamp(ns // 1000000000).astimezone()


###############################################################################


@dataclasses.dataclass(frozen=True)
class LogFormat:
    alloc: str
    stderr: str
    stdout: str
    module: str

    @classmethod
    def mk(cls, prefix: str, log_timestamp: bool = False):
        now = "%(asctime)s:" if log_timestamp else ""
        alloc_now = "" if log_timestamp else " %(asctime)s"
        lf = cls(
            f"{now}{prefix}A{alloc_now} %(message)s",
            f"{now}{prefix}E %(message)s",
            f"{now}{prefix}O %(message)s",
            f"{now}%(module)s:%(lineno)03d: %(levelname)s %(message)s",
        )
        lf = cls(
            f"%(cyan)s{lf.alloc}%(reset)s",
            f"%(orange)s{lf.stderr}%(reset)s",
            lf.stdout,
            f"%(blue)s{lf.module}%(reset)s",
        )
        return lf

    def astuple(self):
        return dataclasses.astuple(self)


log_format = LogFormat.mk("%(allocid).6s:%(group)s:%(task)s:")


def click_log_options():
    """All logging options"""
    return composed(
        click.option(
            "-T",
            "--log-timestamp",
            is_flag=True,
            help="Additionally add timestamp of the logs from the task. The timestamp is when the log was received. Nomad does not store timestamp of logs sadly.",
        ),
        click.option(
            "--log-timestamp-format",
            default="%Y-%m-%dT%H:%M:%S%z",
            show_default=True,
        ),
        click.option("--log-format-alloc", default=log_format.alloc, show_default=True),
        click.option(
            "--log-format-stderr", default=log_format.stderr, show_default=True
        ),
        click.option(
            "--log-format-stdout", default=log_format.stdout, show_default=True
        ),
        click.option(
            "--log-long-alloc", is_flag=True, help="Log full length allocation id"
        ),
        click.option(
            "-G",
            "--log-no-group",
            is_flag=True,
            help="Do not log group",
        ),
        click.option(
            "--log-no-task",
            is_flag=True,
            help="Do not log task",
        ),
        click.option(
            "-1",
            "--log-only-task",
            is_flag=True,
            help="Prefix the lines only with task name.",
        ),
        click.option(
            "-0",
            "--log-none",
            is_flag=True,
            help="Log only stream prefix",
        ),
    )


def log_format_choose():
    global log_format
    log_format = LogFormat(
        args.log_format_alloc,
        args.log_format_stderr,
        args.log_format_stdout,
        log_format.module,
    )
    alloc = "%(allocid)s" if args.log_long_alloc else "%(allocid).6s"
    group = "" if args.log_no_group else "%(group)s:"
    task = "" if args.log_no_task else "%(task)s:"
    log_format = LogFormat.mk(f"{alloc}:{group}{task}", args.log_timestamp)
    if args.log_only_task:
        log_format = LogFormat.mk("%(task)s:", args.log_timestamp)
    if args.log_none:
        log_format = LogFormat.mk("", args.log_timestamp)


@dataclasses.dataclass(frozen=True)
class TaskKey:
    """Represent data to unique identify a task"""

    allocid: str
    nodename: str
    group: str
    task: str

    def __str__(self):
        return f"{self.allocid:.6}:{self.group}:{self.task}"

    def _params(self, params: Dict[str, Any] = {}) -> Dict[str, Any]:
        return {
            **params,
            **dataclasses.asdict(self),
            **COLORS,
            "asctime": params["now"].strftime(args.log_timestamp_format),
        }

    def _log(self, fmt, **kvargs: Any):
        print(fmt % self._params(kvargs), flush=True)

    def log_alloc(self, now: datetime.datetime, message: str):
        self._log(log_format.alloc, now=now, message=message)

    def log_task(self, stderr: bool, message: str):
        self._log(
            log_format.stderr if stderr else log_format.stdout,
            message=message,
            now=datetime.datetime.now().astimezone(),
        )


###############################################################################


class Logger(threading.Thread):
    """Represents a single logging stream from Nomad. Such stream is created separately for stdout and stderr."""

    def __init__(self, tk: TaskKey, stderr: bool):
        super().__init__(name=f"{tk}{1 + int(stderr)}")
        self.tk = tk
        self.stderr: bool = stderr
        self.exitevent = threading.Event()
        self.ignoredlines: List[str] = []
        self.first_line = True
        # Ignore input lines if printing only trailing lines.
        self.ignoretime_ns = (
            0 if args.lines < 0 else args_lines_start_ns + int(args.lines_timeout * 1e9)
        )
        # If ignore time is in the past, it is no longer relevant anyway.
        if self.ignoretime_ns and self.ignoretime_ns < time.time_ns():
            self.ignoretime_ns = 0

    @staticmethod
    def _read_json_stream(stream: requests.Response):
        txt: str = ""
        for data in stream.iter_content(decode_unicode=True):
            for c in data:
                txt += c
                # Nomad happens to be consistent, the jsons are flat.
                if c == "}":
                    try:
                        ret = json.loads(txt)
                        # log.debug(f"RECV: {ret}")
                        yield ret
                    except json.JSONDecodeError as e:
                        log.warn(f"error decoding json: {txt} {e}")
                    txt = ""

    def _taskout(self, lines: List[str]):
        """Output the lines"""
        # If ignoring and this is first received line or the ignoring time is still happenning.
        if self.ignoretime_ns and (
            self.first_line or time.time_ns() < self.ignoretime_ns
        ):
            # Accumulate args.lines into ignoredlines array.
            self.first_line = False
            self.ignoredlines = lines
            self.ignoredlines = self.ignoredlines[: args.lines]
        else:
            if self.ignoretime_ns:
                # If not ignoring lines, flush the accumulated lines.
                lines = self.ignoredlines + lines
                self.ignoredlines.clear()
                # Disable further accumulation of ignored lines.
                self.ignoretime_ns = 0
            # Print the log lines.
            for line in lines:
                line = line.rstrip()
                self.tk.log_task(self.stderr, line)

    def run(self):
        """Listen to Nomad log stream and print the logs"""
        with mynomad.stream(
            f"client/fs/logs/{self.tk.allocid}",
            params={
                "task": self.tk.task,
                "type": "stderr" if self.stderr else "stdout",
                "follow": True,
                "origin": "end" if self.ignoretime_ns else "start",
                "offset": 50000 if self.ignoretime_ns else 0,
            },
        ) as stream:
            for event in self._read_json_stream(stream):
                if event:
                    line64: Optional[str] = event.get("Data")
                    if line64:
                        lines = base64.b64decode(line64.encode()).decode().splitlines()
                        self._taskout(lines)
                else:
                    # Nomad json stream periodically sends empty {}.
                    # No idea why, but I can implement timeout.
                    self._taskout([])
                    if self.exitevent.is_set():
                        break

    def stop(self):
        self.exitevent.set()


class TaskHandler:
    """A handler for one task. Creates loggers, writes out task events, handle exit conditions"""

    def __init__(self):
        # Array of loggers that log allocation logs.
        self.loggers: List[Logger] = []
        # A set of message timestamp to know what has been printed.
        self.messages: Set[int] = set()
        self.exitcode: Optional[int] = None

    @staticmethod
    def _create_loggers(tk: TaskKey):
        ths: List[Logger] = []
        if args_stream.out:
            ths.append(Logger(tk, False))
        if args_stream.err:
            ths.append(Logger(tk, True))
        assert len(ths)
        for th in ths:
            th.start()
        return ths

    def notify(self, tk: TaskKey, task: nomadlib.AllocTaskStates):
        """Receive notification that a task state has changed"""
        events = task.Events
        if args_stream.alloc:
            for e in events:
                msg = e.DisplayMessage
                msgtime_ns = e.Time
                # Ignore message before ignore times.
                if (
                    msgtime_ns
                    and msg
                    and msgtime_ns not in self.messages
                    and (
                        not args_lines_start_ns
                        or msgtime_ns >= args_lines_start_ns
                        or len(self.messages) < args.lines
                    )
                ):
                    self.messages.add(msgtime_ns)
                    tk.log_alloc(ns2dt(msgtime_ns), msg)
        if (
            not self.loggers
            and task["State"] in ["running", "dead"]
            and task.find_event("Started")
        ):
            self.loggers += self._create_loggers(tk)
            if task.State == "dead":
                # If the task is already finished, give myself max 3 seconds to query all the logs.
                # This is to reduce the number of connections.
                threading.Timer(3, self.stop)
        if self.exitcode is None and task["State"] == "dead":
            # Assigns None if Terminated event not found
            self.exitcode = (task.find_event("Terminated") or {}).get("ExitCode")
            self.stop()

    def stop(self):
        for l in self.loggers:
            l.stop()


class AllocWorker:
    """Represents a worker that prints out and manages state related to one allocation"""

    def __init__(self):
        self.taskhandlers: Dict[TaskKey, TaskHandler] = {}

    def notify(self, alloc: nomadlib.Alloc):
        """Update the state with alloc"""
        for taskname, task in alloc.taskstates().items():
            if args.task and not args.task.search(taskname):
                continue
            tk = TaskKey(alloc.ID, alloc.NodeName, alloc.TaskGroup, taskname)
            self.taskhandlers.setdefault(tk, TaskHandler()).notify(tk, task)


class ExitCode:
    success = 0
    exception = 1
    unfinished = 2
    any_failed = 124
    all_failed = 125
    any_unfinished = 126
    no_allocations = 127


class AllocWorkers(Dict[str, AllocWorker]):
    """An containers for storing a map of allocation workers"""

    def notify(self, alloc: nomadlib.Alloc):
        self.setdefault(alloc.ID, AllocWorker()).notify(alloc)

    def stop(self):
        for w in self.values():
            for th in w.taskhandlers.values():
                th.stop()

    def join(self):
        threads: List[Tuple[str, threading.Thread]] = [
            (f"{tk.task}[{i}]", logger)
            for w in self.values()
            for tk, th in w.taskhandlers.items()
            for i, logger in enumerate(th.loggers)
        ]
        thcnt = sum(len(w.taskhandlers) for w in self.values())
        log.debug(
            f"Joining {len(self)} allocations with {thcnt} taskhandlers and {len(threads)} loggers"
        )
        timeend = time.time() + args.shutdown_timeout
        for desc, thread in threads:
            timeout = timeend - time.time()
            if timeout > 0:
                log.debug(f"joining worker {desc} timeout={timeout}")
                thread.join(timeout=timeout)
            else:
                log.debug("timeout passed for joining workers")
                break

    def exitcode(self) -> int:
        exitcodes: List[int] = [
            # If thread did not return, exit with -1.
            -1 if th.exitcode is None else th.exitcode
            for w in self.values()
            for th in w.taskhandlers.values()
        ]
        if len(exitcodes) == 0:
            return ExitCode.no_allocations
        any_unfinished = any(v == -1 for v in exitcodes)
        if any_unfinished:
            return ExitCode.any_unfinished
        only_one_task = len(exitcodes) == 1
        if only_one_task:
            return exitcodes[0]
        all_failed = all(v != 0 for v in exitcodes)
        if all_failed:
            return ExitCode.all_failed
        any_failed = any(v != 0 for v in exitcodes)
        if any_failed:
            return ExitCode.any_failed
        return ExitCode.success


###############################################################################


Event = nomadlib.Event
EventTopic = nomadlib.EventTopic
EventType = nomadlib.EventType


class DbThread(threading.Thread):
    def __init__(self, topics: List[str], init_cb: Callable[[], Iterable[Event]]):
        super().__init__(name="db", daemon=True)
        self.topics: List[str] = topics
        self.init_cb: Callable[[], Iterable[Event]] = init_cb
        self.queue: queue.Queue[Optional[Event]] = queue.Queue()
        self.stopevent = threading.Event()
        assert self.topics
        assert not any(not x for x in topics)

    def _run_stream(self):
        log.debug(f"Starting listen Nomad stream with {' '.join(self.topics)}")
        with mynomad.stream(
            "event/stream",
            params={"topic": self.topics},
        ) as stream:
            for line in stream.iter_lines():
                data = json.loads(line)
                events: List[dict] = data.get("Events", [])
                for event in events:
                    e = Event(
                        EventTopic[event["Topic"]],
                        EventType[event["Type"]],
                        event["Payload"][event["Topic"]],
                        stream=True,
                    )
                    # log.debug(f"RECV EVENT: {e}")
                    self.queue.put(e)
                if self.stopevent.is_set():
                    break

    def _poll(self):
        while not self.stopevent.is_set():
            for e in self.init_cb():
                self.queue.put(e)
            self.stopevent.wait(1)

    def run(self):
        try:
            try:
                if args.polling:
                    self._poll()
                else:
                    try:
                        self._run_stream()
                    except nomadlib.PermissionDenied as e:
                        log.info(
                            f"Falling to polling method because stream API returned permission denied: {e}"
                        )
                        self._poll()
            finally:
                log.debug("Nomad database thread exiting")
                self.queue.put(None)
        except requests.HTTPError as e:
            log.exception("http request failed")
            exit(ExitCode.exception)


class Db:
    """Represents relevant state cache from Nomad database"""

    def __init__(
        self,
        topics: List[str],
        filter_event_cb: Callable[[Event], bool],
        init_cb: Callable[[], Iterable[Event]],
    ):
        """
        :param topics: Passed to nomad evnet stream as topics.
        :param filter_event_cb: Filter the events from Nomad.
        :param init_cb: Get initial data from Nomad.
        """
        self.thread = DbThread(topics, init_cb)
        self.filter_event_cb: Callable[[Event], bool] = filter_event_cb
        self.initialized = threading.Event()
        self.job: Optional[nomadlib.Job] = None
        self.allocations: Dict[str, nomadlib.Alloc] = {}
        self.evaluations: Dict[str, nomadlib.Eval] = {}

    def start(self):
        assert (
            mynomad.namespace
        ), "Nomad namespace has to be set before starting to listen"
        self.thread.start()

    def handle_event(self, e: Event) -> bool:
        if self._filter_old_event(e):
            if self.filter_event_cb(e):
                # log.debug(f"EVENT: {e}")
                self._add_event_to_db(e)
                return True
            else:
                # log.debug(f"USER FILTERED: {e}")
                pass
        else:
            # log.debug(f"OLD EVENT: {e}")
            pass
        return False

    def _add_event_to_db(self, e: Event):
        if e.topic == EventTopic.Job:
            if e.type == EventType.JobDeregistered:
                self.job = None
            else:
                self.job = nomadlib.Job(e.data)
        elif e.topic == EventTopic.Evaluation:
            if e.type == EventType.JobDeregistered:
                self.job = None
            self.evaluations[e.data["ID"]] = nomadlib.Eval(e.data)
        elif e.topic == EventTopic.Allocation:
            self.allocations[e.data["ID"]] = nomadlib.Alloc(e.data)

    @staticmethod
    def apply_filters(
        e: Event,
        job_filter: Callable[[nomadlib.Job], bool],
        eval_filter: Callable[[nomadlib.Eval], bool],
        alloc_filter: Callable[[nomadlib.Alloc], bool],
    ) -> bool:
        return e.apply(job_filter, eval_filter, alloc_filter)

    def _filter_old_event(self, e: Event):
        job_filter: Callable[[nomadlib.Job], bool] = (
            lambda job: self.job is None or job.ModifyIndex > self.job.ModifyIndex
        )
        eval_filter: Callable[[nomadlib.Eval], bool] = (
            lambda eval: eval.ID not in self.evaluations
            or eval.ModifyIndex > self.evaluations[eval.ID].ModifyIndex
        )
        alloc_filter: Callable[[nomadlib.Alloc], bool] = (
            lambda alloc: alloc.ID not in self.allocations
            or alloc.ModifyIndex > self.allocations[alloc.ID].ModifyIndex
        )
        return e.data["Namespace"] == mynomad.namespace and self.apply_filters(
            e, job_filter, eval_filter, alloc_filter
        )

    def stop(self):
        log.debug("Stopping listen Nomad stream")
        self.initialized.set()
        self.thread.stopevent.set()

    def join(self):
        self.thread.join()

    def events(self) -> Iterable[Event]:
        assert self.thread.is_alive(), "Thread not alive"
        if not self.initialized.is_set():
            if self.thread.init_cb:
                for event in self.thread.init_cb():
                    if self.handle_event(event):
                        yield event
            self.initialized.set()
        log.debug("Starting getting events from thread")
        while not self.thread.queue.empty() or (
            self.thread.is_alive() and not self.thread.stopevent.is_set()
        ):
            event = self.thread.queue.get()
            if event is None:
                break
            if self.handle_event(event):
                yield event


###############################################################################


def nomad_watch_eval(evalid: str):
    assert isinstance(evalid, str), f"not a string: {evalid}"
    db = Db(
        topics=[
            f"Evaluation:{evalid}",
        ],
        filter_event_cb=lambda e: e.topic == EventTopic.Evaluation
        and e.data["ID"] == evalid,
        init_cb=lambda: [
            Event(
                EventTopic.Evaluation,
                EventType.EvaluationUpdated,
                mynomad.get(f"evaluation/{evalid}"),
            )
        ],
    )
    db.start()
    log.info(f"Waiting for evaluation {evalid}")
    eval_ = None
    for event in db.events():
        eval_ = event.data
        if eval_["Status"] != "pending":
            break
    db.stop()
    assert eval_ is not None
    assert (
        eval_["Status"] == "complete"
    ), f"Evaluation {evalid} did not complete: {eval_.get('StatusDescription')}"
    FailedTGAllocs = eval_.get("FailedTGAllocs")
    if FailedTGAllocs:
        groups = " ".join(list(FailedTGAllocs.keys()))
        log.info(f"Evaluation {evalid} failed to place groups: {groups}")


def nomad_start_job_and_wait(input: str) -> nomadlib.Job:
    assert isinstance(input, str)
    evalid = nomad_smart_start_job(input, args.json)
    eval_: dict = mynomad.get(f"evaluation/{evalid}")
    mynomad.namespace = eval_["Namespace"]
    nomad_watch_eval(evalid)
    jobid = eval_["JobID"]
    return nomadlib.Job(mynomad.get(f"job/{jobid}"))


def nomad_find_job(jobid: str) -> nomadlib.Job:
    jobid = mynomad.find_job(jobid)
    return nomadlib.Job(mynomad.find_last_not_stopped_job(jobid))


###############################################################################


class NomadJobWatcher(ABC, threading.Thread):
    """Watches over a job. Schedules watches over allocations. Spawns loggers."""

    def __init__(self, job: nomadlib.Job):
        super().__init__(name=f"NomadJobWatcher({job['ID']})")
        self.job = job
        self.allocworkers = AllocWorkers()
        self.db = Db(
            topics=[
                f"Job:{self.job.ID}",
                f"Evaluation:{self.job.ID}",
                f"Allocation:{self.job.ID}",
            ],
            filter_event_cb=self.db_filter_event_job,
            init_cb=self.db_init_cb,
        )
        # I am using threading.Event because you can't handle KeyboardInterrupt while Thread.join().
        self.done = threading.Event()
        # If set to True, menas that JobNotFound is not an error - the job was removed.
        self.purged = threading.Event()

    def db_init_cb(self):
        """Db initialization callback"""

        try:
            job: dict = mynomad.get(f"job/{self.job.ID}")
            evaluations: List[dict] = mynomad.get(f"job/{self.job.ID}/evaluations")
            allocations: List[dict] = mynomad.get(f"job/{self.job.ID}/allocations")
        except nomadlib.JobNotFound:
            if args.purge:
                # If args.purge, the job could have been purged and it could be fine.
                return
            raise
        if not allocations:
            log.info(f"Job {self.job.description()} has no allocations")
        for e in evaluations:
            yield Event(EventTopic.Evaluation, EventType.EvaluationUpdated, e)
        for a in allocations:
            yield Event(EventTopic.Allocation, EventType.AllocationUpdated, a)
        yield Event(EventTopic.Job, EventType.JobRegistered, job)

    def db_filter_event_jobid(self, e: Event):
        return Db.apply_filters(
            e,
            lambda job: job.ID == self.job.ID,
            lambda eval: eval.JobID == self.job.ID,
            lambda alloc: alloc["JobID"] == self.job.ID,
        )

    def db_filter_event_job(self, e: Event):
        job_filter: Callable[[nomadlib.Job], bool] = lambda _: True
        eval_filter: Callable[[nomadlib.Eval], bool] = lambda eval: (
            # Either all, or the JobModifyIndex has to be greater.
            args.all
            or (
                "JobModifyIndex" in eval
                and eval.JobModifyIndex >= self.job.JobModifyIndex
            )
        )
        alloc_filter: Callable[[nomadlib.Alloc], bool] = lambda alloc: (
            args.all
            # If allocation has JobVersion, then it has to match the version in the job.
            or ("JobVersion" in alloc and alloc.JobVersion >= self.job.Version)
            or (
                # If the allocation has no JobVersion, find the maching evaluation.
                # The JobModifyIndex from the evalution has to match.
                alloc.EvalID in self.db.evaluations
                and self.db.evaluations[alloc.EvalID].JobModifyIndex
                >= self.job.JobModifyIndex
            )
        )
        return self.db_filter_event_jobid(e) and Db.apply_filters(
            e, job_filter, eval_filter, alloc_filter
        )

    @property
    def allocs(self):
        return list(self.db.allocations.values())

    @abstractmethod
    def until_cb(self) -> bool:
        """Overloaded callback to call to determine if we should finish watching the job"""
        raise NotImplementedError()

    def _watch_job(self):
        log.info(f"Watching job {self.job.description()}")
        no_follow_timeend = time.time() + args.shutdown_timeout
        for event in self.db.events():
            if event.topic == EventTopic.Allocation:
                alloc = nomadlib.Alloc(event.data)
                # for alloc in self.db.allocations.values():
                self.allocworkers.notify(alloc)
            if (not args.all and self.until_cb()) or (
                args.no_follow and time.time() > no_follow_timeend
            ):
                break

    def run(self):
        self.db.start()
        try:
            self._watch_job()
        finally:
            self.close()
            self.done.set()

    def close(self):
        log.debug("close()")
        self.db.stop()
        self.allocworkers.stop()
        # Logs stream outputs empty {} which allows to handle timeouts.
        self.allocworkers.join()
        # Not joining self.db - neither requests nor stream API allow for timeouts.
        # self.db.join()
        mynomad.session.close()

    def exitcode(self) -> int:
        assert self.done.is_set(), f"Watcher not finished"
        if args.no_preserve_status:
            # If the job has been purged when --purge,
            # Or the job has finished.
            if (self.db.job is None and self.purged.is_set()) or (
                self.db.job is not None and self.db.job.Status != "dead"
            ):
                return ExitCode.success
            else:
                return ExitCode.unfinished
        return self.allocworkers.exitcode()

    def join(self):
        self.done.wait()

    def stop_job(self, purge: bool):
        self.db.initialized.wait()
        if purge:
            self.purged.set()
        mynomad.stop_job(self.job.ID, purge)

    def run_till_end(self):
        self.run()
        exit(self.exitcode())


class NomadJobWatcherUntilFinished(NomadJobWatcher):
    """Watcher a job until the job is dead"""

    # The job was found at least once.
    foundjob: bool = False

    def until_cb(self) -> bool:
        if self.db.job is None:
            self.foundjob = True
        if not self.foundjob:
            return False
        if self.purged.is_set():
            # If the job was purged, then we wait until the job is completely purged.
            if self.db.job is None:
                log.info(f"Job {self.job.description()} purged. Exiting.")
                return True
            return False
        if self.db.job:
            jobjson = self.db.job
            if jobjson is not None:
                if jobjson.Version != self.job.Version:
                    log.info(
                        f"New version of job {self.job.description()} was posted. Exiting."
                    )
                    return True
                if jobjson.Status == "dead":
                    log.info(f"Job {self.job.description()} is dead. Exiting.")
                    return True
        return False


class NomadJobWatcherUntilStarted(NomadJobWatcher):
    """Watches a job until the job is started"""

    # The job had allocations.
    hadalloc: bool = False
    # The job finished because all allocations started, not because they failed.
    started: bool = False

    def until_cb(self) -> bool:
        runningallocsids = list(self.allocworkers.keys())
        tasks = [
            task
            for allocid in runningallocsids
            for task in self.db.allocations[allocid].taskstates().values()
        ]
        alltasksstarted = all(task.find_event("Started") for task in tasks)
        if alltasksstarted and len(self.allocworkers) and len(tasks):
            allocsstr = " ".join(runningallocsids)
            log.info(
                f"Allocations {allocsstr} started {len(runningallocsids)} allocations with {len(tasks)} tasks"
            )
            self.started = True
            return True
        if runningallocsids:
            self.hadalloc = True
        if self.hadalloc and self.db.job and self.db.job.Status == "dead":
            log.info(f"Job {self.db.job.description()} is ")
            return True
        return False

    def exitcode(self) -> int:
        assert self.done.is_set(), f"Watcher not finished"
        return 2 if not self.started else 0


###############################################################################


class JobPath:
    jobname: str
    group: Optional[Pattern]
    task: Optional[Pattern]

    def __init__(self, param: str):
        a = param.split("@")
        self.jobname = a[0]
        if len(a) == 2:
            self.task = re.compile(a[1])
        elif len(a) == 3:
            self.group = re.compile(a[1])
            self.task = re.compile(a[2])
        assert (
            1 <= len(a) <= 3
        ), f"Invalid job/job@task/job@group@task specification: {param}"

    @staticmethod
    def complete(ctx: click.Context, _: str, incomplete: str):
        _complete_set_namespace(ctx)
        try:
            jobs = mynomad.get("jobs")
        except requests.HTTPError:
            return []
        jobsids = [x["ID"] for x in jobs]
        arg = incomplete.split("@")
        complete = []
        if len(arg) == 1:
            complete = [x for x in jobsids]
            complete += [f"{x}@" for x in jobsids]
        elif len(arg) == 2 or len(arg) == 3:
            jobid = arg[0]
            jobsids = [x for x in jobsids if x == arg[0]]
            if len(jobsids) != 1:
                return []
            mynomad.namespace = next(x for x in jobs if x["ID"] == arg[0])["Namespace"]
            try:
                job = nomadlib.Job(mynomad.get(f"job/{jobid}"))
            except requests.HTTPError:
                return []
            if len(arg) == 2:
                tasks = [t.Name for tg in job.TaskGroups for t in tg.Tasks]
                groups = [tg.Name for tg in job.TaskGroups]
                complete = itertools.chain(tasks, groups)
            elif len(arg) == 3:
                complete = [f"{tg.Name}@{t}" for tg in job.TaskGroups for t in tg.Tasks]
            complete = [f"{arg[0]}@{x}" for x in complete]
        else:
            return []
        return [x for x in complete if x.startswith(incomplete)]


@click.group(
    help=f"""
    Run a Nomad job in Nomad and then print logs to stdout and wait for
    the job to be completely finish. Made for running batch commands and monitoring
    them until they are done.

    \b
    If the option --no-preserve-exit is given, then exit with the following status:
        0    if operation was successfull - the job was run or was purged on --purge
    Ohterwise, when mode is alloc, run, job, stop or stopped, exit with the following status:
        ?    when the job has one task, with that task exit status
        0    if all tasks of the job exited with 0 exit status
        {ExitCode.any_failed}  if any of the job tasks have failed
        {ExitCode.all_failed}  if all job tasks have failed
        {ExitCode.any_unfinished}  if any tasks are still running
        {ExitCode.no_allocations}  if job has no started tasks
    When the mode is start or started, then exit with the following status:
        0    all tasks of the job have started running
    In either case, exit with the following status:
        1    if some error occured, like python exception

    \b
    Examples:
        nomad-watch --namespace default run ./some-job.nomad.hcl
        nomad-watch job some-job
        nomad-watch alloc af94b2
        nomad-watch -N services --task redis -1f job redis
    """,
    epilog="""
    Written by Kamil Cukrowski 2023. Licensed under GNU GPL version 3 or later.
    """,
)
@namespace_option()
@click.option(
    "-a",
    "--all",
    is_flag=True,
    help="""
        Do not exit after the current job version is finished.
        Instead, watch endlessly for any existing and new allocations of a job.
        """,
)
@click.option(
    "-s",
    "--stream",
    type=click.Choice("all alloc a stdout out o 1 stderr err e 2".split()),
    default=["all"],
    multiple=True,
    help="Print only messages from allocation and stdout or stderr of the task. This option is cummulative.",
)
@click.option("-v", "--verbose", count=True, help="Be verbose")
@click.option(
    "--json",
    is_flag=True,
    help="job input is in json form, passed to nomad command with -json",
)
@click.option(
    "--stop",
    is_flag=True,
    help="Only relevant in stop mode. Stop the job before exiting.",
)
@click.option(
    "--purge",
    is_flag=True,
    help="Only relevant in run and stop modes. Purge the job.",
)
@click.option(
    "-n",
    "--lines",
    default=-1,
    show_default=True,
    type=int,
    help="""
        Sets the tail location in best-efforted number of lines relative to the end of logs.
        Default prints all the logs.
        Set to 0 to try try best-efforted logs from the current log position.
        See also --lines-timeout.
        """,
)
@click.option(
    "--lines-timeout",
    default=0.5,
    show_default=True,
    type=float,
    help="When using --lines the number of lines is best-efforted by ignoring lines for specific time",
)
@click.option(
    "--shutdown-timeout",
    default=2,
    show_default=True,
    type=float,
    help="Rather leave at 2 if you want all the logs.",
)
@click.option(
    "-f",
    "--follow",
    is_flag=True,
    help="Shorthand for --all --lines=10 to act similar to tail -f.",
)
@click.option(
    "--no-follow",
    is_flag=True,
    help="Just run once, get the logs in a best-effort style and exit.",
)
@click.option(
    "-t",
    "--task",
    type=re.compile,
    help="Only watch tasks names matching this regex.",
)
@click.option(
    "--polling",
    is_flag=True,
    help="Instead of listening to Nomad event stream, periodically poll for events",
)
@click.option(
    "-x",
    "--no-preserve-status",
    is_flag=True,
    help="Do not preserve tasks exit statuses",
)
@click_log_options()
@common_options()
@click.pass_context
def cli(ctx, **_):
    global args
    args = argparse.Namespace(**ctx.params)
    #
    if args.verbose > 1:
        http_client.HTTPConnection.debuglevel = 1
    global args_stream
    args_stream = Argsstream(
        err=any(s in "all stderr err e 2".split() for s in args.stream),
        out=any(s in "all stdout out o 1".split() for s in args.stream),
        alloc=any(s in "all alloc a".split() for s in args.stream),
    )
    if args.follow:
        args.lines = 10
        args.all = True
    if args.namespace:
        os.environ["NOMAD_NAMESPACE"] = nomad_find_namespace(args.namespace)
    #
    if args.lines >= 0:
        global args_lines_start_ns
        args_lines_start_ns = time.time_ns()
    # init logging
    if True:
        log_format_choose()
        logging.basicConfig(
            format=log_format.module,
            datefmt=args.log_timestamp_format,
            level=logging.DEBUG if args.verbose else logging.INFO,
        )
        # https://stackoverflow.com/questions/17558552/how-do-i-add-custom-field-to-python-log-format-string
        old_factory = logging.getLogRecordFactory()

        def record_factory(*args, **kwargs):
            record = old_factory(*args, **kwargs)
            for k, v in COLORS.items():
                setattr(record, k, v)
            return record

        logging.setLogRecordFactory(record_factory)


cli_jobid = click.argument(
    "jobid",
    shell_complete=complete_job(),
)
cli_jobfile = click.argument(
    "jobfile",
    shell_complete=click.File().shell_complete,
)

###############################################################################


@cli.command("alloc", help="Watch over specific allocation")
@click.argument(
    "allocid",
    shell_complete=completor(lambda: (x["ID"] for x in mynomad.get("allocations"))),
)
@common_options()
def mode_alloc(allocid):
    allocs = mynomad.get(f"allocations", params={"prefix": allocid})
    assert len(allocs) > 0, f"Allocation with id {allocid} not found"
    assert len(allocs) < 2, f"Multiple allocations found starting with id {allocid}"
    alloc = nomadlib.Alloc(allocs[0])
    mynomad.namespace = alloc.Namespace
    allocid = alloc.ID
    log.info(f"Watching allocation {allocid}")
    db = Db(
        topics=[f"Allocation:{alloc.JobID}"],
        filter_event_cb=lambda e: e.topic == EventTopic.Allocation
        and e.data["ID"] == allocid,
        init_cb=lambda: [
            Event(
                EventTopic.Allocation,
                EventType.AllocationUpdated,
                mynomad.get(f"allocation/{allocid}"),
            )
        ],
    )
    allocworkers = AllocWorkers()
    db.start()
    try:
        for event in db.events():
            if event.topic == EventTopic.Allocation:
                alloc = nomadlib.Alloc(event.data)
                allocworkers.notify(alloc)
                if alloc.is_finished():
                    log.info(
                        f"Allocation {allocid} has status {alloc.ClientStatus}. Exiting."
                    )
                    break
            if args.no_follow:
                break
    finally:
        db.stop()
        allocworkers.stop()
        db.join()
        allocworkers.join()
        exit(allocworkers.exitcode())


@cli.command("run", help="Run a Nomad job and then watch over it until it is finished.")
@cli_jobfile
@common_options()
def mode_run(jobfile):
    jobinit = nomad_start_job_and_wait(jobfile)
    do = NomadJobWatcherUntilFinished(jobinit)
    do.start()
    try:
        do.join()
    finally:
        # On exception, stop or purge the job if needed.
        if args.purge or args.stop:
            do.stop_job(args.purge)
        do.join()
    exit(do.exitcode())


@cli.command("job", help="Watch a Nomad job, show its logs and events.")
@cli_jobid
@common_options()
def mode_job(jobid):
    jobinit = nomad_find_job(jobid)
    NomadJobWatcherUntilFinished(jobinit).run_till_end()


@cli.command("start", help="Start a Nomad Job. Then act like started mode.")
@cli_jobfile
@common_options()
def mode_start(jobfile):
    jobinit = nomad_start_job_and_wait(jobfile)
    NomadJobWatcherUntilStarted(jobinit).run_till_end()


@cli.command(
    "started",
    help="""
Watch a Nomad job until the job has all allocations running.
Exit with 2 exit status when the job has status dead.
""",
)
@cli_jobid
@common_options()
def mode_started(jobid):
    jobinit = nomad_find_job(jobid)
    NomadJobWatcherUntilStarted(jobinit).run_till_end()


@cli.command(
    "stop",
    help="Stop a Nomad job and then watch the job until it is stopped - has no running allocations.",
)
@cli_jobid
@common_options()
def mode_stop(jobid: str):
    jobinit = nomad_find_job(jobid)
    do = NomadJobWatcherUntilFinished(jobinit)
    do.start()
    do.stop_job(args.purge)
    try:
        do.join()
    finally:
        do.close()
    exit(do.exitcode())


@cli.command(
    "stopped",
    help="Watch a Nomad job until the job is stopped - has not running allocation.",
)
@cli_jobid
@common_options()
def mode_stopped(jobid):
    jobinit = nomad_find_job(jobid)
    NomadJobWatcherUntilFinished(jobinit).run_till_end()


###############################################################################

if __name__ == "__main__":
    try:
        cli.main()
    finally:
        mynomad.session.close()
