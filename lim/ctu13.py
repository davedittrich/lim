# -*- coding: utf-8 -*-

from __future__ import print_function

import aiohttp
import asyncio
import async_timeout
import ipaddress
import json
import logging
import os
import signal
import six
import time
import requests
import textwrap
import warnings

from bs4 import BeautifulSoup  # noqa
from collections import OrderedDict
from cliff.command import Command
from cliff.lister import Lister
from datetime import datetime
from urllib3.exceptions import InsecureRequestWarning
from .main import BUFFER_SIZE
from .main import DEFAULT_PROTOCOLS
from .utils import safe_to_open
from .utils import LineReader

# TODO(dittrich): Make this a command line argument?
__ASYNC_TIMEOUT__ = 10
__SEMAPHORE_LIMIT__ = 10
__DATASETS_URLS__ = {
    'ctu13': 'https://www.stratosphereips.org/datasets-ctu13',
    'mixed': 'https://www.stratosphereips.org/datasets-mixed',
    'normal': 'https://www.stratosphereips.org/datasets-normal',
    'malware': 'https://www.stratosphereips.org/datasets-malware'
}
__GROUPS__ = [g for g, _ in __DATASETS_URLS__.items()]
__DATASETS_URL__ = __DATASETS_URLS__['ctu13']

# Initialize a logger for this module.
logger = logging.getLogger(__name__)


def unhex(x):
    """Ensure hexidecimal strings are converted to decimal form"""
    if x == '':
        return '0'
    elif x.startswith('0x'):
        return str(int(x, base=16))
    else:
        return x


# TODO(dittrich): Add support for IPv6
def IPv4ToID(x):
    """
    Convert IPv4 dotted-quad address to INT for more
    efficient use with xGT.
    """

#     try:
#         if six.PY2:
#             id = int(ipaddress.IPv4Address(x.decode('utf-8')))
#         else:
#             id = int(ipaddress.IPv4Address(x))
#     except ipaddress.AddressValueError as err:
#         if 'Expected 4 octets' in err.str:
#             logger.info(str(err))
#     return id

    if six.PY2:
        id = int(ipaddress.IPv4Address(x.decode('utf-8')))
    else:
        id = int(ipaddress.IPv4Address(x))
    return id


def download_ctu_netflow(url=None,
                         datadir='',
                         maxlines=None,
                         protocols=['any'],
                         force=False):
    """
    Get CTU Netflow data BZ2 file, decompressing into
    CSV data file. This function also filters input by
    row and/or column to produce "clean" data for use
    by xGT without further post-load processing.

    Examples of (randomly sampled) first lines:
    Botnet 17-1:  'StartTime,Dur,Proto,SrcAddr,Sport,Dir,DstAddr,Dport,State,sTos,dTos,TotPkts,TotBytes,SrcBytes,srcUdata,dstUdata,Label\n'
    Botnet 50:    'StartTime,Dur,Proto,SrcAddr,Sport,Dir,DstAddr,Dport,State,sTos,dTos,TotPkts,TotBytes,SrcBytes,Label\n'
    Botnet 367-1: 'StartTime,Dur,Proto,SrcAddr,Sport,Dir,DstAddr,Dport,State,sTos,dTos,TotPkts,TotBytes,SrcBytes,SrcPkts,Label\n'

    """  # noqa

    infilename = url.split('/')[-1]
    outfilename = os.path.join(datadir, infilename)
    safe_to_open(outfilename, force)
    _columns = ['StartTime', 'Dur', 'Proto', 'SrcAddr', 'Sport',  # noqa
                'Dir', 'DstAddr', 'Dport', 'State', 'sTos',
                'dTos', 'TotPkts', 'TotBytes', 'SrcBytes', 'Label']
    _remove_columns = ['Dir', 'sTos', 'dTos']  # noqa
    use_columns = [0, 1, 2, 3, 4, 6, 7, 8, 11, 12, 13, 14]
    with open(outfilename, 'wb') as fp:
        # We're disabling SSL certificate verification ("verify=False")
        # in this specific case because (a) the CTU web server
        # certificate is invalid, and (b) we're only loading data
        # for test purposes. DON'T DO THIS as a general rule.
        linereader = LineReader(url,
                                verify=False,
                                buffer_size=BUFFER_SIZE)
        # header = linereader.get_header()
        # fp.write(header.encode('UTF-8') + '\n')
        count = 0
        _filter_protocols = len(protocols) > 0 or \
            (len(protocols) == 1 and 'any' in protocols)
        for record in [line.strip() for line in linereader.readlines()]:
            # Skip all lines after first (header) line that we don't want.
            fields = record.strip().split(',')
            if count > 0:
                if _filter_protocols and fields[2] not in protocols:
                    continue
                else:
                    # Convert datetimes to epoch times for faster comparisons
                    try:
                        fields[0] = str(datetime.strptime(fields[0],
                                        '%Y/%m/%d %H:%M:%S.%f').timestamp())
                    except Exception as err:  # noqa
                        pass
                    # Convert ICMP hex fields to decimal values so all ports
                    # can be inserted into xGT as INT instead of TEXT.
                    if fields[2] == 'icmp':
                        fields[4] = unhex(fields[4])
                        fields[7] = unhex(fields[7])
            # Rejoin only desired columns
            record = ','.join([fields[i] for i in use_columns]) + '\n'
            fp.write(record.encode('UTF-8'))
            count += 1
            # The CTU datasets have a header line. Make sure to not
            # count it when comparing maxlines.
            if maxlines is not None and count > int(maxlines):
                break
        logger.info('[+] wrote file {}'.format(outfilename))


