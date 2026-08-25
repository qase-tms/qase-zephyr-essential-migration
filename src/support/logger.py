import datetime
import os
import sys


class Logger:
    """Level-based logger.

    Levels are additive: each includes everything above it in this list.

        error    only failures that stop or corrupt the migration
        warn     plus anything skipped, truncated or silently defaulted
        info     plus normal progress: entities migrated, counts, timings
        verbose  plus request URIs and HTTP status codes
        debug    plus request parameters and bodies

    'error' and 'warn' always reach the console, whatever the level. A
    migration that quietly drops data must not look successful in the
    terminal. Everything else goes to the console only at 'verbose' or
    above, so the default run stays readable next to the progress lines.
    """

    LEVELS = {'error': 0, 'warn': 1, 'info': 2, 'verbose': 3, 'debug': 4}
    _ALIASES = {'warning': 'warn', 'err': 'error', 'trace': 'debug'}
    _COLORS = {'error': '31', 'warn': '33'}
    _ICONS = {'error': '\u2717', 'warn': '!'}

    def __init__(self, level: str = 'info', write_to_file: bool = True, log_dir: str = './logs', prefix: str = ''):
        self.level_name = self._normalise(level)
        self.level = self.LEVELS[self.level_name]
        self.write_to_file = write_to_file
        self.log_file = None

        if self.write_to_file:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f'{prefix}_zephyr_essential_{timestamp}.log' if prefix else f'zephyr_essential_{timestamp}.log'
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
            self.log_file = os.path.join(log_dir, filename)
            with open(self.log_file, 'w'):
                pass

    @classmethod
    def _normalise(cls, level) -> str:
        if level is None:
            return 'info'
        # Accept the old boolean debug flag so an existing config still runs
        if isinstance(level, bool):
            return 'debug' if level else 'info'
        name = str(level).strip().lower()
        name = cls._ALIASES.get(name, name)
        return name if name in cls.LEVELS else 'info'

    def log(self, message: str, level: str = 'info'):
        name = self._normalise(level)
        severity = self.LEVELS[name]

        if severity > self.level:
            return

        time_str = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{time_str}][{name}] {message}"

        if self.write_to_file and self.log_file:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(line + "\n")

        if severity <= self.LEVELS['warn']:
            color = self._COLORS.get(name, '0')
            icon = self._ICONS.get(name, '')
            # Leading newline so the message does not land on top of a progress
            # line, which print_status redraws with a carriage return
            print(f"\n\t\033[{color}m{icon}\033[0m {line}", file=sys.stderr, flush=True)
        elif self.level >= self.LEVELS['verbose']:
            print(line, flush=True)

    def divider(self):
        self.log('-----------------------------------')

    def print_status(self, message: str, completed: int = 0, total: int = 0, level: int = 0):
        icon = '↪'
        color_code = '34'
        if completed != 0 and total != 0:
            message = f"{message} [{completed}/{total}]"
        if completed == total:
            icon = '✓'
            color_code = '32'

        tabs = '\t'
        for i in range(level):
            tabs += '  '
        print(f"{tabs}\033[{color_code}m{icon}\033[0m {message}", end='\r', flush=True)
        if completed == total:
            print()

    def print_group(self, message: str):
        print(f"\t\033[35m↪\033[0m {message}", end='\r')
        print()