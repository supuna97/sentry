"""Query use-case module.

For now, this is the search and sort entry-point.  Some of this code may be moved to
replays/query.py when the pre-existing query module is deprecated.

There are two important functions in this module: "search_filter_to_condition" and
"query_using_optimized_search".  "search_filter_to_condition" is responsible for transforming a
SearchFilter into a Condition.  This is the only entry-point into the Field system.

"query_using_optimized_search" is the request processing engine.  It accepts raw data from an
external source, makes decisions around what to query and when, and is responsible for returning
intelligible output for the "post_process" module.  More information on its implementation can be
found in the function.
"""
from __future__ import annotations

from collections import namedtuple
from datetime import datetime, timedelta
from typing import Any, Mapping, Union, cast

from rest_framework.exceptions import ParseError
from snuba_sdk import (
    And,
    Column,
    Condition,
    Direction,
    Entity,
    Function,
    Granularity,
    Op,
    Or,
    OrderBy,
    Query,
    Request,
)
from snuba_sdk.expressions import Expression

from sentry.api.event_search import ParenExpression, SearchFilter, SearchKey, SearchValue
from sentry.models.organization import Organization
from sentry.replays.lib.new_query.errors import CouldNotParseValue, OperatorNotSupported
from sentry.replays.lib.new_query.fields import ColumnField
from sentry.replays.usecases.query.fields import ComputedField, TagField
from sentry.utils.snuba import raw_snql_query


def handle_search_filters(
    search_config: dict[str, Union[ColumnField, ComputedField, TagField]],
    search_filters: list[Union[SearchFilter, str, ParenExpression]],
) -> list[Condition]:
    """Convert search filters to snuba conditions."""
    result: list[Condition] = []
    look_back = None
    for search_filter in search_filters:
        # SearchFilters are transformed into Conditions and appended to the result set.  If they
        # are top level filters they are implicitly AND'ed in the WHERE/HAVING clause.  Otherwise
        # explicit operators are used.
        if isinstance(search_filter, SearchFilter):
            try:
                condition = search_filter_to_condition(search_config, search_filter)
            except OperatorNotSupported:
                raise ParseError(f"Invalid operator specified for `{search_filter.key.name}`")
            except CouldNotParseValue:
                raise ParseError(f"Could not parse value for `{search_filter.key.name}`")

            if look_back == "AND":
                look_back = None
                attempt_compressed_condition(result, condition, And)
            elif look_back == "OR":
                look_back = None
                attempt_compressed_condition(result, condition, Or)
            else:
                result.append(condition)
        # ParenExpressions are recursively computed.  If more than one condition is returned then
        # those conditions are AND'ed.
        elif isinstance(search_filter, ParenExpression):
            conditions = handle_search_filters(search_config, search_filter.children)
            if len(conditions) < 2:
                result.extend(conditions)
            else:
                result.append(And(conditions))
        # String types are limited to AND and OR... I think?  In the case where its not a valid
        # look-back it is implicitly ignored.
        elif isinstance(search_filter, str):
            look_back = search_filter

    return result


def attempt_compressed_condition(
    result: list[Expression],
    condition: Condition,
    condition_type: Union[And, Or],
):
    """Unnecessary query optimization.

    Improves legibility for query debugging. Clickhouse would flatten these nested OR statements
    internally anyway.

    (block OR block) OR block => (block OR block OR block)
    """
    if isinstance(result[-1], condition_type):
        result[-1].conditions.append(condition)
    else:
        result.append(condition_type([result.pop(), condition]))


def search_filter_to_condition(
    search_config: dict[str, Union[ColumnField, ComputedField, TagField]],
    search_filter: SearchFilter,
) -> Condition:
    # The field-name is whatever the API says it is.  We take it at face value.
    field_name = search_filter.key.name

    # If the field-name is in the search config then we can apply the search filter and return a
    # result.  If its not then its a tag and the same operation is performed only with a few more
    # steps.
    field = search_config.get(field_name)
    if isinstance(field, (ColumnField, ComputedField)):
        return field.apply(search_filter)

    if field is None:
        # Tags are represented with an "*" field by convention.  We could name it `tags` and
        # update our search config to point to this field-name.
        field = cast(TagField, search_config["*"])

    # Tags that are namespaced are stripped.
    if field_name.startswith("tags["):
        field_name = field_name[5:-1]

    # The field_name in this case does not represent a column_name but instead it represents a
    # dynamic value in the tags.key array.  For this reason we need to pass it into our "apply"
    # function.
    return field.apply(field_name, search_filter)