class CTU_Dataset(object):
    """
    Class for CTU dataset metadata.

    This class gets metadata about available labeled and unlabeled
    bi-directional NetFlow data files from the CTU dataset. It does
    this by scraping the CTU web site for metadata and identifying
    the path to the file (which varies, depending on whether it
    is the unlabeled version, or the post-processed labeled
    version.

    Since it takes a long time to scrape the web site, a cache of
    collected metadata is used. If the timeout period for the cache
    has expired, the file does not exist, or the --ignore-cache
    flag is given, the site will be scraped.
    """

    #__DATASETS_URL__ = 'https://mcfp.felk.cvut.cz/publicDatasets/'
    # __DATASETS_URL__ = 'https://www.stratosphereips.org/datasets-malware'
    __NETFLOW_DATA_DIR__ = 'detailed-bidirectional-flow-labels/'
    __CACHE_FILE__ = "ctu-cache.json"
    __CACHE_TIMEOUT__ = 60 * 60 * 24 * 7  # secs * mins * hours * days
    __CORE_SCENARIOS__ = [str(s) for s in range(42, 59)]
    __COLUMNS__ = [
        'SCENARIO',
        'GROUP',
        'SCENARIO_URL',
        'PROBABLE_NAME',
        'ZIP',
        'MD5',
        'SHA1',
        'SHA256',
        'LABELED',
        'BINETFLOW',
        'PCAP'
    ]

    def __init__(self,
                 groups=['ctu13'],
                 columns=__COLUMNS__,
                 cache_timeout=__CACHE_TIMEOUT__,
                 semaphore_limit=__SEMAPHORE_LIMIT__,
                 ignore_cache=False,
                 cache_file=__CACHE_FILE__,
                 debug=False):
        """Initialize object."""

        for g in groups:
            if g not in __GROUPS__:
                valid = ",".join([i for i in __GROUPS__])
                raise RuntimeError(
                    'Dataset group "{}" '.format(g) +
                    'not found in [{}]'.format(valid)
                    )
        if 'all' in groups:
            self.groups = __GROUPS__
        else:
            self.groups = groups
        self.columns = columns
        self.cache_timeout = cache_timeout
        self.semaphore_limit = semaphore_limit
        self.sem = asyncio.Semaphore(self.semaphore_limit)
        self.ignore_cache = ignore_cache
        self.cache_file = cache_file
        self.debug = debug

        # Attributes
        self.scenarios = OrderedDict()
        self.session = None
        self.info_dict = dict()
        self.loop = None
        self.netflow_urls = dict()
        self.attributes = dict()

    def load_ctu_metadata(self):
        if not self.cache_expired() and not self.ignore_cache:
            self.read_cache()
        else:
            self.loop = asyncio.get_event_loop()
            self.loop.add_signal_handler(signal.SIGINT, self.loop.stop)
            for group in self.groups:
                scenarios = self.get_scenarios(
                    group,
                    url=__DATASETS_URLS__[group])
                self.loop.run_until_complete(self.fetch_main(group, scenarios))
            self.loop.close()
            self.write_cache()

    async def record_scenario_metadata(self, group, url=None):
        if url is None:
            raise RuntimeError('url must not be None')
        url_parts = url.split('/')
        name = url_parts[url_parts.index('publicDatasets')+1]
        if name not in self.scenarios:
            self.scenarios[name] = dict()
        _scenario = self.scenarios[name]
        _scenario['GROUP'] = group
        _scenario['URL'] = url
        page = await self.fetch_scenario(url)
        # Underscore on _page means ignore later (logic coupling)
        _scenario['_PAGE'] = page
        _scenario['_SUCCESS'] = page not in ["", None] \
            and "Not Found" not in page
        # Process links
        soup = BeautifulSoup(page, 'html.parser')
        # Scrape page for metadata
        for line in soup.text.splitlines():
            if self.__NETFLOW_DATA_DIR__ in line:
                # TODO(dittrich): Parse subpage to get labeled binetflow
                subpage = await self.fetch(
                    url + self.__NETFLOW_DATA_DIR__)
                subsoup = BeautifulSoup(subpage, 'html.parser')
                for item in subsoup.findAll('a'):
                    if item['href'].endswith('.binetflow'):
                        _scenario['BINETFLOW'] = \
                            self.__NETFLOW_DATA_DIR__ + item['href']
                        break
            if ":" in line and line != ":":
                try:
                    (_k, _v) = line.split(':')
                    k = _k.upper().replace(' ', '_')
                    v = _v.strip()
                    if k in self.columns:
                        _scenario[k] = v
                except (ValueError, TypeError) as err: # noqa
                    pass
                except Exception as err:  # noqa
                    pass
        for item in soup.findAll('a'):
            try:
                href = item['href']
            except KeyError:
                href = ''
            if href.startswith('?'):
                continue
            href_ext = os.path.splitext(href)[-1][1:].upper()
            if href_ext == '':
                continue
            if href_ext in self.columns:
                _scenario[href_ext] = href
        pass

    async def fetch(self, url):
        with async_timeout.timeout(__ASYNC_TIMEOUT__):
            print('[+] fetch({})'.format(url))
            async with self.session.get(url) as response:
                return await response.text()

    async def fetch_scenario(self, url):
        async with self.sem:
            print('[+] fetch_scenario({})'.format(url))
            raw_html = await self.fetch(url)
        return raw_html

    async def fetch_main(self, group, scenarios):
        async with aiohttp.ClientSession(
                loop=self.loop,
                connector=aiohttp.TCPConnector(verify_ssl=False)) as self.session:  # noqa
            await asyncio.wait([
                self.record_scenario_metadata(group, s) for s in scenarios
            ])

    def cache_expired(self, cache_timeout=__CACHE_TIMEOUT__):
        """
        Returns True if cache_file is expired or does not exist.
        Returns False if file exists and is not expired.
        """

        cache_expired = True
        now = time.time()
        try:
            stat_results = os.stat(self.cache_file)
            if stat_results.st_size == 0:
                logger.debug('[!] found empty cache')
                self.delete_cache()
            age = now - stat_results.st_mtime
            if age <= cache_timeout:
                logger.debug('[!] cache {} '.format(self.cache_file) +
                             'has not yet expired')
                cache_expired = False
        except FileNotFoundError as err:  # noqa
            logger.debug('[!] cache {} '.format(self.cache_file) +
                         'not found')
        return cache_expired

    def read_cache(self):
        """
        Load cached data (if any). Returns True if read
        was successful, otherwise False.
        """

        _cache = dict()
        if not self.cache_expired():
            with open(self.cache_file, 'r') as infile:
                _cache = json.load(infile)
            self.scenarios = _cache['scenarios']
            self.columns = _cache['columns']
            logger.debug('[!] loaded metadata from cache: ' +
                         '{}'.format(self.cache_file))
            return True
        return False

    def write_cache(self):
        """Save metadata to local cache as JSON"""

        _cache = dict()
        _cache['scenarios'] = self.scenarios
        _cache['columns'] = self.columns
        with open(self.cache_file, 'w') as outfile:
            json.dump(_cache, outfile)
        logger.debug('[!] wrote new cache file ' +
                     '{}'.format(self.cache_file))
        return True

    def delete_cache(self):
        """Delete cache file"""

        os.remove(self.cache_file)
        logger.debug('[!] deleted cache file {}'.format(self.cache_file))
        return True

    def get_scenarios(self, group, url=__DATASETS_URL__):
        """Scrape CTU web site for metadata about binetflow
        files that are available."""

        # See "verify=False" comment in download_netflow() function.
        # See also: https://stackoverflow.com/questions/15445981/how-do-i-disable-the-security-certificate-check-in-python-requests  # noqa
        # requests.packages.urllib3.disable_warnings(
        #     category=InsecureRequestWarning)

        logger.info('[+] identifying scenarios for group {} from {}'.format(group, url))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            requests.packages.urllib3.disable_warnings(
                category=InsecureRequestWarning)
            response = requests.get(url, verify=False)  # nosec
        soup = BeautifulSoup(response.text, 'html.parser')
        scenarios = []
        for item in soup.findAll('a'):
            try:
                href = item['href']
            except KeyError:
                href = ''
            if href.startswith('?'):
                continue
            if '/publicDatasets/' in href and href.endswith('/'):
                logger.debug('[+] found scenario {}'.format(href))
                scenarios.append(href)
        return scenarios

    def get_metadata(self, groups=None, name_includes=None, has_hash=None):
        """
        Return a list of lists of data suitable for use by
        cliff, following the order of elements in self.columns.
        """
        data = list()
        for (scenario, attributes) in self.scenarios.items():
            if '_SUCCESS' in attributes and not attributes['_SUCCESS']:
                continue
            if 'GROUP' in attributes and attributes['GROUP'] not in groups:
                continue
            if name_includes is not None:
                # Can't look for something that doesn't exist.
                if 'PROBABLE_NAME' not in attributes:
                    continue
                probable_name = attributes['PROBABLE_NAME'].lower()
                find = probable_name.find(name_includes.lower())
                if find == -1:
                    continue
            row = dict()
            row['SCENARIO'] = scenario
            row['SCENARIO_URL'] = attributes['URL']
            if has_hash is not None:
                if not (has_hash == attributes['MD5'] or
                        has_hash == attributes['SHA1'] or
                        has_hash == attributes['SHA256']):
                    continue
            # Get remaining attributes
            for c in self.columns:
                if c not in row:
                    row[c] = attributes.get(c)
            data.append([row.get(c) for c in self.columns])
        return data


