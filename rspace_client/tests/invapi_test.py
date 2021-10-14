#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sat Oct  2 22:09:40 2021

@author: richard
"""
import sys
import datetime as dt
import rspace_client.tests.base_test as base
from rspace_client.inv import inv
import rspace_client.inv.quantity_unit as qu


class InventoryApiTest(base.BaseApiTest):
    def setUp(self):
        """
        Skips tests if aPI client credentials are missing
        """
        self.assertClientCredentials()
        self.invapi = inv.InventoryClient(self.rspace_url, self.rspace_apikey)

    def test_create_sample(self):
        sample_name = base.random_string(5)
        sample_tags = base.random_string(4)
        ef1 = inv.ExtraField("f1", inv.ExtraFieldType.TEXT, "hello")
        ef2 = inv.ExtraField("f2", inv.ExtraFieldType.NUMBER, 123)

        minTemp = inv.StorageTemperature(1, inv.TemperatureUnit.KELVIN)
        maxTemp = inv.StorageTemperature(4, inv.TemperatureUnit.KELVIN)
        expiry_date = dt.date(2024, 12, 25)
        amount = inv.Quantity(5, qu.QuantityUnit.of("ml"))
        sample = self.invapi.create_sample(
            name=sample_name,
            tags=sample_tags,
            extra_fields=[ef1, ef2],
            storage_temperature_min=minTemp,
            storage_temperature_max=maxTemp,
            expiry_date=expiry_date,
            total_quantity=amount,
            subsample_count=12,
            attachments=[open(base.get_any_datafile(), "rb")],
        )
        self.assertEqual(sample_name, sample["name"])
        self.assertEqual(2, len(sample["extraFields"]))
        self.assertEqual(4, sample["storageTempMax"]["numericValue"])
        self.assertEqual(1, sample["storageTempMin"]["numericValue"])
        self.assertEqual(12, sample["subSamplesCount"])
        self.assertEqual(expiry_date.isoformat(), sample["expiryDate"])
        self.assertEqual(1, len(sample["attachments"]))

    def test_create_sample_name_only(self):
        sample = self.invapi.create_sample(base.random_string(5))
        self.assertIsNotNone(sample)

    def test_get_single_sample(self):
        sample = self.invapi.create_sample(base.random_string(5))
        sample2 = self.invapi.get_sample_by_id(sample["id"])
        sample3 = self.invapi.get_sample_by_id(sample["globalId"])
        self.assertEqual(sample["name"], sample2["name"])
        self.assertEqual(sample["name"], sample3["name"])

    def test_upload_file(self):
        sample = self.invapi.create_sample(base.random_string())
        data_file = base.get_any_datafile()
        with open(data_file, "rb") as f:
            resp = self.invapi.uploadAttachment(sample["globalId"], f)
            self.assertIsNotNone(resp["id"])
            self.assertTrue(resp["globalId"][0:2] == "IF")

    def test_rename_sample(self):
        sample = self.invapi.create_sample(base.random_string(5))
        new_name = base.random_string()
        updated = self.invapi.rename_sample(sample["id"], new_name)
        self.assertEqual(new_name, updated["name"])

    def test_list_samples(self):
        samples = self.invapi.list_samples(inv.Pagination(sort_order="desc"))

        self.assertEqual(0, samples["pageNumber"])
        self.assertEqual(10, len(samples["samples"]))

    def test_paginated_samples(self):
        pag = inv.Pagination(page_nunber=1, page_size=1, sort_order="desc")
        samples = self.invapi.list_samples(pag)
        self.assertEqual(1, samples["pageNumber"])
        self.assertEqual(1, len(samples["samples"]))

    def test_stream_samples(self):
        onePerPage = inv.Pagination(page_size=1)
        gen = self.invapi.stream_samples(onePerPage)
        # get 2 items
        s1 = next(gen)
        s2 = next(gen)
        self.assertNotEqual(s1["id"], s2["id"])

        unknown_user = base.random_string(15)
        sample_filter = inv.SampleFilter(owned_by=unknown_user)
        gen = self.invapi.stream_samples(onePerPage, sample_filter)
        result_count = 0
        for sample in gen:
            result_count = result_count + 1
        self.assertEqual(0, result_count)

    def test_add_extra_fields(self):
        sample = self.invapi.create_sample(base.random_string())
        ef1 = inv.ExtraField(name="ef1", content="ef1 content")
        ef2 = inv.ExtraField(name="ef2", content="ef2 content")
        updatedS = self.invapi.add_extra_fields(sample["id"], ef1, ef2)
        self.assertEqual(2, len(updatedS["extraFields"]))

    def test_search_sample(self):
        name = base.random_string()
        tags = base.random_string()
        sample = self.invapi.create_sample(name, tags=tags)
        results = self.invapi.search(query=name)
        ## sample and its subsample
        self.assertEqual(2, results["totalHits"])
        results_from_tag = self.invapi.search(query=tags)
        self.assertEqual(1, results_from_tag["totalHits"])

        results2 = self.invapi.search(query=name, result_type=inv.ResultType.SAMPLE)
        self.assertEqual(1, results2["totalHits"])
        results3 = self.invapi.search(query=name, result_type=inv.ResultType.SUBSAMPLE)
        self.assertEqual(1, results3["totalHits"])
        results4 = self.invapi.search(query=name, result_type=inv.ResultType.CONTAINER)
        self.assertEqual(0, results4["totalHits"])

    def test_create_list_container(self):
        name = base.random_string()
        ct = self.invapi.create_list_container(name, tags="ab,cd,ef")
        self.assertTrue(ct["cType"] == "LIST")

    def test_move_container_to_list_container(self):
        name = base.random_string() + "_to_move"
        toMove = self.invapi.create_list_container(name)
        name_target = base.random_string() + "_target"
        target = self.invapi.create_list_container(name_target)
        moved = self.invapi.add_containers_to_list_container(target["id"], toMove["id"])
        self.assertEqual(moved[0]["parentContainers"][0]["name"], target["name"])

    def test_move_subsamples_to_list_container(self):
        name = base.random_string() + "_to_move"
        toMove = self.invapi.create_sample(name, subsample_count=2)
        name_target = base.random_string() + "_target"
        target = self.invapi.create_list_container(name_target)

        ## get the 2 subsample ids ad move to container
        subsample_ids = [ss["id"] for ss in toMove["subSamples"]]
        moved = self.invapi.add_subsamples_to_list_container(
            target["id"], *subsample_ids
        )
        self.assertEqual(moved[0]["parentContainers"][0]["name"], target["name"])
        self.assertEqual(moved[1]["parentContainers"][0]["name"], target["name"])

    def test_move_single_item_to_grid(self):
        grid_c = self.invapi.create_grid_container("gridX", 3, 2)
        sample = self.invapi.create_sample(name="multiS", subsample_count=1)
        self.invapi.add_items_to_grid_container(
            grid_c, 2, 1, 2, 3, sample["subSamples"][0]["globalId"]
        )

        ## now reload the container, which should show subsamples
        updated_container_json = self.invapi.get_container_by_id(grid_c["id"])
        container = inv.Container.of(updated_container_json)
        self.assertEqual(6, container.capacity())
        self.assertEqual(5, container.free())
        self.assertEqual(1, container.in_use())
        self.assertTrue((2, 1) in container.used_locations())

    def test_move_container_and_subsample_to_grid(self):
        grid_c = self.invapi.create_grid_container("gridX", 3, 2)
        sample = self.invapi.create_sample(name="multiS", subsample_count=1)
        other_container = self.invapi.create_list_container("toMove")
        ids_to_move = [sample["subSamples"][0]["globalId"], other_container["globalId"]]
        result = self.invapi.add_items_to_grid_container(
            grid_c, inv.ByRow(1, 1, 3, 2, *ids_to_move)
        )
        self.assertTrue(result.is_ok())

    def test_cannot_move_too_many_items_b4_request(self):
        grid_c = self.invapi.create_grid_container("gridX", 2, 2)
        sample = self.invapi.create_sample(name="toomany", subsample_count=5)

        ss_ids = [x["globalId"] for x in sample["subSamples"]]
        ## using GridContainer object, we can check capacity before requesting
        self.assertRaises(
            ValueError,
            self.invapi.add_items_to_grid_container,
            inv.GridContainer(grid_c), inv.ByRow(
            1,
            1,
            2,
            2,
            *ss_ids)
        )

    def test_cannot_move_too_many_items_with_request(self):
        grid_c = self.invapi.create_grid_container("gridX", 1, 2)
        sample = self.invapi.create_sample(name="toomany", subsample_count=4)

        ss_ids = [x["globalId"] for x in sample["subSamples"]]
        ## using just id, we try to make request anyway
        resp = self.invapi.add_items_to_grid_container(
            grid_c["id"], inv.ByRow(1, 1, 2, 1, *ss_ids[0:2])
        )
        self.assertTrue(resp.is_ok())
        ## overwrite, what happens
        resp2 = self.invapi.add_items_to_grid_container(
            grid_c["id"], inv.ByRow(1, 1, 2, 1, *ss_ids[2:4])
            )
        
        self.assertFalse(resp2.is_ok())
        self.assertTrue(resp2.is_failed())

    def test_bulk_move_to_grid(self):
        grid_c = self.invapi.create_grid_container("gridX", 7, 3)
        sample = self.invapi.create_sample(name="multiS", subsample_count=10)
        ss_ids = [x["globalId"] for x in sample["subSamples"]]
        print(" ss_ids are " + ",".join([str(x) for x in ss_ids]))
        rc = self.invapi.add_items_to_grid_container(
            grid_c, inv.ByColumn(2, 1, 3, 7, *ss_ids)
        )
        ## get list of updated subsamples
        self.assertEqual(10, len(rc.data["results"]))

        ## now reload the container, which should show subsamples
        updated_container_json = self.invapi.get_container_by_id(grid_c["id"])
        container = inv.Container.of(updated_container_json)
        self.assertEqual(21, container.capacity())
        self.assertEqual(11, container.free())
        self.assertEqual(10, container.in_use())

    def test_calculate_grid_start(self):
        total_cols = 5
        total_rows = 4
        self.assertEqual(
            7,
            inv._calculate_start_index(
                3, 2, total_cols, total_rows, inv.FillingStrategy.BY_ROW
            ),
        )
        self.assertEqual(
            9,
            inv._calculate_start_index(
                3, 2, total_cols, total_rows, inv.FillingStrategy.BY_COLUMN
            ),
        )
        self.assertEqual(
            0,
            inv._calculate_start_index(
                1, 1, total_cols, total_rows, inv.FillingStrategy.BY_COLUMN
            ),
        )
        self.assertEqual(
            19,
            inv._calculate_start_index(
                5, 4, total_cols, total_rows, inv.FillingStrategy.BY_COLUMN
            ),
        )
        self.assertEqual(
            19,
            inv._calculate_start_index(
                5, 4, total_cols, total_rows, inv.FillingStrategy.BY_ROW
            ),
        )

    def test_calculate_grid_start_validation(self):
        self.assertRaises(
            ValueError,
            inv._calculate_start_index,
            -1,
            2,
            5,
            4,
            inv.FillingStrategy.BY_ROW,
        )
        self.assertRaises(
            ValueError,
            inv._calculate_start_index,
            -2,
            -1,
            5,
            4,
            inv.FillingStrategy.BY_ROW,
        )

        self.assertRaises(
            ValueError,
            inv._calculate_start_index,
            6,
            2,
            5,
            4,
            inv.FillingStrategy.BY_ROW,
        )

        self.assertRaises(
            ValueError,
            inv._calculate_start_index,
            5,
            5,
            5,
            4,
            inv.FillingStrategy.BY_ROW,
        )

    def test_delete_samples(self):
        total_samples = self.invapi.list_samples()
        total_samples_count = total_samples["totalHits"]
        total_deleted = self.invapi.list_samples(
            sample_filter=inv.SampleFilter(
                deleted_item_filter=inv.DeletedItemFilter.DELETED_ONLY
            )
        )["totalHits"]
        self.invapi.delete_sample(total_samples["samples"][0]["id"])
        total_deleted2 = self.invapi.list_samples(
            sample_filter=inv.SampleFilter(
                deleted_item_filter=inv.DeletedItemFilter.DELETED_ONLY
            )
        )["totalHits"]
        self.assertEqual(total_deleted2, total_deleted + 1)
