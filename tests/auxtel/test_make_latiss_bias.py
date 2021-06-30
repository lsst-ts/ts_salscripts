# This file is part of ts_standardscripts
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

__all__ = ['MakeLatissBias']

import unittest
import logging

from lsst.ts import standardscripts
from lsst.ts import externalscripts
from lsst.ts.externalscripts.auxtel import MakeLatissBias

logger = logging.getLogger(__name__)
logger.propagate = True


class TestMakeComCamBias(
    standardscripts.BaseScriptTestCase, unittest.IsolatedAsyncioTestCase
):
    async def test_configure(self):
        async with self.make_script():

            # Try configure with minimum set of parameters declared
            # Note that all are scalars and should be converted to arrays
            n_bias = 2
            detectors = "(0)"
            input_collections = "LATISS/calib"
            calib_dir = "LATISS/calib/u/plazas/TEST"
            await self.configure_script(
                n_bias=n_bias,
                detectors=detectors,
                input_collections=input_collections,
                calib_dir=calib_dir,
            )

            self.assertEqual(self.script.n_bias, n_bias)
            self.assertEqual(self.script.detectors, detectors)
            self.assertEqual(self.script.input_collections, input_collections)
            self.assertEqual(self.script.calib_dir, calib_dir)

    async def test_executable(self):
        scripts_dir = externalscripts.get_scripts_dir()
        script_path = scripts_dir / "auxtel" / "make_latiss_bias.py"
        logger.debug(f"Checking for script in {script_path}")
        await self.check_executable(script_path)


if __name__ == "__main__":
    unittest.main()