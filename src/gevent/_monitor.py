# Copyright (c) 2018 gevent. See LICENSE for details.
from __future__ import print_function, absolute_import, division

import sys
import traceback

from weakref import ref as wref

from greenlet import settrace
from greenlet import getcurrent

from gevent import config as GEVENT_CONFIG
from gevent.monkey import get_original
from gevent.util import format_run_info

from gevent._compat import thread_mod_name
from gevent._util import gmctime

# Clocks
try:
    # Python 3.3+ (PEP 418)
    from time import perf_counter
except ImportError:
    import time

    if sys.platform == "win32":
        perf_counter = time.clock
    else:
        perf_counter = time.time

__all__ = [
    'PeriodicMonitoringThread',
]

get_thread_ident = get_original(thread_mod_name, 'get_ident')
start_new_thread = get_original(thread_mod_name, 'start_new_thread')
thread_sleep = get_original('time', 'sleep')

class _MonitorEntry(object):

    __slots__ = ('function', 'period', 'last_run_time')

    def __init__(self, function, period):
        self.function = function
        self.period = period
        self.last_run_time = 0

    def __eq__(self, other):
        return self.function == other.function and self.period == other.period

    def __repr__(self):
        return repr((self.function, self.period, self.last_run_time))

class PeriodicMonitoringThread(object):

    # The amount of seconds we will sleep when we think we have nothing
    # to do.
    inactive_sleep_time = 2.0


    # A counter, incremented by the greenlet trace function
    # we install on every greenlet switch. This is reset when the
    # periodic monitoring thread runs.
    _greenlet_switch_counter = 0
    # The greenlet being switched to.
    _active_greenlet = None

    # The trace function that was previously installed,
    # if any.
    previous_trace_function = None

    # The absolute minimum we will sleep, regardless of
    # what particular monitoring functions want to say.
    min_sleep_time = 0.005

    # A list of _MonitorEntry objects: [(function(hub), period, last_run_time))]
    # The first entry is always our entry for self.monitor_blocking
    _monitoring_functions = None

    # The calculated min sleep time for the monitoring functions list.
    _calculated_sleep_time = None

    def __init__(self, hub):
        self._hub_wref = wref(hub, self._on_hub_gc)
        self.should_run = True

        # Must be installed in the thread that the hub is running in;
        # the trace function is threadlocal
        assert get_thread_ident() == hub.thread_ident
        prev_trace = settrace(self.greenlet_trace)
        self.previous_trace_function = prev_trace

        self._monitoring_functions = [_MonitorEntry(self.monitor_blocking,
                                                    GEVENT_CONFIG.max_blocking_time)]
        self._calculated_sleep_time = GEVENT_CONFIG.max_blocking_time
        # Create the actual monitoring thread. This is effectively a "daemon"
        # thread.
        self.monitor_thread_ident = start_new_thread(self, ())

    @property
    def hub(self):
        return self._hub_wref()

    def greenlet_trace(self, event, args):
        # This function runs in the thread we are monitoring.
        self._greenlet_switch_counter += 1
        if event in ('switch', 'throw'):
            # args is (origin, target). This is the only defined
            # case
            self._active_greenlet = args[1]
        else:
            self._active_greenlet = None
        if self.previous_trace_function is not None:
            self.previous_trace_function(event, args)

    def monitoring_functions(self):
        # Return a list of _MonitorEntry objects

        # Update max_blocking_time each time.
        mbt = GEVENT_CONFIG.max_blocking_time # XXX: Events so we know when this changes.
        if mbt != self._monitoring_functions[0].period:
            self._monitoring_functions[0].period = mbt
            self._calculated_sleep_time = min(x.period for x in self._monitoring_functions)
        return self._monitoring_functions

    def add_monitoring_function(self, function, period):
        """
        Schedule the *function* to be called approximately every *period* fractional seconds.

        The *function* receives one argument, the hub being monitored. It is called
        in the monitoring thread, *not* the hub thread.

        If the *function* is already a monitoring function, then its *period*
        will be updated for future runs.

        If the *period* is ``None``, then the function will be removed.

        A *period* less than or equal to zero is not allowed.

        """
        if not callable(function):
            raise ValueError("function must be callable")

        if period is None:
            # Remove.
            self._monitoring_functions = [
                x for x in self._monitoring_functions
                if x.function != function
            ]
        elif period <= 0:
            raise ValueError("Period must be positive.")
        else:
            # Add or update period
            entry = _MonitorEntry(function, period)
            self._monitoring_functions = [
                x if x.function != function else entry
                for x in self._monitoring_functions
            ]
            if entry not in self._monitoring_functions:
                self._monitoring_functions.append(entry)
        self._calculated_sleep_time = min(x.period for x in self._monitoring_functions)

    def calculate_sleep_time(self):
        min_sleep = self._calculated_sleep_time
        if min_sleep <= 0:
            # Everyone wants to be disabled. Sleep for a longer period of
            # time than usual so we don't spin unnecessarily. We might be
            # enabled again in the future.
            return self.inactive_sleep_time
        return max((min_sleep, self.min_sleep_time))

    def kill(self):
        if not self.should_run:
            # Prevent overwriting trace functions.
            return
        # Stop this monitoring thread from running.
        self.should_run = False
        # Uninstall our tracing hook
        settrace(self.previous_trace_function)
        self.previous_trace_function = None

    def _on_hub_gc(self, _):
        self.kill()

    def __call__(self):
        # The function that runs in the monitoring thread.
        # We cannot use threading.current_thread because it would
        # create an immortal DummyThread object.
        getcurrent().gevent_monitoring_thread = wref(self)

        try:
            while self.should_run:
                functions = self.monitoring_functions()
                assert functions
                sleep_time = self.calculate_sleep_time()

                thread_sleep(sleep_time)

                # Make sure the hub is still around, and still active,
                # and keep it around while we are here.
                hub = self.hub
                if not hub:
                    self.kill()

                if self.should_run:
                    this_run = perf_counter()
                    for entry in functions:
                        f = entry.function
                        period = entry.period
                        last_run = entry.last_run_time
                        if period and last_run + period <= this_run:
                            entry.last_run_time = this_run
                            f(hub)
                del hub # break our reference to hub while we sleep

        except SystemExit:
            pass
        except: # pylint:disable=bare-except
            # We're a daemon thread, so swallow any exceptions that get here
            # during interpreter shutdown.
            if not sys or not sys.stderr: # pragma: no cover
                # Interpreter is shutting down
                pass
            else:
                hub = self.hub
                if hub is not None:
                    # XXX: This tends to do bad things like end the process, because we
                    # try to switch *threads*, which can't happen. Need something better.
                    hub.handle_error(self, *sys.exc_info())

    def monitor_blocking(self, hub):
        # Called periodically to see if the trace function has
        # fired to switch greenlets. If not, we will print
        # the greenlet tree.

        # For tests, we return a true value when we think we found something
        # blocking

        # There is a race condition with this being incremented in the
        # thread we're monitoring, but probably not often enough to lead
        # to annoying false positives.
        active_greenlet = self._active_greenlet
        did_switch = self._greenlet_switch_counter != 0
        self._greenlet_switch_counter = 0

        if did_switch or active_greenlet is None or active_greenlet is hub:
            # Either we switched, or nothing is running (we got a
            # trace event we don't know about or were requested to
            # ignore), or we spent the whole time in the hub, blocked
            # for IO. Nothing to report.
            return

        report = ['=' * 80,
                  '\n%s : Greenlet %s appears to be blocked' %
                  (gmctime(), active_greenlet)]
        report.append("    Reported by %s" % (self,))
        try:
            frame = sys._current_frames()[hub.thread_ident]
        except KeyError:
            # The thread holding the hub has died. Perhaps we shouldn't
            # even report this?
            stack = ["Unknown: No thread found for hub %r\n" % (hub,)]
        else:
            stack = traceback.format_stack(frame)
        report.append('Blocked Stack (for thread id %s):' % (hex(hub.thread_ident),))
        report.append(''.join(stack))
        report.append("Info:")
        report.extend(format_run_info(greenlet_stacks=False,
                                      current_thread_ident=self.monitor_thread_ident))
        report.append(report[0])
        hub.exception_stream.write('\n'.join(report))
        return (active_greenlet, report)

    def ignore_current_greenlet_blocking(self):
        # Don't pay attention to the current greenlet.
        self._active_greenlet = None

    def monitor_current_greenlet_blocking(self):
        self._active_greenlet = getcurrent()

    def __repr__(self):
        return '<%s at %s in thread %s greenlet %r for %r>' % (
            self.__class__.__name__,
            hex(id(self)),
            hex(self.monitor_thread_ident),
            getcurrent(),
            self._hub_wref())
