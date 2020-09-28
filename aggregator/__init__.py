def _config_logging():
    # Aggressively take over control of logging from Scrapy
    # This function modifies sys.argv
    # Set LOG_USE_CUSTOM_CONFIG=False to give control back to Scrapy

    import argparse
    import logging
    import sys
    from logging.config import dictConfig
    from operator import itemgetter
    from pathlib import Path

    from scrapy.settings import Settings

    from . import settings as scrapy_settings
    from .logger import make_logging_config

    g = vars(scrapy_settings)
    if not g.get('LOG_USE_CUSTOM_CONFIG'):
        return

    defaults = [
        ('output', g.get('LOG_FILE', None)),
        ('enabled', g.get('LOG_ENABLED', True)),
        ('encoding', g.get('LOG_ENCODING', None)),
        ('level', g.get('LOG_LEVEL', 20)),
        ('style', g.get('LOG_FORMAT', 'standard')),
        ('datefmt', g.get('LOG_DATEFORMAT', None)),
        ('log_stdout', g.get('LOG_STDOUT', False)),
        ('log_short_names', g.get('LOG_SHORT_NAMES', False)),
        ('overrides', g.get('LOGGING_OVERRIDES', [])),
        ('colored', g.get('LOGGING_COLORED', True)),
    ]
    settings = ('LOG_FILE', 'LOG_ENABLED', 'LOG_ENCODING', 'LOG_LEVEL',
                'LOG_FORMAT', 'LOG_DATEFORMAT', 'LOG_STDOUT', 'LOG_SHORT_NAMES',
                'LOGGING_OVERRIDES', 'LOGGING_COLORED')
    settings_getter = itemgetter(*settings)
    settings_default = dict(zip(settings, [t[1] for t in defaults]))

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-s', '--settings', action='append', default=[])
    parser.add_argument('--nolog', action='store_true')
    parser.add_argument('--logfile')
    parser.add_argument('-L', '--loglevel')
    args, _ = parser.parse_known_args(sys.argv[1:])

    settings = [s.split('=', 1) for s in args.settings]
    settings = [t for t in settings if len(t) == 2]
    settings = {t[0]: t[1] for t in settings if t[0] in settings_default}
    settings = {**settings_default, **settings}
    if args.nolog:
        settings['LOG_ENABLED'] = False
    if args.logfile:
        settings['LOG_FILE'] = args.logfile
    if args.loglevel:
        settings['LOG_LEVEL'] = args.loglevel

    config = Settings(dict(zip([t[0] for t in defaults], settings_getter(settings))))

    if config.getbool('enabled'):
        overrides = []
        if config['output']:
            encoding = config['encoding'] or 'utf8'
            overrides.append({'handlers': {'console': {'stream': open(config.pop('output'), 'a+', encoding=encoding)}}})
        if config['datefmt']:
            overrides.append({'formatters': {'fmt': {'datefmt': config.pop('datefmt')}}})
        if config['log_stdout']:
            config.pop('log_stdout')
            from scrapy.utils.log import StreamLogger
            sys.stdout = StreamLogger(logging.getLogger('stdout'))
        if config['log_short_names']:
            config.pop('log_short_names')
            from scrapy.utils.log import TopLevelFormatter
            overrides.append({
                'filters': {
                    'tlfmt': {
                        '()': TopLevelFormatter,
                        'loggers': ['scrapy', 'main', 'worker'],
                    }}})
        overrides += config['overrides']
        dictConfig(make_logging_config('feedly', *overrides, **config))

    if Path(sys.argv[0]).name == 'scrapy':
        # Is Scrapy command
        enabled = {f'{k}={v}' for k, v in settings.items()}
        pos = []
        for i in range(len(sys.argv)):
            if sys.argv[i] == '--logfile':
                pos.extend([i, i + 1])
            if sys.argv[i] in enabled:
                pos.extend([i - 1, i])
        pos = sorted(pos, reverse=True)
        for i in pos:
            sys.argv.pop(i)
        sys.argv.append('--nolog')


_config_logging()