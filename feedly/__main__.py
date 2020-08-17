# MIT License
#
# Copyright (c) 2020 Tony Wu <tony[dot]wu(at)nyu[dot]edu>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from functools import reduce

import click
import simplejson as json

from .items import HyperlinkStore


def load_json(path):
    with open(path, 'r') as f:
        index = json.load(f)
    store = HyperlinkStore(index['resources'])
    return store


@click.group()
def cli():
    pass


def _kvp_to_dict(s):
    return dict([f.split('=', 1) for f in s.split(',')])


@cli.command()
@click.option('-i', '--include', default=None, multiple=True, help='include URLs matching this filter.', metavar='[filter]')
@click.option('-x', '--exclude', default=None, multiple=True, help='exclude URLs matching this filter.', metavar='[filter]')
@click.argument('crawl_data', type=click.Path(exists=True))
def collect_urls(crawl_data, include, exclude):
    """Collect all external links in a crawled project and print them as a newline-separated list.

    CRAWL_DATA is the path to the JSON file of an existing crawl.

    \b
    Filtering
    ---------

    You can use the `--include` and `--exclude` options to specify which URLs to print.

    Each `--include` or `--exclude` takes a filter, which is either a single criterion or a comma-separated list
    of multiple criteria. Each criterion is written as:

        topic=value

    Each filter matches URLs that satisfy _all_ of the filter's criteria. You may use multiple `--include`s and `--exclude`s;
    the resulting URL list is generated by finding all URLs that match _any_ `--include` filter and then subtracting it with
    all URLs that match _any_ `--exclude` filter.

    There should not be any spaces around the commas; if your filter values contain spaces, you should quote the filter.

    \b
    Currently supported topics are:
        - source (webpages on which the URL is found)
        - domain (domain name of the URL)
        - tag (HTML tags from which the URL is extracted)
        - id (`id`s of the HTML tags, if any)
        - class (HTML classes of the HTML tags)

    The default filters (when no --include or --exclude is specified) are

    --include tag=img --include tag=source

    Examples
    --------

    \b
    --include tag=a
        include all URLs found on <a> (HTML anchor) tags
    \b
    --include tag=a,class=citation
        include all URLs found on <a> tags whose class list contains `citation`
    \b
    --include tag=source --include tag=img --exclude class=hidden
        include all URLs found on <source> tags or <img> tags but exclude those whose class list contains `hidden`
    """
    store: HyperlinkStore = load_json(crawl_data)

    if not include:
        include = ()
    if not exclude:
        exclude = ()
    if not include and not exclude:
        include = ['tag=img', 'tag=source']

    included = reduce(lambda x, y: x | set(store.get_all(**_kvp_to_dict(y))), include, set())
    excluded = reduce(lambda x, y: x | set(store.get_all(**_kvp_to_dict(y))), exclude, set())

    print('\n'.join(included - excluded))


@cli.command()
@click.argument('crawl_data', type=click.Path(exists=True))
def collect_keywords(crawl_data):
    store: HyperlinkStore = load_json(crawl_data)
    items = store.get_items()
    print('\n'.join(sorted(reduce(lambda x, y: x | y, [item.get('feedly_keywords', set()) for item in items]))))


if __name__ == '__main__':
    cli()
