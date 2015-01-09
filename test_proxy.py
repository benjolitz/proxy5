#!/usr/bin/env python
import logging
import time
from proxy5 import (MitmProxy, utils)

if __name__ == "__main__":
    import optparse
    try:
        import __pypy__
        __pypy__
    except ImportError:
        __pypy__ = False

    parser = optparse.OptionParser()
    parser.add_option(
        '-p', '--performance', help="Disable Logger for performance",
        default=__pypy__, action="store_true")
    parser.add_option(
        '-t', '--timeout', help="disable after n seconds",
        default=float('inf'), type='int')
    parser.add_option(
        '-a', '--all',
        help='show all logger messages (enables logging on PyPy at a cost!)',
        action="store_true", default=False)
    parser.add_option(
        '--port', default=0,
        help='set a port or let it be dynamic', type='int')
    options, args = parser.parse_args()

    if options.performance and not options.all:
        logging  # shut up the linter
        logging = \
            utils.FakeLogging()

    logger = logging.getLogger('trollius')
    logger.addHandler(logging.StreamHandler())
    logger.handlers[0].setLevel(logging.DEBUG)
    logger.setLevel(logging.DEBUG)

    logger = logging.getLogger('proxy5')
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            '[%(asctime)s] [%(levelname)s] [%(name)s]  %(message)s'))
    logger.addHandler(handler)
    handler.setLevel(logging.INFO)
    if options.all:
        handler.setLevel(logging.DEBUG)
    logger.setLevel(logging.DEBUG)

    proxy = MitmProxy(logger, port=options.port)
    logger.info("Port {0!r}".format(proxy.port))
    proxy.start()
    try:
        ts = time.time()
        while time.time() - ts < options.timeout:
            time.sleep(0.1)
        proxy.stop()
    except KeyboardInterrupt:
        pass
    proxy.stop()
    ts = time.time()
    while proxy.is_alive():
        time.sleep(0.1)
    print("Took {0:.2f} s".format(time.time() - ts))