class CTUGet(Command):
    """Get CTU dataset."""

    log = logging.getLogger(__name__)

    def get_epilog(self):
        return textwrap.dedent("""\
            For testing purposes, use --maxlines to limit the number of
            lines to read from each file.
            """)

    def get_parser(self, prog_name):
        parser = super(CTUGet, self).get_parser(prog_name)
        parser.add_argument(
            '--force',
            action='store_true',
            dest='force',
            default=False,
            help="Force over-writing files if they exist (default: False)."
        )
        _default_protocols = ",".join(DEFAULT_PROTOCOLS)
        parser.add_argument(
            '-P', '--protocols',
            metavar='<protocol-list>',
            dest='protocols',
            type=lambda s: [i for i in s.split(',')],
            default=_default_protocols,
            help='Protocols to include, or "any" ' +
                 '(default: {})'.format(_default_protocols)
        )
        parser.add_argument(
            '-L', '--maxlines',
            metavar='<lines>',
            dest='maxlines',
            default=None,
            help="Maximum number of lines to get (default: None)"
        )
        parser.add_argument(
            '--ignore-cache',
            action='store_true',
            dest='ignore_cache',
            default=False,
            help="Ignore any cached results (default: False)."
        )
        parser.add_argument('scenario', nargs='*', default=[])
        return parser

    def take_action(self, parsed_args):
        self.log.debug('[!] getting CTU data')
        if 'ctu_metadata' not in dir(self):
            self.ctu_metadata = CTU_Dataset(
                ignore_cache=parsed_args.ignore_cache,
                debug=self.app_args.debug)
        self.ctu_metadata.load_ctu_metadata()

        if not os.path.exists(self.app_args.data_dir):
            os.mkdir(self.app_args.data_dir, 0o750)
        datatype = self.cmd_name.split()[-1]
        if len(parsed_args.scenario) == 0:
            raise RuntimeError(('must specify a scenario: '
                                'try "lim ctu list netflow"'))
        self.log.debug('[!] downloading ctu {} data'.format(datatype))

        for scenario in parsed_args.scenario:
            if datatype == 'netflow':
                download_ctu_netflow(
                    url=self.ctu_metadata.get_netflow_url(scenario),
                    datadir=self.app_args.data_dir,
                    protocols=parsed_args.protocols,
                    maxlines=parsed_args.maxlines,
                    force=parsed_args.force)
            else:
                raise RuntimeError('getting "{}" '.format(datatype) +
                                   'not implemented')


