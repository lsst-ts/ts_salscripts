# This file is part of ts_externalscripts
#
# Developed for the LSST Telescope and Site Systems.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License

__all__ = ["MakeLatissBias"]

import yaml

from lsst.ts.observatory.control.auxtel.latiss import LATISS
from ..base_make_bias import BaseMakeBias


class MakeLatissBias(BaseMakeBias):
    """ Take biases and construct a master bias SAL Script.

    This class takes biases with LATISS and constructs
    a master bias calling the bias pipetask via OCPS.
    """

    def __init__(self, index=1):
        super().__init__(
            index=index,
            descr="This class takes biases with Auxtel-LATISS and constructs "
                  "a master bias calling the bias pipetask via OCPS.",
        )
        self._latiss = LATISS(domain=self.domain, log=self.log)

    @property
    def camera(self):
        return self._latiss

    @property
    def instrument_name(self):
        """String with instrument name for pipeline task"""
        return "LATISS"

    @property
    def image_in_oods(self):
        """Archiver"""
        return self.camera.rem.atarchiver.evt_imageInOODS

    @classmethod
    def get_schema(cls):
        schema = """
        $schema: http://json-schema.org/draft-07/schema#
        $id: https://github.com/lsst-ts/ts_externalscripts/python/lsst/ts/\
                externalscripts/auxtel/make_latiss_bias.py
        title: MakeLatissBias v1
        description: Configuration for making a LATISS bias SAL Script.
        type: object
        """
        schema_dict = yaml.safe_load(schema)
        schema_dict["properties"] = {}
        base_schema_dict = super(MakeLatissBias, cls).get_schema()

        for prop in base_schema_dict["properties"]:
            schema_dict["properties"][prop] = base_schema_dict["properties"][prop]

        return schema_dict
