# This file is part of ts_externalcripts
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

__all__ = ["StandbyLATISS"]

import yaml

from lsst.ts import salobj
from lsst.ts.standardscripts.auxtel.latiss import LATISS


class StandbyLATISS(salobj.BaseScript):
    """ Put LATISS components in standby.

    Parameters
    ----------
    index : `int`
        Index of Script SAL component.

    Notes
    -----
    **Checkpoints**

    **Details**

    """

    __test__ = False  # stop pytest from warning that this is not a test

    def __init__(self, index):

        super().__init__(index=index,
                         descr="Enable LATISS.")

        self.latiss = LATISS(self.domain)

    @classmethod
    def get_schema(cls):
        schema_yaml = """
            $schema: http://json-schema.org/draft-07/schema#
            $id: https://github.com/lsst-ts/ts_standardscripts/auxtel/StandbyLATISS.yaml
            title: EnableLATISS v1
            description: Enable LATISS.
            type: object
            additionalProperties: false
        """
        return yaml.safe_load(schema_yaml)

    async def configure(self, config):
        """Configure script.

        Parameters
        ----------
        config : `types.SimpleNamespace`
            Script configuration, as defined by `schema`.
        """
        pass

    def set_metadata(self, metadata):
        metadata.duration = 60.

    async def run(self):

        await self.latiss.enable()