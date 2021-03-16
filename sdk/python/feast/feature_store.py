# Copyright 2019 The Feast Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, Union

import pandas as pd
import pyarrow

from feast.data_source import FileSource
from feast.entity import Entity
from feast.feature_view import FeatureView
from feast.infra.provider import Provider, get_provider
from feast.offline_store import (
    OfflineStore,
    RetrievalJob,
    get_offline_store,
    get_offline_store_for_retrieval,
)
from feast.registry import Registry
from feast.repo_config import (
    LocalOnlineStoreConfig,
    OnlineStoreConfig,
    RepoConfig,
    load_repo_config,
)
from feast.types.EntityKey_pb2 import EntityKey as EntityKeyProto
from feast.types.Value_pb2 import Value as ValueProto


class FeatureStore:
    """
    A FeatureStore object is used to define, create, and retrieve features.
    """

    config: RepoConfig

    def __init__(
        self, repo_path: Optional[str] = None, config: Optional[RepoConfig] = None,
    ):
        if repo_path is not None and config is not None:
            raise ValueError("You cannot specify both repo_path and config")
        if config is not None:
            self.config = config
        elif repo_path is not None:
            self.config = load_repo_config(Path(repo_path))
        else:
            self.config = RepoConfig(
                metadata_store="./metadata.db",
                project="default",
                provider="local",
                online_store=OnlineStoreConfig(
                    local=LocalOnlineStoreConfig("online_store.db")
                ),
            )

    def _get_provider(self) -> Provider:
        return get_provider(self.config)

    def _get_offline_store(self) -> Type[OfflineStore]:
        return get_offline_store(self.config)

    def _get_registry(self) -> Registry:
        return Registry(self.config.metadata_store)

    def apply(self, objects: List[Union[FeatureView, Entity]]):
        """Register objects to metadata store and update related infrastructure.

        The apply method registers one or more definitions (e.g., Entity, FeatureView) and registers or updates these
        objects in the Feast registry. Once the registry has been updated, the apply method will update related
        infrastructure (e.g., create tables in an online store) in order to reflect these new definitions. All
        operations are idempotent, meaning they can safely be rerun.

        Args: objects (List[Union[FeatureView, Entity]]): A list of FeatureView or Entity objects that should be
            registered

        Examples:
            Register a single Entity and FeatureView.
            >>> from feast.feature_store import FeatureStore
            >>> from feast import Entity, FeatureView, Feature, ValueType, FileSource
            >>> from datetime import timedelta
            >>>
            >>> fs = FeatureStore()
            >>> customer_entity = Entity(name="customer", value_type=ValueType.INT64, description="customer entity")
            >>> customer_feature_view = FeatureView(
            >>>     name="customer_fv",
            >>>     entities=["customer"],
            >>>     features=[Feature(name="age", dtype=ValueType.INT64)],
            >>>     input=FileSource(path="file.parquet", event_timestamp_column="timestamp"),
            >>>     ttl=timedelta(days=1)
            >>> )
            >>> fs.apply([customer_entity, customer_feature_view])
        """

        # TODO: Add locking
        # TODO: Optimize by only making a single call (read/write)
        # TODO: Add infra update operation (currently we are just writing to registry)
        registry = self._get_registry()
        for ob in objects:
            if isinstance(ob, FeatureView):
                registry.apply_feature_view(ob, project=self.config.project)
            elif isinstance(ob, Entity):
                registry.apply_entity(ob, project=self.config.project)
            else:
                raise ValueError(
                    f"Unknown object type ({type(ob)}) provided as part of apply() call"
                )

    def get_historical_features(
        self, entity_df: Union[pd.DataFrame, str], feature_refs: List[str],
    ) -> RetrievalJob:
        """Enrich an entity dataframe with historical feature values for either training or batch scoring.

        This method joins historical feature data from one or more feature views to an entity dataframe by using a time
        travel join.

        Each feature view is joined to the entity dataframe using all entities configured for the respective feature
        view. All configured entities must be available in the entity dataframe. Therefore, the entity dataframe must
        contain all entities found in all feature views, but the individual feature views can have different entities.

        Time travel is based on the configured TTL for each feature view. A shorter TTL will limit the
        amount of scanning that will be done in order to find feature data for a specific entity key. Setting a short
        TTL may result in null values being returned.

        Args:
            entity_df (Union[pd.DataFrame, str]): An entity dataframe is a collection of rows containing all entity
                columns (e.g., customer_id, driver_id) on which features need to be joined, as well as a event_timestamp
                column used to ensure point-in-time correctness. Either a Pandas DataFrame can be provided or a string
                SQL query. The query must be of a format supported by the configured offline store (e.g., BigQuery)
            feature_refs: A list of features that should be retrieved from the offline store. Feature references are of
                the format "feature_view:feature", e.g., "customer_fv:daily_transactions".

        Returns:
            RetrievalJob which can be used to materialize the results.

        Examples:
            Retrieve historical features using a BigQuery SQL entity dataframe
            >>> from feast.feature_store import FeatureStore
            >>>
            >>> fs = FeatureStore(config=RepoConfig(provider="gcp"))
            >>> retrieval_job = fs.get_historical_features(
            >>>     entity_df="SELECT event_timestamp, order_id, customer_id from gcp_project.my_ds.customer_orders",
            >>>     feature_refs=["customer:age", "customer:avg_orders_1d", "customer:avg_orders_7d"]
            >>> )
            >>> feature_data = job.to_df()
            >>> model.fit(feature_data) # insert your modeling framework here.
        """

        registry = self._get_registry()
        all_feature_views = registry.list_feature_views(project=self.config.project)
        feature_views = _get_requested_feature_views(feature_refs, all_feature_views)
        offline_store = get_offline_store_for_retrieval(feature_views)
        job = offline_store.get_historical_features(
            self.config, feature_views, feature_refs, entity_df
        )
        return job

    @property
    def project(self) -> str:
        return "default"

    def materialize(
        self, feature_views: List[str], start_date: datetime, end_date: datetime
    ):
        full_feature_views = []
        registry = self._get_registry()
        for name in feature_views:
            feature_view = registry.get_feature_view(name, self.project)
            full_feature_views.append(feature_view)

        # TODO paging large loads
        for feature_view in full_feature_views:
            if isinstance(feature_view.input, FileSource):
                raise NotImplementedError(
                    "This function is not yet implemented for File data sources"
                )
            if feature_view.input.table_ref is None:
                raise NotImplementedError(
                    f"This function is only implemented for FeatureViews with a table_ref; {feature_view.name} does not have one."
                )
            (
                entity_names,
                feature_names,
                event_timestamp_column,
                created_timestamp_column,
            ) = _run_reverse_field_mapping(feature_view)

            offline_store = self._get_offline_store()
            table = offline_store.pull_latest_from_table(
                feature_view.input.table_ref,
                entity_names,
                feature_names,
                event_timestamp_column,
                created_timestamp_column,
                start_date,
                end_date,
            )

            if feature_view.input.field_mapping is not None:
                table = _run_forward_field_mapping(
                    table, feature_view.input.field_mapping
                )

            rows_to_write = _convert_arrow_to_proto(table, feature_view)

            provider = self._get_provider()
            provider.online_write_batch(self.project, feature_view, rows_to_write)


