from enum import Enum
import datetime
import json
import re
import sys
import requests
from typing import Optional, Sequence, Union, List

from rspace_client.client_base import ClientBase, Pagination
from rspace_client.inv import quantity_unit as qu


class DeletedItemFilter(Enum):
    EXCLUDE = 1
    INCLUDE = 2
    DELETED_ONLY = 3


class Barcode(Enum):
    BARCODE = 1
    QR = 2


class FillingStrategy(Enum):
    """
    Strategy for filling grid containers
    """

    BY_ROW = 1
    BY_COLUMN = 2
    EXACT = 3


class Sample:
    """
     Wraps a dict of Sample data returned from samples/{id} GET API call
    """

    def __init__(self, data: dict):
        self.data = data

    def wherep(self) -> List[str]:
        """
        Returns a list of breadcrumb names of all containers that subsamples 
        of this sample are located in.

        Returns
        -------
        A List of Strings like "Mikes fridge->shelf 2-> Blue box #23"

        """
        bcumbs = set()
        for ss in self.data["subSamples"]:
            b_crumb = " -> ".join([x["name"] for x in ss["parentContainers"]][::-1])
            bcumbs.add(b_crumb)
        return bcumbs

    def __str__(self):
        return f"Sample: id = {self.data['id']}, name = {self.data['name']}, creationDate = {self.data['created']}"


class GridPlacement:
    """
     Superclass of all grid placement strategies
    """

    def __init__(self, items_to_move: str, filling_strategy: FillingStrategy):
        ids = []
        for item in items_to_move:
            toMove = Id(item)
            if not toMove.is_movable():
                raise ValueError(f" Can't move {item} - not a movable type")
            ids.append(toMove)
        self.items_to_move = ids
        self.filling_strategy = filling_strategy


class AutoFit(GridPlacement):
    """
     Base class of ByRow and ByColumn filling strategies.
    """

    def __init__(
        self,
        column_index: int,
        row_index: int,
        total_columns: int,
        total_rows: int,
        items_to_move,
        filling_strategy,
    ):
        if len(items_to_move) == 0:
            raise ValueError("Provide at least one item to move")
        for arg in (row_index, column_index, total_columns, total_rows):
            if arg < 1:
                raise ValueError("All row/column indices must be >= 1")
        if column_index > total_columns or row_index > total_rows:
            raise ValueError(
                f"Column and row indexes({column_index},{row_index}"
                + " must fit in dimensions ({total_columns}, {total_rows}"
            )
        super().__init__(items_to_move, filling_strategy)
        self.row_index = row_index
        self.column_index = column_index
        self.total_columns = total_columns
        self.total_rows = total_rows


class GridLocation:
    """
    Stores column(x) and row(y) indices of a GridContainer
    """

    def __init__(self, x: int, y: int):
        if x < 1 or y < 1:
            raise ValueError("Grid location coordinates must be >= 1")
        self.x = x
        self.y = y


class ByRow(AutoFit):
    """
      Defines a strategy for filling a grid container with a list of items, filling rows
      in turn, from a starting location. 
    """

    def __init__(
        self,
        column_index: int,
        row_index: int,
        total_columns: int,
        total_rows: int,
        *items_to_move,
    ):
        """

        Parameters
        ----------
        column_index : int
            The column (x) index, 1-based, to start placing items.
        row_index : int
           The row (y) index, 1-based, from top->bottom, to start placing items.
        total_columns : int
            The total number of columns in the grid
        total_rows : int
            The total number of rows in the grid
        *items_to_move : 
            One or more global Ids.
         : TYPE
            DESCRIPTION.

        Returns
        -------
        None.

        """
        super().__init__(
            column_index,
            row_index,
            total_columns,
            total_rows,
            items_to_move,
            filling_strategy=FillingStrategy.BY_ROW,
        )


class ByColumn(AutoFit):
    """
      Defines a strategy for filling a grid container with a list of items, filling columns
      in turn, from a starting location. 
    """

    def __init__(
        self,
        column_index: int,
        row_index: int,
        total_columns: int,
        total_rows: int,
        *items_to_move,
    ):
        """
        Parameters
        ----------
        column_index : int
            The column (x) index, 1-based, to start placing items.
        row_index : int
           The row (y) index, 1-based, from top->bottom, to start placing items.
        total_columns : int
            The total number of columns in the grid
        total_rows : int
            The total number of rows in the grid
        *items_to_move : 
            One or more global Ids.
         : TYPE
            DESCRIPTION.

       """
        super().__init__(
            column_index,
            row_index,
            total_columns,
            total_rows,
            items_to_move,
            filling_strategy=FillingStrategy.BY_COLUMN,
        )


