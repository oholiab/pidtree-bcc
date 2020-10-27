import inspect
import json
import os.path
import re
from datetime import datetime
from multiprocessing import SimpleQueue
from threading import Thread
from typing import Any

from bcc import BPF
from jinja2 import Template

from pidtree_bcc.plugins import load_plugins


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

    def __init__(self, output_queue: SimpleQueue, probe_config: dict = {}):
        """ Constructor

        :param Queue output_queue: queue for event output
        :param dict probe_config: (optional) config passed as kwargs to BPF template
                                  all fields are passed to the template engine with the exception
                                  of "plugins". This behaviour can be overidden with the TEMPLATE_VARS
                                  class variable defining a list of config fields.
                                  It is possible for child class to define a CONFIG_DEFAULTS class
                                  variable containing default templating variables.
        """
        self.output_queue = output_queue
        self.validate_config(probe_config)
        self.plugins = load_plugins(probe_config.get('plugins', {}))
        module_src = inspect.getsourcefile(type(self))
        self.probe_name = os.path.basename(module_src).split('.')[0]
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
        self.expanded_bpf_text = Template(self.BPF_TEXT).render(**template_config)

    def _process_events(self, cpu: Any, data: Any, size: Any, from_bpf: bool = True):
        """ BPF event callback

        :param Any cpu: unused arg required for callback
        :param Any data: BPF raw event
        :param Any size: unused arg required for callback
        :param bool from_bpf: event generated by BPF code
        """
        event = self.bpf['events'].event(data) if from_bpf else data
        event = self.enrich_event(event)
        event['timestamp'] = datetime.utcnow().isoformat() + 'Z'
        event['probe'] = self.probe_name
        for event_plugin in self.plugins:
            event = event_plugin.process(event)
        self.output_queue.put(json.dumps(event))

    def start_polling(self):
        """ Start infinite loop polling BPF events """
        for func, args in self.SIDECARS:
            Thread(target=func, args=args, daemon=True).start()
        self.bpf = BPF(text=self.expanded_bpf_text)
        self.bpf['events'].open_perf_buffer(self._process_events)
        while True:
            self.bpf.perf_buffer_poll()

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
