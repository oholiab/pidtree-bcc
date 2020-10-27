import logging
from typing import List

from pidtree_bcc.utils import find_subclass


class BasePlugin:

    # Specifies which probes are compatible with the plugin
    # Set to "*" to allow all probes
    PROBE_SUPPORT = tuple()

    def __init__(self, args: dict):
        """ Constructor

        :param dict args: plugin parameters
        """
        self.validate_args(args)

    def process(self, event: dict) -> dict:
        """ Process the `event` dict, add in additional metadata and return a dict

        :param dict event: event dictionary
        :return: processed event dictionary
        """
        raise NotImplementedError(
            'Required method `process` has not been implemented by {}'.format(self.__name__),
        )

    def validate_args(self, args: dict):
        """ Not required, override in inheriting class if you want to use this

        :param dict args: plugin parameters
        """
        pass


def load_plugins(plugin_dict: dict, calling_probe: str, plugin_dir: str = 'pidtree_bcc.plugins') -> List[BasePlugin]:
    """ Load and configure plugins

    :param dict plugin_dict: where the keys are plugin names and the value
                             for each key is another dict of kwargs. Each key
                             must match a `.py` file in the plugin directory
    :param str calling_probe: name of the calling probe for support validation
    :param str plugin_dir: (optional) module path for plugins
    :return: list of loaded plugins
    """
    plugins = []
    for plugin_name, plugin_args in plugin_dict.items():
        error = None
        unload_on_init_exception = plugin_args.get(
            'unload_on_init_exception', False,
        )
        if not plugin_args.get('enabled', True):
            continue
        import_line = '.'.join([plugin_dir, plugin_name])
        try:
            plugin_class = find_subclass(import_line, BasePlugin)
            if plugin_class.PROBE_SUPPORT != '*' and calling_probe not in plugin_class.PROBE_SUPPORT:
                raise RuntimeError(
                    '{} is not among supported probes for plugin {}: {}'
                    .format(calling_probe, plugin_name, plugin_class.PROBE_SUPPORT),
                )
            plugins.append(plugin_class(plugin_args))
        except ImportError as e:
            error = RuntimeError(
                'Could not import {import_line}: {e}'.format(
                    import_line=import_line,
                    e=e,
                ),
            )
        except StopIteration as e:
            error = RuntimeError(
                'Could not find plugin class in module {import_line}: {e}'.format(
                    import_line=import_line,
                    e=e,
                ),
            )
        except Exception as e:
            error = e
        finally:
            if error:
                if unload_on_init_exception:
                    logging.error(str(error))
                else:
                    raise error
    return plugins