class ByLocation(GridPlacement):
    """
    Place one or more items by exact location
    """

    def __init__(self, locations: List[GridLocation], *items_to_move):
        if len(locations) != len(items_to_move):
            raise ValueError(
                f"locations list (length {len(locations)}) is not the same length as items list ({len(items_to_move)})"
            )
        super().__init__(items_to_move, filling_strategy=FillingStrategy.EXACT)
        self.locations = locations


class BulkOperationResult:
    def __init__(self, json):
        self.data = json

    def is_ok(self):
        return self.data["status"] == "COMPLETED"

    def is_failed(self):
        return not self.is_ok()


class Container:
    """
     Base class of all Container types
    """

    @classmethod
    def of(clz, container: dict):
        """
        Factory method to create a specific container object from raw JSON

        Parameters
        ----------
        container : dict
          JSON returned from get_container_by_id or create_container methods

        Raises
        ------
        ValueError
            if the JSON is not of the correct type

        Returns
        -------
            A subclass based on the 'cType' value of the container's JSON

        """
        Container._is_valid_container(container)
        if container["cType"] == "GRID":
            return GridContainer(container)
        elif container["cType"] == "LIST":
            return ListContainer(container)
        elif container["cType"] == "WORKBENCH":
            return Workbench(container)
        else:
            raise ValueError(f"unsupported container type {container['cType']}")

    @staticmethod
    def _is_valid_container(container):
        if "cType" not in container.keys():
            raise ValueError(
                "no 'cType' container type entry - is this really a container?"
            )

    def __init__(self, container: dict):
        Container._is_valid_container(container)
        self.data = container

    def _validate_type(self, c, expected_c_type):
        if c["cType"] != expected_c_type:
            raise ValueError(
                f"required {expected_c_type} container but is of cType {c['cType']}"
            )

    def is_grid(self) -> bool:
        return False

    def is_list(self) -> bool:
        return False

    def is_workbench(self) -> bool:
        return False

    def accept_subsamples(self) -> bool:
        return self.data["canStoreSamples"] == True

    def accept_containers(self) -> bool:
        return self.data["canStoreContainers"] == True

    def capacity(self) -> int:
        pass


class ListContainer(Container):
    """
     A ListContainer is an ordered container of unlimited capacity
    """

    def __init__(self, list_container: dict):
        super().__init__(list_container)
        self._validate_type(list_container, "LIST")

    def is_list() -> bool:
        return True

    def capacity(self) -> int:
        """
        Unlimited capacity
        """
        return sys.maxsize


class Workbench(Container):
    """
      A specialised Container holding currently active samples and containers.
    """

    def __init__(self, workbench: dict):
        super().__init__(workbench)
        self._validate_type(workbench, "WORKBENCH")

    def is_workbench(self) -> bool:
        return True


class GridContainer(Container):
    """
    Encapsulates results from create_grid_container() or get_container_by_id()
    """

    def __init__(self, grid_container: dict):
        super().__init__(grid_container)
        self._validate_type(grid_container, "GRID")

    def is_grid(self) -> bool:
        return True

    def row_count(self) -> int:
        return self.data["gridLayout"]["rowsNumber"]

    def column_count(self) -> int:
        return self.data["gridLayout"]["columnsNumber"]

    def capacity(self) -> int:
        """
        The number of cells in the grid - product of row and column counts
        """
        return self.row_count() * self.column_count()

    def free(self) -> int:
        """
        Returns
        -------
        int Number of free cells available to hold new content
        """
        return self.capacity() - self.in_use()

    def in_use(self) -> int:
        """
        Returns
        -------
        int Number of cells holding content
        """
        return len(self.data["locations"])

    def percent_full(self) -> float:
        return (self.in_use() / self.capacity()) * 100

    def used_locations(self):
        """
        Returns
        -------
        list of tuples of x,y coords of cells with content; 1-based, where x is column number and y is row number

        """
        return [(item["coordX"], item["coordY"]) for item in self.data["locations"]]

    def free_locations(self):
        """
        The inverse of 'used_locations' - gets empty grid cells
        Returns
        -------
        list of tuples of x,y coords of empty cells; 1-based, where x is column number and y is row number
        """
        rc = []
        used = self.used_locations()
        for col in range(1, self.column_count() + 1):
            for row in range(1, self.row_count() + 1):
                if (col, row) not in used:
                    rc.append((col, row))
        return rc