# Everything below here will move to replays/query.py once we deprecate the old query behavior.
# Leaving it here for now so this is easier to review/remove.
from sentry.replays.usecases.query.configs.aggregate import search_config as agg_search_config
from sentry.replays.usecases.query.configs.aggregate_sort import sort_config as agg_sort_config
from sentry.replays.usecases.query.configs.scalar import (
    can_scalar_search_subquery,
    scalar_search_config,
)
from sentry.replays.usecases.query.configs.scalar_sort import (
    can_scalar_sort_subquery,
    scalar_sort_config,
)

Paginators = namedtuple("Paginators", ("limit", "offset"))


def query_using_optimized_search(
    fields: list[str],
    search_filters: list[Union[SearchFilter, str, ParenExpression]],
    environments: list[str],
    sort: str | None,
    pagination: Paginators | None,
    organization: Organization | None,
    project_ids: list[int],
    period_start: datetime,
    period_stop: datetime,
):
    tenant_id = _make_tenant_id(organization)

    # Environments is provided to us outside of the ?query= url parameter. It's stil filtered like
    # the values in that parameter so let's shove it inside and process it like any other filter.
    if environments:
        search_filters.append(
            SearchFilter(SearchKey("environment"), "IN", SearchValue(environments))
        )

    can_scalar_sort = can_scalar_sort_subquery(sort or "started_at")
    can_scalar_search, has_varying_condition = can_scalar_search_subquery(search_filters)

    if can_scalar_sort and can_scalar_search:
        query = make_simple_scalar_query(
            search_filters=search_filters,
            sort=sort,
            project_ids=project_ids,
            period_start=period_start,
            period_stop=period_stop,
            has_varying_condition=has_varying_condition,
        )
        referrer = "replays.query.browse_subquery"
    else:
        query = make_simple_aggregation_query(
            search_filters=search_filters,
            sort=sort,
            project_ids=project_ids,
            period_start=period_start,
            period_stop=period_stop,
        )
        referrer = "replays.query.browse_aggregated_subquery"

    if pagination:
        query = query.set_limit(pagination.limit)
        query = query.set_offset(pagination.offset)

    subquery_response = _execute_query(query, tenant_id, referrer)

    # These replay_ids are ordered by the OrderBy expression in the query above.
    replay_ids = [row["replay_id"] for row in subquery_response.get("data", [])]
    if not replay_ids:
        return []

    # The final aggregation step.  Here we pass the replay_ids as the only filter.  In this step
    # we select everything and use as much memory as we need to complete the operation.
    #
    # If this step runs out of memory your pagination size is about 1,000,000 rows too large.
    # That's a joke.  This will complete very quickly at normal pagination sizes.
    results = _execute_query(
        make_full_aggregation_query(
            fields=fields,
            replay_ids=replay_ids,
            project_ids=project_ids,
            period_start=period_start,
            period_end=period_stop,
        ),
        tenant_id,
        referrer="replays.query.browse_query",
    )["data"]

    return _make_ordered(replay_ids, results)


def make_simple_scalar_query(
    search_filters: list[Union[SearchFilter, str, ParenExpression]],
    sort: str | None,
    project_ids: list[int],
    period_start: datetime,
    period_stop: datetime,
    has_varying_condition: bool,
) -> Query:
    orderby = _sort_to_orderby(scalar_sort_config, sort)

    if has_varying_condition:
        group_by = [Column("replay_id")]
        where = handle_search_filters(scalar_search_config, search_filters)

        # Because we're grouping we have to wrap our ordering key in an aggregate condition.
        orderby = [OrderBy(Function("any", parameters=[orderby[0].exp]), orderby[0].direction)]
    else:
        group_by = []
        where = handle_search_filters(scalar_search_config, search_filters)

        # Because we're not grouping we have to filter by segment_id to remove duplicate
        # replay_ids.
        #
        # NOTE: This could still return duplicates if the segment_id has not been de-duplicated by
        # ClickHouse. This is current production behavior and we have not received any reports.
        where.append(Condition(Column("segment_id"), Op.EQ, 0))

    return Query(
        match=Entity("replays"),
        select=[Column("replay_id")],
        where=[
            Condition(Column("project_id"), Op.IN, project_ids),
            Condition(Column("timestamp"), Op.LT, period_stop),
            Condition(Column("timestamp"), Op.GTE, period_start),
            *where,
        ],
        orderby=orderby,
        groupby=group_by,
        granularity=Granularity(3600),
    )


