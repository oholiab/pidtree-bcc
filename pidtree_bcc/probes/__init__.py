import inspect
import json
import os.path
import re
from datetime import datetime
from multiprocessing import SimpleQueue
from threading import Thread
from typing import Any
from typing import Mapping

from bcc import BPF
from jinja2 import Environment
from jinja2 import FileSystemLoader

from pidtree_bcc.plugins import load_plugins
from pidtree_bcc.utils import find_subclass


class BPFProbe:
    """ Base class for defining BPF probes.

    Takes care of loading a BPF program and polling events.
    The BPF program can be either define in the `BPF_TEXT` class variable or
    in a Jinja template file (.j2) with the same basename of the module file.
    In either case the program text will be processed in Jinja templating.
    """

    # List of (function, args) tuples to run in parallel with the probes as "sidecars"
    # No health monitoring is performed on these after launch so they are expect to be
    # stable or self-healing.
    SIDECARS = []

    # To be populated by `load_probes`
    EXTRA_PLUGIN_PATH = None

    def __init__(self, output_queue: SimpleQueue, probe_config: dict = {}, lost_event_telemetry: int = -1):
        """ Constructor

        :param Queue output_queue: queue for event output
        :param dict probe_config: (optional) config passed as kwargs to BPF template
                                  all fields are passed to the template engine with the exception
                                  of "plugins". This behaviour can be overidden with the TEMPLATE_VARS
                                  class variable defining a list of config fields.
                                  It is possible for child class to define a CONFIG_DEFAULTS class
                                  variable containing default templating variables.
        :param int lost_event_telemetry: every how many messages emit the number of lost messages.
                                         Set to <= 0 to disable.
        """
        self.output_queue = output_queue
        self.validate_config(probe_config)
        module_src = inspect.getsourcefile(type(self))
        self.probe_name = os.path.basename(module_src).split('.')[0]
        self.plugins = load_plugins(
            probe_config.get('plugins', {}),
            self.probe_name,
            self.EXTRA_PLUGIN_PATH,
        )
        if not hasattr(self, 'BPF_TEXT'):
            with open(re.sub(r'\.py$', '.j2', module_src)) as f:
                self.BPF_TEXT = f.read()
        template_config = (
            {**self.CONFIG_DEFAULTS, **probe_config}
            if hasattr(self, 'CONFIG_DEFAULTS')
            else probe_config.copy()
        )
        if hasattr(self, 'TEMPLATE_VARS'):
            template_config = {k: template_config[k] for k in self.TEMPLATE_VARS}
        else:
            template_config.pop('plugins', None)
        jinja_env = Environment(loader=FileSystemLoader(os.path.dirname(module_src)))
        self.expanded_bpf_text = jinja_env.from_string(self.BPF_TEXT).render(**template_config)
        self.lost_event_telemetry = lost_event_telemetry
        self.lost_event_timer = lost_event_telemetry
        self.lost_event_count = 0

    def _process_events(self, cpu: Any, data: Any, size: Any, from_bpf: bool = True):
        """ BPF event callback

        :param Any cpu: unused arg required for callback
        :param Any data: BPF raw event
        :param Any size: unused arg required for callback
        :param bool from_bpf: (optional, default=True) event generated by BPF code
        """
        event = self.bpf['events'].event(data) if from_bpf else data
        event = self.enrich_event(event)
        if not event:
            return
        self._add_event_metadata(event)
        for event_plugin in self.plugins:
            event = event_plugin.process(event)
        self.output_queue.put(json.dumps(event))

    def _add_event_metadata(self, event: dict):
        """ Adds probe name and current ISO-format timestamp to event dictionary (in place)

        :param dict event: event dictionary
        """
        event['timestamp'] = datetime.utcnow().isoformat() + 'Z'
        event['probe'] = self.probe_name

    def _lost_event_callback(self, lost_count: int):
        """ Method to be used as callback to count lost events

        :param int lost_count: number of events lost
        """
        self.lost_event_count += lost_count

    def _poll_and_check_lost(self):
        """ Simple wrapper method which outputs lost event telemetry while polling """
        self.bpf.perf_buffer_poll()
        self.lost_event_timer -= 1
        if self.lost_event_timer == 0:
            self.lost_event_timer = self.lost_event_telemetry
            event = {'type': 'lost_event_telemetry', 'count': self.lost_event_count}
            self._add_event_metadata(event)
            self.output_queue.put(json.dumps(event))

    def start_polling(self):
        """ Start infinite loop polling BPF events """
        for func, args in self.SIDECARS:
            Thread(target=func, args=args, daemon=True).start()
        self.bpf = BPF(text=self.expanded_bpf_text)
        if self.lost_event_telemetry > 0:
            extra_args = {'lost_cb': self._lost_event_callback}
            poll_func = self._poll_and_check_lost
        else:
            extra_args = {}
            poll_func = self.bpf.perf_buffer_poll
        self.bpf['events'].open_perf_buffer(self._process_events, **extra_args)
        while True:
            poll_func()

    def enrich_event(self, event: Any) -> dict:
        """ Transform raw BPF event data into dictionary,
        possibly adding more interesting data to it.

        :param Any event: BPF event data
        """
        raise NotImplementedError

    def validate_config(self, config: dict):
        """ Overridable method to implement config validation.
        Should raise exceptions on errors.

        :param dict config: probe configuration
        """
        pass


def load_probes(
    config: dict,
    output_queue: SimpleQueue,
    extra_probe_path: str = None,
    extra_plugin_path: str = None,
    lost_event_telemetry: int = -1,
) -> Mapping[str, BPFProbe]:
    """ Find and load probe classes

    :param dict config: pidtree-bcc configuration
    :param Queue output_queue: queue for event output
    :param str extra_probe_path: (optional) additional package path where to look for probes
    :param str extra_probe_path: (optional) additional package path where to look for plugins
    :param int lost_event_telemetry: (optional) every how many messages emit the number of lost messages.
    :return: dictionary mapping probe name to its instance
    """
    BPFProbe.EXTRA_PLUGIN_PATH = extra_plugin_path
    packages = [p for p in (__package__, extra_probe_path) if p]
    return {
        probe_name: find_subclass(
            ['{}.{}'.format(p, probe_name) for p in packages],
            BPFProbe,
        )(output_queue, probe_config, lost_event_telemetry)
        for probe_name, probe_config in config.items()
        if not probe_name.startswith('_')
    }