class SearchFilter:
    def __init__(
        self, deleted_item_filter=DeletedItemFilter.EXCLUDE, owned_by: str = None
    ):
        self.data = {}
        if deleted_item_filter is not None:
            self.data["deletedItems"] = deleted_item_filter.name
        if owned_by is not None and len(owned_by) > 0:
            self.data["ownedBy"] = owned_by


class ResultType(Enum):
    SAMPLE = 1
    SUBSAMPLE = 2
    TEMPLATE = 3
    CONTAINER = 4


class Id:
    """
    Supports integer or string representation of a globalId or
    numeric ID or a dict / object representation of an Inventory item.
    If a dict is passed, it must have 'id' and 'globalId' properties
    """

    Pattern = r"([A-Z]{2})?\d+"

    PREFIX_TO_TYPE = {
        "IC": "CONTAINER",
        "SS": "SUBSAMPLE",
        "SA": "SAMPLE",
        "IT": "TEMPLATE",
    }
    PREFIX_TO_API = {
        "IC": "containers",
        "SS": "subSamples",
        "SA": "samples",
        "IT": "templates",
    }

    def __init__(self, value: Union[int, str, dict, Container, Sample]):

        if isinstance(value, str):
            if re.match(Id.Pattern, value) is None:
                raise ValueError("incorrect global id format")

            if len(value) > 2 and value[0:2].isalpha():
                self.prefix = value[0:2]
                self.id = int(value[2:])
            else:
                self.id = int(value)
        elif isinstance(value, Container):
            self.prefix = "IC"
            self.id = value.data["id"]
        elif isinstance(value, dict):
            if "id" in value.keys():
                self.id = value["id"]
            else:
                raise TypeError(
                    "Could not interpet dict as an identifiable Inventory item."
                )

            if "globalId" in value.keys():
                self.prefix = value["globalId"][0:2]

        elif isinstance(value, int):
            self.id = value
        else:
            raise TypeError(
                f"Could not interpet {value} as an identifiable Inventory item."
            )

    def as_id(self) -> int:
        return self.id

    def as_global_id(self) -> str:
        """
        Assumes that prefix has been set

        Returns
        -------
        str global_id

        """
        return self.prefix + str(self.id)

    def is_container(self, maybe: bool = False) -> bool:
        return self._check("IC", maybe)

    def is_subsample(self, maybe: bool = False) -> bool:
        return self._check("SS", maybe)

    def is_movable(self, maybe: bool = False) -> bool:
        return self.is_subsample(maybe) or self.is_container(maybe)

    def get_type(self):
        return Id.PREFIX_TO_TYPE[self.prefix]

    def get_api_endpoint(self):
        return Id.PREFIX_TO_API[self.prefix]

    def _check(self, prefix, maybe: bool):
        if maybe:
            return not hasattr(self, "prefix") or self.prefix == prefix
        else:
            return hasattr(self, "prefix") and self.prefix == prefix


class ExtraFieldType(Enum):
    TEXT = "text"
    NUMBER = "number"


class TemperatureUnit(Enum):
    CELSIUS = 8
    KELVIN = 9


class StorageTemperature:
    def __init__(
        self, degrees: float, units: TemperatureUnit = TemperatureUnit.CELSIUS
    ):
        self.degrees = degrees
        self.units = units

    def _toDict(self) -> dict:
        return {"unitId": self.units.value, "numericValue": self.degrees}


class Quantity:
    def __init__(self, value: float, units: qu.QuantityUnit):
        self.value = value
        self.units = units

    def _toDict(self) -> dict:
        return {"numericValue": self.value, "unitId": self.units["id"]}


class ExtraField:
    """
    The data in the 'content' field must be of the type set in the 'fieldType' field
    """

    def __init__(
        self,
        name: str,
        fieldType: ExtraFieldType = ExtraFieldType.TEXT,
        content: Union[str, int, float] = "",
    ):
        self.data = {"name": name, "type": fieldType.value, "content": content}