def _get_requested_feature_views(
    feature_refs: List[str], all_feature_views: List[FeatureView]
) -> List[FeatureView]:
    """Get list of feature views based on feature references"""

    feature_views_dict = {}
    for ref in feature_refs:
        ref_parts = ref.split(":")
        found = False
        for feature_view in all_feature_views:
            if feature_view.name == ref_parts[0]:
                found = True
                feature_views_dict[feature_view.name] = feature_view
                continue

        if not found:
            raise ValueError(f"Could not find feature view from reference {ref}")
    feature_views_list = []
    for view in feature_views_dict.values():
        feature_views_list.append(view)

    return feature_views_list


def _run_reverse_field_mapping(
    feature_view: FeatureView,
) -> Tuple[List[str], List[str], str, Optional[str]]:
    """
    If a field mapping exists, run it in reverse on the entity names, feature names, event timestamp column, and created timestamp column to get the names of the relevant columns in the BigQuery table.

    Args:
        feature_view: FeatureView object containing the field mapping as well as the names to reverse-map.
    Returns:
        Tuple containing the list of reverse-mapped entity names, reverse-mapped feature names, reverse-mapped event timestamp column, and reverse-mapped created timestamp column that will be passed into the query to the offline store.
    """
    # if we have mapped fields, use the original field names in the call to the offline store
    event_timestamp_column = feature_view.input.event_timestamp_column
    entity_names = [entity for entity in feature_view.entities]
    feature_names = [feature.name for feature in feature_view.features]
    created_timestamp_column = feature_view.input.created_timestamp_column
    if feature_view.input.field_mapping is not None:
        reverse_field_mapping = {
            v: k for k, v in feature_view.input.field_mapping.items()
        }
        event_timestamp_column = (
            reverse_field_mapping[event_timestamp_column]
            if event_timestamp_column in reverse_field_mapping.keys()
            else event_timestamp_column
        )
        created_timestamp_column = (
            reverse_field_mapping[created_timestamp_column]
            if created_timestamp_column is not None
            and created_timestamp_column in reverse_field_mapping.keys()
            else created_timestamp_column
        )
        entity_names = [
            reverse_field_mapping[col] if col in reverse_field_mapping.keys() else col
            for col in entity_names
        ]
        feature_names = [
            reverse_field_mapping[col] if col in reverse_field_mapping.keys() else col
            for col in feature_names
        ]
    return (
        entity_names,
        feature_names,
        event_timestamp_column,
        created_timestamp_column,
    )