class CTUList(Lister):
    """List CTU dataset metadata."""

    log = logging.getLogger(__name__)

    def get_epilog(self):
        return textwrap.dedent("""\
            """)

    def get_parser(self, prog_name):
        parser = super(CTUList, self).get_parser(prog_name)
        parser.add_argument(
            '--ignore-cache',
            action='store_true',
            dest='ignore_cache',
            default=False,
            help="Ignore any cached results (default: False)."
        )
        parser.add_argument(
            '--group',
            action='append',
            dest='groups',
            type=str,
            choices=__GROUPS__ + ['all'],
            default=['ctu13'],
            help="Group to process or 'all' (default: 'ctu13')."
        )
        find = parser.add_mutually_exclusive_group(required=False)
        find.add_argument(
            '--hash',
            dest='hash',
            metavar='<{md5|sha1|sha256} hash>',
            default=None,
            help=('Only list scenarios that involve a '
                  'specific hash (default: None).')
        )
        find.add_argument(
            '--name-includes',
            dest='name_includes',
            metavar='<string>',
            default=None,
            help=('Only list "PROBABLE_NAME" including this '
                  'string (default: None).')
        )
        return parser

    # FYI, https://mcfp.felk.cvut.cz/publicDatasets/CTU-Malware-Capture-Botnet-269-1/README.html  # noqa
    # is an Emotet sample...
    # TODO(dittrich): Figure out how to handle these

    def take_action(self, parsed_args):
        self.log.debug('[!] listing CTU data')
        if 'all' in parsed_args.groups:
            parsed_args.groups = __GROUPS__
        if 'ctu_metadata' not in dir(self):
            self.ctu_metadata = CTU_Dataset(
                ignore_cache=parsed_args.ignore_cache,
                groups=parsed_args.groups,
                debug=self.app_args.debug)
        self.ctu_metadata.load_ctu_metadata()

        columns = self.ctu_metadata.columns
        data = self.ctu_metadata.get_metadata(
            name_includes=parsed_args.name_includes,
            groups=parsed_args.groups,
            has_hash=parsed_args.hash)
        return columns, data


# vim: set fileencoding=utf-8 ts=4 sw=4 tw=0 et :
