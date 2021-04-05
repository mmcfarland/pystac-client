import itertools as it
import json
import logging
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime as datetime_
from typing import Callable, Iterator, List, Optional, Tuple, Union
from urllib.error import HTTPError

import pystac
import pytz
from requests import Request, Session

from pystac_api.exceptions import APIError
from pystac_api.item_collection import ItemCollection
from pystac_api.stac_io import make_request, get_pages, simple_stac_resolver
from pystac_api.stac_api_object import STACAPIObjectMixin

DatetimeOrTimestamp = Optional[Union[datetime_, str]]
Datetime = Union[Tuple[str], Tuple[str, str]]
DatetimeLike = Union[
    DatetimeOrTimestamp,
    Tuple[DatetimeOrTimestamp, DatetimeOrTimestamp],
    List[DatetimeOrTimestamp],
    Iterator[DatetimeOrTimestamp]
]

BBox = Tuple[float, ...]
BBoxLike = Union[BBox, List[float], Iterator[float], str]

Collections = Tuple[str, ...]
CollectionsLike = Union[
    List[Union[str, pystac.Collection]],
    Iterator[Union[str, pystac.Collection]],
    str,
    pystac.Collection
]

IDs = Tuple[str, ...]
IDsLike = Union[IDs, str, List[str], Iterator[str]]

Intersects = dict
IntersectsLike = Union[str, Intersects, object]


logger = logging.getLogger(__name__)