def _run_forward_field_mapping(
    table: pyarrow.Table, field_mapping: Dict[str, str],
) -> pyarrow.Table:
    # run field mapping in the forward direction
    cols = table.column_names
    mapped_cols = [
        field_mapping[col] if col in field_mapping.keys() else col for col in cols
    ]
    table = table.rename_columns(mapped_cols)
    return table


def _convert_arrow_to_proto(
    table: pyarrow.Table, feature_view: FeatureView
) -> List[Tuple[EntityKeyProto, Dict[str, ValueProto], datetime, Optional[datetime]]]:
    rows_to_write = []
    for row in zip(*table.to_pydict().values()):
        entity_key = EntityKeyProto()
        for entity_name in feature_view.entities:
            entity_key.entity_names.append(entity_name)
            idx = table.column_names.index(entity_name)
            value = _convert_to_proto(row[idx])
            entity_key.entity_values.append(value)
        feature_dict = {}
        for feature in feature_view.features:
            idx = table.column_names.index(feature.name)
            value = _convert_to_proto(row[idx])
            feature_dict[feature.name] = value
        event_timestamp_idx = table.column_names.index(
            feature_view.input.event_timestamp_column
        )
        event_timestamp = row[event_timestamp_idx]
        if feature_view.input.created_timestamp_column is not None:
            created_timestamp_idx = table.column_names.index(
                feature_view.input.created_timestamp_column
            )
            created_timestamp = row[created_timestamp_idx]
        else:
            created_timestamp = None

        rows_to_write.append(
            (entity_key, feature_dict, event_timestamp, created_timestamp)
        )
    return rows_to_write


def _convert_to_proto(value: Any) -> ValueProto:
    value_proto = ValueProto()
    if isinstance(value, str):
        value_proto.string_val = value
    elif isinstance(value, bool):
        value_proto.bool_val = value
    elif isinstance(value, int):
        value_proto.int32_val = value
    elif isinstance(value, float):
        value_proto.double_val = value
    elif isinstance(value, bytes):
        value_proto.bytes_val = value
    else:
        raise ValueError(f"Cannot convert value {value} of type {type(value)}.")
    return value_proto