class InventoryClient(ClientBase):
    API_VERSION = "v1"

    def _get_api_url(self):
        """
        Returns an API server URL.
        :return: string URL
        """

        return f"{self.rspace_url}/api/inventory/{self.API_VERSION}"

    def create_sample(
        self,
        name: str,
        tags: Optional[str] = None,
        description: Optional[str] = None,
        extra_fields: Optional[Sequence] = [],
        storage_temperature_min: StorageTemperature = None,
        storage_temperature_max: StorageTemperature = None,
        expiry_date: datetime.datetime = None,
        subsample_count: int = None,
        total_quantity: Quantity = None,
        attachments=None,
    ) -> dict:
        """
        Creates a new sample with a mandatory name, optional attributes
        If no template id is specified, the default template will be used,
        whose quantity is measured as a volume.
        """
        data = self._set_core_properties(name, tags, description, extra_fields)
        if storage_temperature_min is not None:
            data["storageTempMin"] = storage_temperature_min._toDict()
        if storage_temperature_max is not None:
            data["storageTempMax"] = storage_temperature_max._toDict()
        if expiry_date is not None:
            data["expiryDate"] = expiry_date.isoformat()
        if subsample_count is not None:
            data["newSampleSubSamplesCount"] = subsample_count
        if total_quantity is not None:
            data["quantity"] = total_quantity._toDict()
        ## fail early
        if attachments is not None:
            if not isinstance(attachments, list):
                raise ValueError("attachments must be a list of open files")

        sample = self.retrieve_api_results("/samples", request_type="POST", params=data)
        if attachments is not None:
            self.serr(f"adding {len(attachments)} attachments")
            for file in attachments:
                self.uploadAttachment(sample["globalId"], file)
            ## get latest version
            sample = self.get_sample_by_id(sample["id"])
        return sample

    def get_sample_by_id(self, sample_id: Union[str, int]) -> dict:
        """
        Gets a full sample information by id or global id
        Parameters
        ----------
        id : Union[int, str]
            An integer ID e.g 1234 or a global ID e.g. SA1234
        Returns
        -------
        dict
            A full description of one sample
        """
        s_id = Id(sample_id)
        return self.retrieve_api_results(f"/samples/{s_id.as_id()}")

    def get_subsample_by_id(self, subsample_id: Union[str, int]) -> dict:
        ss_id = Id(subsample_id)
        if ss_id.is_subsample is False:
            raise ValueError(f"{ss_id} is not id of a subsample")
        return self.retrieve_api_results(f"/subSamples/{ss_id.as_id()}")

    def list_samples(
        self, pagination: Pagination = Pagination(), sample_filter: SearchFilter = None
    ) -> dict:
        """
        Parameters
        ----------
        pagination : Pagination, optional
            The default is Pagination().
        Returns
        -------
        Paginated Search result. Use 'next' and 'prev' links to navigate
        """
        return self._do_simple_list("samples", pagination, sample_filter)

    def list_top_level_containers(
        self, pagination: Pagination = Pagination(), sample_filter: SearchFilter = None
    ) -> dict:
        """
        Parameters
        ----------
        pagination : Pagination, optional
            The default is Pagination().
        Returns
        -------
        Paginated Search result. Use 'next' and 'prev' links to navigate
        """
        return self._do_simple_list("containers", pagination, sample_filter)

    def list_subsamples(
        self, pagination: Pagination = Pagination(), sample_filter: SearchFilter = None
    ) -> dict:
        """
        Parameters
        ----------
        pagination : Pagination, optional
            The default is Pagination().
        Returns
        -------
        Paginated Search result. Use 'next' and 'prev' links to navigate
        """
        return self._do_simple_list("subSamples", pagination, sample_filter)

    def _do_simple_list(self, endpoint, pagination, sample_filter):
        if sample_filter is not None:
            pagination.data.update(sample_filter.data)
        self.serr(f"pg is {pagination.data}")
        return self.retrieve_api_results(
            f"/{endpoint}", request_type="GET", params=pagination.data,
        )

    def stream_samples(
        self, pagination: Pagination = Pagination(), sample_filter: SearchFilter = None
    ):
        """
        Streams all samples. Pagination argument sets batch size and ordering.
        Parameters
        ----------
        pagination : Pagination, optional. The default is Pagination().

        Yields
        ------
        item : One Sample at a time
        """
        if sample_filter is not None:
            pagination.data.update(sample_filter.data)
        return self._stream("samples", pagination)

    def stream_top_level_containers(
        self, pagination: Pagination = Pagination(), sample_filter: SearchFilter = None
    ):
        """
        Streams all containers. Pagination argument sets batch size and ordering.
        Parameters
        ----------
        pagination : Pagination, optional. The default is Pagination().

        Yields
        ------
        item : One Container at a time
        """
        if sample_filter is not None:
            pagination.data.update(sample_filter.data)
        return self._stream("containers", pagination)

    def rename(self, sample_id: Union[str, dict], new_name: str) -> dict:
        """
        Parameters
        ----------
            id : Id  of item to rename
            new_name : str The new name.
        Returns
        -------
            dict : The updated item
        """
        s_id = Id(sample_id)
        endpoint = s_id.get_api_endpoint()
        return self.retrieve_api_results(
            f"/{endpoint}/{s_id.as_id()}",
            request_type="PUT",
            params={"name": new_name},
        )

    def delete_sample(self, sample_id: Union[int, str]):
        """
        Parameters
        ----------
        sample_id : Union[int, str]
            A integer id, or a string id or global ID.

        Returns
        -------
        None.

        """
        id_to_delete = Id(sample_id)
        self.doDelete("samples", id_to_delete.as_id())

    def add_extra_fields(self, item_id: Union[str, dict], *ExtraField) -> dict:
        s_id = Id(item_id)
        endpoint = s_id.get_api_endpoint()
        toPut = []
        for ef in ExtraField:
            ef.data["newFieldRequest"] = True
            toPut.append(ef.data)
        return self.retrieve_api_results(
            f"/{endpoint}/{s_id.as_id()}",
            request_type="PUT",
            params={"extraFields": toPut},
        )

    def uploadAttachment(self, inventory_item: Union[str, dict], file) -> dict:
        """
        Uploads an attachment file to an sample, subsample or container.
        Parameters
        ----------
        - inventory_item : str
            Global id or dictionary of a sample (SA...), Subsample (SS...) or Container (IC...)
        - file : an open file
            An open file stream.

        Returns
        -------
        Dict of the created InventoryFile
        """
        global_id = Id(inventory_item)
        fs = {"parentGlobalId": global_id.as_global_id()}
        fsStr = json.dumps(fs)
        headers = self._get_headers()
        response = requests.post(
            self._get_api_url() + "/files",
            files={"file": file, "fileSettings": (None, fsStr, "application/json")},
            headers=headers,
        )
        return self._handle_response(response)

    def split_subsample(
        self,
        subsample: Union[int, str, dict],
        num_new_subsamples: int,
        quantity_per_subsample: float = None,
    ):
        def _do_call(ss_id, params):
            return self.retrieve_api_results(
                f"/subSamples/{ss_id.as_id()}/actions/split",
                request_type="POST",
                params=params,
            )

        ss_id = Id(subsample)
        if quantity_per_subsample is None:
            to_post = {"numSubSamples": num_new_subsamples + 1, "split": True}
            return _do_call(ss_id, to_post)
        else:
            qu_to_decrement_from_original = num_new_subsamples * quantity_per_subsample
            curr_quantity = None
            if isinstance(subsample, dict) and ss_id.is_subsample(True):
                ## we already have quantity info, don't need to call
                curr_quantity = subsample["quantity"]
            else:
                full_ss = self.get_subsample_by_id(ss_id.as_id())
                curr_quantity = full_ss["quantity"]
            if qu_to_decrement_from_original > curr_quantity["numericValue"]:
                raise ValueError(
                    f"Attempting to remove {qu_to_decrement_from_original}, but original subsample {ss_id.as_id()} has amount {curr_quantity['numericValue']}."
                )
            to_post = {"numSubSamples": num_new_subsamples + 1, "split": True}
            new_ss = _do_call(ss_id, to_post)
            curr_quantity["numericValue"] = (
                curr_quantity["numericValue"] - qu_to_decrement_from_original
            )
            unit_id = curr_quantity["unitId"]
            records = []
            records.append(
                {
                    "id": ss_id.as_id(),
                    "type": ss_id.get_type(),
                    "quantity": curr_quantity,
                }
            )
            for split_ss in new_ss:
                split_ss_id = Id(split_ss)
                records.append(
                    {
                        "id": split_ss_id.as_id(),
                        "type": split_ss_id.get_type(),
                        "quantity": {
                            "unitId": unit_id,
                            "numericValue": quantity_per_subsample,
                        },
                    }
                )
            bulk_post = {"records": records, "operationType": "UPDATE"}
            rc = self.retrieve_api_results(
                "/bulk", request_type="POST", params=bulk_post
            )
            return BulkOperationResult(rc)

    def duplicate(
        self, item_to_duplicate: Union[str, dict], new_name: str = None
    ) -> dict:
        """
        Parameters
        ----------
        global_id : str
            Global id  of template,sample, subsample or container or aict containing global_id
        new_name : optional new name of the copy

        Returns
        -------
        The duplicated item

        """
        id_to_copy = Id(item_to_duplicate)
        endpoint = id_to_copy.get_api_endpoint()
        rc = self.retrieve_api_results(
            f"/{endpoint}/{id_to_copy.as_id()}/actions/duplicate", request_type="POST",
        )
        if new_name is not None:
            rc = self.rename(rc, new_name)
        return rc

    def search(
        self, query: str, pagination=Pagination(), result_type: ResultType = None
    ) -> dict:
        """
        Searches by a query, optionally paginated or restricted to a particular type (container,
                                                                                      sample, subsample or template)
        Parameters
        ----------
        query : str
            Any text string. Will search against name, tag, description
        pagination : optional
            The default is Pagination().
        result_type : ResultType, optional
         The default is None.

        Returns
        -------
        dict
            Search result summary and first page of results.

        """
        params = {"query": query}
        params.update(pagination.data)
        if result_type is not None:
            params["resultType"] = result_type.name
        return self.retrieve_api_results("/search", params=params)

    def _set_core_properties(
        self,
        name: str,
        tags: Optional[str] = None,
        description: Optional[str] = None,
        extra_fields: Optional[Sequence] = [],
    ):
        data = {}
        data["name"] = name
        if tags is not None:
            data["tags"] = tags
        if description is not None:
            data["description"] = description
        if extra_fields is not None:
            data["extraFields"] = [ef.data for ef in extra_fields]
        return data

    def add_note_to_subsample(
        self, subsample: Union[str, int, dict], note: str
    ) -> dict:
        ss_id = Id(subsample)
        if not ss_id.is_subsample(True):
            raise ValueError("Supplied id is not a subsamples")
        data = {"content": note}
        return self.retrieve_api_results(
            f"/subSamples/{ss_id.as_id()}/notes", request_type="POST", params=data,
        )

    def get_workbenches(self) -> Sequence[dict]:
        """
        Returns
        -------
        Sequence[dict]
            A list of Workbenches that you have permission to see. You will also retrieve
            your own workbench

        """
        result = self.retrieve_api_results("/workbenches")
        return [wb for wb in result["containers"]]

    def create_list_container(
        self,
        name: str,
        tags: Optional[str] = None,
        description: Optional[str] = None,
        extra_fields: Optional[Sequence] = [],
        can_store_containers: bool = True,
        can_store_subsamples: bool = True,
    ) -> dict:

        data = self._set_core_properties(name, tags, description, extra_fields)
        data["cType"] = "LIST"
        data["canStoreContainers"] = can_store_containers
        data["canStoreSubsamples"] = can_store_subsamples

        container = self.retrieve_api_results(
            "/containers", request_type="POST", params=data
        )
        return container

    def get_container_by_id(self, container_id: Union[str, int]) -> dict:
        c_id = Id(container_id)
        return self.retrieve_api_results(f"/containers/{c_id.as_id()}")

    def create_grid_container(
        self,
        name: str,
        row_count: int,
        column_count: int,
        tags: Optional[str] = None,
        description: Optional[str] = None,
        extra_fields: Optional[Sequence] = [],
        can_store_containers: bool = True,
        can_store_subsamples: bool = True,
    ) -> dict:

        data = self._set_core_properties(name, tags, description, extra_fields)
        data["cType"] = "GRID"
        data["canStoreContainers"] = can_store_containers
        data["canStoreSubsamples"] = can_store_subsamples
        data["gridLayout"] = {"columnsNumber": column_count, "rowsNumber": row_count}

        container = self.retrieve_api_results(
            "/containers", request_type="POST", params=data
        )
        return container

    def set_as_top_level_container(self, container: Union[int, str, dict, Container]):
        data = {"removeFromParentContainerRequest": True}
        c_id = Id(container)

        return self.retrieve_api_results(
            f"/containers/{c_id.as_id()}", request_type="PUT", params=data
        )

    def add_items_to_list_container(
        self, target_container_id: Union[str, int], *item_ids: str,
    ) -> list:
        """
        Adds 1 or more items to a list container

        Parameters
        ----------
        target_container_id : Union[str, int]
            The id of a List container

        *item_ids : Union[str, int]
            One or more globalids of items  to move into the target container

        Raises
        ------
        ValueError
            If any item_id is not movable

        Returns
        -------
        BulkoperationResult

        """
        id_target = Id(target_container_id)
        if not id_target.is_container(maybe=True):
            raise ValueError("Target must be a container")

        valid_item_ids = []

        ## assert there are no invalid globai ids
        for item_id in item_ids:
            id_ob = Id(item_id)
            if not id_ob.is_movable():
                raise ValueError(f"Item to move '{item_id}' must be a container")
            valid_item_ids.append(id_ob)

        return self._do_add_to_list_container(valid_item_ids, id_target)

    def add_items_to_grid_container(
        self,
        target_container_id: Union[str, int, GridContainer],
        grid_placement: GridPlacement,
    ) -> BulkOperationResult:
        """
        Add one or more subsamples or containers to a grid container, starting at given row/ column
        index

        Parameters
        ----------
        target_container_id : Union[str, int]
            The Grid container to move to.
        grid_placement: configuration for how to place items in the grid
        Raises
        ------
        ValueError
            If items are the wrong or inconsistent type

        Returns
        -------
        list
            A list of updated items showing their current position

        """
        if isinstance(target_container_id, GridContainer):
            if target_container_id.free() < len(grid_placement.items_to_move):
                raise ValueError(
                    f"not enough space in {target_container_id.data['globalId']} to store {len(grid_placement.items_to_move)} - only {target_container_id.free()} spaces free."
                )
        id_target = Id(target_container_id)
        if not id_target.is_container(maybe=True):
            raise ValueError("Target must be a container")
        ## assert there are no invalid global ids (things that are not subsamples)

        bulk_post = self._create_bulk_move(id_target, grid_placement)
        ## get target - are there enough spaces?
        ## iterate over grid (0 or 1 based?)
        ## use bulk API?

        resp_json = self.retrieve_api_results(
            "/bulk", request_type="POST", params=bulk_post
        )
        return BulkOperationResult(resp_json)

    def _do_add_to_list_container(self, items, id_target):
        coords = []
        for item in items:
            coords.append(
                {
                    "type": item.get_type(),
                    "id": item.as_id(),
                    "parentContainers": [{"id": id_target.as_id()}],
                }
            )
        to_post = {"operationType": "MOVE", "records": coords}

        resp_json = self.retrieve_api_results(
            "/bulk", request_type="POST", params=to_post
        )

        return BulkOperationResult(resp_json)

    def _create_bulk_move(self, grid_id: Id, gp: GridPlacement):
        coords = []  # array of x,y coords
        ##
        if FillingStrategy.EXACT == gp.filling_strategy:
            for (item, coord) in zip(gp.items_to_move, gp.locations):
                coords.append(
                    {
                        "type": item.get_type(),
                        "id": item.as_id(),
                        "parentContainers": [{"id": grid_id.as_id()}],
                        "parentLocation": {"coordX": coord.x, "coordY": coord.y},
                    }
                )

            return {"operationType": "MOVE", "records": coords}
        else:
            counter = _calculate_start_index(
                gp.column_index,
                gp.row_index,
                gp.total_columns,
                gp.total_rows,
                gp.filling_strategy,
            )
            for ss_id in gp.items_to_move:

                x = gp.column_index
                y = gp.row_index
                if FillingStrategy.BY_ROW == gp.filling_strategy:
                    x = counter % gp.total_columns + 1
                    y = int(counter / gp.total_columns) + 1
                elif FillingStrategy.BY_COLUMN == gp.filling_strategy:
                    x = int(counter / gp.total_rows) + 1
                    y = counter % gp.total_rows + 1
                coords.append(
                    {
                        "type": ss_id.get_type(),
                        "id": ss_id.as_id(),
                        "parentContainers": [{"id": grid_id.as_id()}],
                        "parentLocation": {"coordX": x, "coordY": y},
                    }
                )
                counter = counter + 1
            return {"operationType": "MOVE", "records": coords}

    def create_list_of_materials(
        self,
        eln_field_id: int,
        name: str,
        *materials: Union[str, dict],
        description: str = None,
    ) -> dict:
        """
        Creates a new ListOfMaterials, attached to an ELN text field.

        Parameters
        ----------
        eln_field_id : int
            The ID of the field
        name : str
            A label for the LoM
        *materials : Union[str, dict]
            One or more globalIds or objects representing samples, subsamples or containers
        description : str, optional
            DESC. The default is None.
         : TYPE
            A decription of the purpose or the LoM

        Returns
        -------
        dict
            The newly created ListOfMaterials.

        """
        id_list = [Id(item) for item in materials]
        materials = []
        for item_id in id_list:
            materials.append(
                {"invRec": {"id": item_id.as_id(), "type": item_id.get_type()}}
            )

        to_post = {"name": name, "elnFieldId": eln_field_id, "materials": materials}
        if description is not None:
            to_post["description"] = description
        return self.retrieve_api_results(
            "/listOfMaterials", request_type="POST", params=to_post
        )

    def get_list_of_materials_for_document(self, document_id: Union[str, int, dict]):
        """
        Gets all ListsOfMaterials belonging to one ELN document

        Parameters
        ----------
        document_id : Union[str, int, dict]
            The document id, globalId or a dict of the document

        Returns
        -------
        A List of List Of Materials

        """
        doc_id = self._get_numeric_record_id(document_id)
        return self.retrieve_api_results(f"/listOfMaterials/forDocument/{doc_id}")

    def get_list_of_materials_for_field(self, field_id: Union[str, int]):
        """
        Gets all lists of materials belongong to an ELN document field

        Parameters
        ----------
        field_id : Union[str, int]

        Returns
        -------
        A List of List Of Materials

        """
        doc_id = self._get_numeric_record_id(field_id)
        return self.retrieve_api_results(f"/listOfMaterials/forField/{doc_id}")

    def get_list_of_materials(self, lom_id: int) -> dict:
        """
        Gets one List Of Materials by its id

        Parameters
        ----------
        lom_id : int

        Returns
        -------
        dict
            The list of materials.

        """
        return self.retrieve_api_results(f"/listOfMaterials/{lom_id}")

    def barcode(
        self,
        global_id: Union[str, dict],
        outfile: str = None,
        barcode_type: Barcode = Barcode.BARCODE,
    ) -> bytes:
        """
        Generates a QR code or barcode image, optionally saving to file if filepath supplied.
        Parameters
        ----------
        global_id : Union[str, dict]
        
        barcode_type:
             The default is Barcode.BARCODE.

        Returns
        -------
            Bytes of the image.

        """
        Id(global_id)  ## validate is identifier
        data = {"content": global_id, "barcodeType": barcode_type.name}
        url = f"{self._get_api_url()}/barcodes"
        headers = {"apiKey": self.api_key, "Accept": "image/png"}

        resp = requests.get(url, headers=headers, params=data)
        resp.raise_for_status()
        content = resp.content
        if outfile is not None:
            with open(outfile, "wb") as fd:
                fd.write(content)
        return content


def _calculate_start_index(
    col_start, row_start, total_columns, total_rows, filling_strategy
):
    if col_start < 1 or row_start < 1:
        raise ValueError("Columns and row starting position must be >= 1")
    if col_start > total_columns or row_start > total_rows:
        raise ValueError(
            f"Columns and row starting position must fit in grid: {total_rows} rows x {total_columns} columns"
        )

    index = 0
    if FillingStrategy.BY_ROW == filling_strategy:
        index = ((row_start - 1) * total_columns) + col_start
    elif FillingStrategy.BY_COLUMN == filling_strategy:
        index = ((col_start - 1) * total_rows) + row_start
    return index - 1