class ItemSearch(STACAPIObjectMixin):
    """Represents a deferred query to an Item Search endpoint as described in the `STAC API - Item Search spec
    <https://github.com/radiantearth/stac-api-spec/tree/master/item-search>`__. No request is sent to the API until
    either the :meth:`ItemSearch.item_collections` or :meth:`ItemSearch.items` method is called and iterated over.

    If ``intersects`` is included in the search parameters, then the instance will first try to make a ``POST`` request.
    If server responds with a ``405 - Method Not Allowed`` status code, then the instance will fall back to using
    ``GET`` requests for all subsequent requests.

    All "Parameters", with the exception of ``max_items``, ``method``, and ``url`` correspond to query parameters
    described in the `STAC API - Item Search: Query Parameters Table
    <https://github.com/radiantearth/stac-api-spec/tree/master/item-search#query-parameter-table>`__ docs. Please refer
    to those docs for details on how these parameters filter search results.

    "Other Parameters" are other keyword arguments specific to this library's implementation and do not correspond to
    concepts in the STAC API spec.

    Parameters
    ----------
    url : str
        The URL to the item-search endpoint
    method : str or None, optional
        The HTTP method to use when making a request to the service. This must be either ``"GET"``, ``"POST"``, or
        ``None``. If ``None``, this will default to ``"POST"`` if the ``intersects`` argument is present and ``"GET"``
        if not. If a ``"POST"`` request receives a ``405`` status for the response, it will automatically retry with a
        ``"GET"`` request for all subsequent requests.
    max_items : int or None, optional
        The maximum number of items to return from the search. *Note that this is not a STAC API - Item Search parameter
        and is instead used by the client to limit the total number of returned items*.
    limit : int, optional
        The maximum number of items to return *per page*. Defaults to ``None``, which falls back to the limit set by the
        service.
    bbox: list or tuple or Iterator or str, optional
        May be a list, tuple, or iterator representing a bounding box of 2D or 3D coordinates. Results will be filtered
        to only those intersecting the bounding box.
    datetime: str or datetime.datetime or list or tuple or Iterator, optional
        Either a single datetime or datetime range used to filter results. You may express a single datetime using
        a :class:`datetime.datetime` instance or a `RFC 3339-compliant <https://tools.ietf.org/html/rfc3339>`__
        timestamp. Instances of :class:`datetime.datetime` may be either timezone aware or unaware. Timezone aware
        instances will be converted to a UTC timestamp before being passed to the endpoint. Timezone unaware instances
        are assumed to represent UTC timestamps.
        You may represent a datetime range using a ``"/"`` separated string as described in the spec, or a list, tuple,
        or iterator of 2 timestamps or datetime instances. For open-ended ranges, use either ``".."``
        (``'2020-01-01:00:00:00Z/..'``, ``['2020-01-01:00:00:00Z', '..']``) or a value of ``None``
        (``['2020-01-01:00:00:00Z', None]``).
    intersects: str or dict, optional
        A GeoJSON-like dictionary or JSON string. Results will be filtered to only those intersecting the geometry
    ids: list, optional
        List of Item ids to return. All other filter parameters that further restrict the number of search results
        (except ``limit``) are ignored.
    collections: list, optional
        List of one or more Collection IDs or :class:`pystac.Collection` instances. Only Items in one of the provided
        Collections will be searched

    Other Parameters
    ----------------
    next_resolver : Callable, optional
        A callable that will be used to construct the next request based on a "next" link and the previous request.
        Defaults to using the :func:`~pystac_api.stac_io.simple_stac_resolver`.
    conformance : list, optional
        A list of conformance URIs indicating the specs that this service conforms to. Note that these are *not*
        published as part of the ``"search"`` endpoint and must be obtained from the service's landing page.
    """
    def __init__(
        self,
        url: str,
        *,
        limit: Optional[int] = None,
        bbox: Optional[BBoxLike] = None,
        datetime: Optional[DatetimeLike] = None,
        intersects: Optional[IntersectsLike] = None,
        ids: Optional[IDsLike] = None,
        collections: Optional[CollectionsLike] = None,

        max_items: Optional[int] = None,
        method: Optional[str] = 'POST',
        headers: Optional[dict] = {},
        conformance: List[str] = [],
        next_resolver: Callable = None,
    ):
        self.conformance = conformance
        self.session = Session()
        self.session.headers.update(headers)
        self.request = Request(
            method = method,
            url = url
        )

        self._next_resolver = next_resolver or simple_stac_resolver
        self._max_items = max_items

        params = {
            'limit': int(limit) if limit is not None else None,
            'bbox': self._format_bbox(bbox),
            'datetime': self._format_datetime(datetime),
            'ids': self._format_ids(ids),
            'collections': self._format_collections(collections),
            'intersects': self._format_intersects(intersects)
        }
        self._search_parameters = {k: v for k, v in params.items() if v is not None}

        if method == 'POST':
            self.request.json = self._search_parameters
        else:
            self.request.params = self._search_parameters

    @property
    def url(self):
        """The base URL to which search requests will be made. This may include query string parameters, but any
        parameters that overlap with initialization arguments will be overwritten."""
        return str(self.request.url)

    @property
    def method(self):
        """The HTTP method/verb that will be used when making requests."""
        return str(self.request.method)

    @staticmethod
    def _format_bbox(value: Optional[BBoxLike]) -> Optional[BBox]:
        if value is None:
            return None

        if isinstance(value, str):
            bbox = tuple(map(float, value.split(',')))
        else:
            bbox = tuple(map(float, value))

        return bbox

    @staticmethod
    def _format_datetime(value: Optional[DatetimeLike]) -> Optional[Datetime]:
        def _format(dt):
            if dt is None:
                return '..'
            if isinstance(dt, str):
                return dt

            if dt.tzinfo is not None:
                dt = dt.astimezone(pytz.UTC)
                dt = dt.replace(tzinfo=None)

            return dt.isoformat('T') + 'Z'

        if value is None:
            return None
        if isinstance(value, str):
            return '/'.join((map(_format, value.split('/'))))
        if isinstance(value, Iterable):
            return '/'.join((map(_format, value)))

        return _format(value),

    @staticmethod
    def _format_collections(value: Optional[CollectionsLike]) -> Optional[Collections]:
        def _format(c):
            if isinstance(c, str):
                return c
            if isinstance(c, Iterable):
                return tuple(map(_format, c))

            return c.id

        if value is None:
            return None
        if isinstance(value, str):
            return tuple(map(_format, value.split(',')))
        if isinstance(value, pystac.Collection):
            return _format(value),

        return _format(value)

    @staticmethod
    def _format_ids(value: Optional[IDsLike]) -> Optional[IDs]:
        if value is None:
            return None

        if isinstance(value, str):
            return tuple(value.split(','))

        return tuple(value)

    @staticmethod
    def _format_intersects(value: Optional[IntersectsLike]) -> Optional[Intersects]:
        if value is None:
            return None
        if isinstance(value, str):
            return json.loads(value)
        return deepcopy(getattr(value, '__geo_interface__', value))

    def matched(self) -> int:
        resp = make_request(self.session, self.request, {"limit": 0})
        found = resp.get('numberMatched')
        if found is None:
            raise APIError('Unexpected response')
        return resp['numberMatched']

    def item_collections(self) -> Iterator[ItemCollection]:
        """Iterator that yields dictionaries matching the `ItemCollection
        <https://github.com/radiantearth/stac-api-spec/blob/master/fragments/itemcollection/README.md>`__ spec. Each of
        these items represents a "page" or results for the search.

        Yields
        -------
        item_collection : pystac_api.ItemCollection
        """
        request = deepcopy(self.request)

        for page in get_pages(
            session=self.session,
            request=request,
            next_resolver=self._next_resolver
        ):
            yield ItemCollection.from_dict(page, conformance=self.conformance)

    def items(self) -> Iterator[pystac.Item]:
        """Iterator that yields :class:`pystac.Item` instances for each item matching the given search parameters. Calls
        :meth:`ItemSearch.item_collections()` internally and yields from
        :attr:`ItemCollection.features <pystac_api.ItemCollection.features>` for each page of results.

        Yields
        ------
        item : pystac.Item
        """
        def _paginate():
            for item_collection in self.item_collections():
                yield from item_collection.features

        try:
            yield from it.islice(_paginate(), self._max_items)
        except HTTPError as e:
            if e.code == 405:
                self._method = 'GET'
                yield from it.islice(_paginate(), self._max_items)
            else:
                raise