def make_simple_aggregation_query(
    search_filters: list[Union[SearchFilter, str, ParenExpression]],
    sort: str | None,
    project_ids: list[int],
    period_start: datetime,
    period_stop: datetime,
) -> Query:
    having: list[Condition] = handle_search_filters(agg_search_config, search_filters)
    orderby = _sort_to_orderby(agg_sort_config, sort)

    return Query(
        match=Entity("replays"),
        select=[Column("replay_id")],
        where=[
            Condition(Column("project_id"), Op.IN, project_ids),
            Condition(Column("timestamp"), Op.LT, period_stop),
            Condition(Column("timestamp"), Op.GTE, period_start),
        ],
        having=having,
        orderby=orderby,
        groupby=[Column("replay_id")],
        granularity=Granularity(3600),
    )


def make_full_aggregation_query(
    fields: list[str],
    replay_ids: list[str],
    project_ids: list[int],
    period_start: datetime,
    period_end: datetime,
) -> Query:
    """Return a query to fetch every replay in the set."""
    from sentry.replays.query import QUERY_ALIAS_COLUMN_MAP, select_from_fields

    def _select_from_fields() -> list[Union[Column, Function]]:
        if fields:
            return select_from_fields(list(set(fields)))
        else:
            return list(QUERY_ALIAS_COLUMN_MAP.values())

    return Query(
        match=Entity("replays"),
        select=_select_from_fields(),
        where=[
            Condition(Column("project_id"), Op.IN, project_ids),
            # Replay-ids were pre-calculated so no having clause and no aggregating significant
            # amounts of data.
            Condition(Column("replay_id"), Op.IN, replay_ids),
            # We can scan an extended time range to account for replays which span either end of
            # the range.  These timestamps are an optimization and could be removed with minimal
            # performance impact.  It's a point query.  Its super fast.
            Condition(Column("timestamp"), Op.GTE, period_start - timedelta(hours=1)),
            Condition(Column("timestamp"), Op.LT, period_end + timedelta(hours=1)),
        ],
        groupby=[Column("project_id"), Column("replay_id")],
        granularity=Granularity(3600),
    )


def _execute_query(query: Query, tenant_id: dict[str, int], referrer: str) -> Mapping[str, Any]:
    return raw_snql_query(
        Request(
            dataset="replays",
            app_id="replay-backend-web",
            query=query,
            tenant_ids=tenant_id,
        ),
        referrer,
    )


def _sort_to_orderby(config, sort: str | None) -> list[OrderBy]:
    if sort is None:
        return [OrderBy(_get_sort_column(config, "started_at"), Direction.DESC)]
    elif sort.startswith("-"):
        return [OrderBy(_get_sort_column(config, sort[1:]), Direction.DESC)]
    else:
        return [OrderBy(_get_sort_column(config, sort), Direction.ASC)]


def _get_sort_column(config, column_name: str) -> Function:
    try:
        return config[column_name]
    except KeyError:
        raise ParseError(f"The field `{column_name}` is not a sortable field.")


def _make_tenant_id(organization: Organization | None) -> dict[str, int]:
    if organization is None:
        return {}
    else:
        return {"organization_id": organization.id}


def _make_ordered(replay_ids: list[str], results: Any) -> list[Any]:
    if not replay_ids:
        return []
    elif not results:
        return []

    replay_id_to_index = {}
    for i, replay_id in enumerate(replay_ids):
        if replay_id not in replay_id_to_index:
            replay_id_to_index[replay_id] = i

    ordered_results = [None] * len(replay_id_to_index)
    for result in results:
        index = replay_id_to_index[result["replay_id"]]
        ordered_results[index] = result

    return list(filter(None, ordered_results))