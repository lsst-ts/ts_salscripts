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

__all__ = ["LatissCWFSAlign"]

import os
import time
import yaml
import asyncio

import concurrent.futures

from pathlib import Path

import numpy as np
from astropy import time as astropytime

from scipy import ndimage
from scipy.signal import medfilt

from lsst.ts import salobj
from lsst.ts.standardscripts.auxtel.attcs import ATTCS
from lsst.ts.standardscripts.auxtel.latiss import LATISS

import lsst.daf.persistence as dafPersist

# Source detection libraries
from lsst.meas.algorithms.detection import SourceDetectionTask

import lsst.afw.table as afwTable

from lsst.ip.isr.isrTask import IsrTask

# Import CWFS package
from lsst import cwfs
from lsst.cwfs.instrument import Instrument
from lsst.cwfs.algorithm import Algorithm
from lsst.cwfs.image import Image


class LatissCWFSAlign(salobj.BaseScript):
    """ Perform an optical alignment procedure of Auxiliary Telescope with
    the LATISS instrument (ATSpectrograph and ATCamera CSCs).

    Parameters
    ----------
    index : `int`
        Index of Script SAL component.

    Notes
    -----
    **Checkpoints**

    * intra: when taking intra focus image.
    * extra: when taking extra focus image.
    * cwfs: when running CWFS code.

    **Details**

    This script is used to perform measurements of the wavefront error, then
    propose hexapod offsets based on an input sensitivity matrix to minimize
    the errors.

    """

    __test__ = False  # stop pytest from warning that this is not a test

    def __init__(self, index, remotes=True):

        super().__init__(
            index=index, descr="Perform optical alignment procedure on LATISS data."
        )

        self.attcs = None
        self.latiss = None
        if remotes:
            self.attcs = ATTCS(self.domain)
            self.latiss = LATISS(self.domain)

        self.short_timeout = 5.0
        self.long_timeout = 30.0

        # Sensitivity matrix: mm of hexapod motion for nm of wfs. To figure out
        # the hexapod correction multiply
        self.sensitivity_matrix = [
            [1.0 / 131.0, 0.0, 0.0],
            [0.0, -1.0 / 131.0, 0.0],
            [0.0, 0.0, -1.0 / 4200.0],
        ]

        # Rotation matrix to take into account angle between camera and
        # boresight
        self.rotation_matrix = lambda angle: np.array(
            [
                [np.cos(np.radians(angle)), -np.sin(np.radians(angle)), 0.0],
                [np.sin(np.radians(angle)), np.cos(np.radians(angle)), 0.0],
                [0.0, 0.0, 1.0],
            ]
        )

        # Matrix to map hexapod offset to alt/az offset in the focal plane
        # units are arcsec/mm. X-axis is Elevation
        self.hexapod_offset_scale = [
            [60.0, 0.0, 0.0],
            [0.0, 60.0, 0.0],
            [0.0, 0.0, 0.0],
        ]

        # Angle between camera and boresight
        self.camera_rotation_angle = 0.0

        # The following attributes can be configured:
        #

        self.filter = "KPNO_406_828nm"
        self.grating = "empty_1"

        # exposure time for the intra/extra images (in seconds)
        self.exposure_time = 30.0

        # offset for the intra/extra images
        self._dz = None

        # butler data path.
        self.dataPath = "/mnt/dmcs/oods_butler_repo/repo/"

        #
        # end of configurable attributes

        self.pre_side = 300
        self._side = 192  # size for dz=1.5

        # angle between elevation axis and nasmyth2 rotator
        self.angle = None

        self.intra_visit_id = None
        self.extra_visit_id = None

        self.intra_exposure = None
        self.extra_exposure = None
        self.detection_exp = None

        self.source_selection_result = None
        self.cwfs_selected_sources = []

        self.I1 = []
        self.I2 = []
        self.fieldXY = [0.0, 0.0]

        self.inst = None
        self._binning = 2  # set binning of images to increase processing speed at the expense of resolution
        self.algo = None

        self.zern = None
        self.hexapod_corr = None

        self.data_pool_sleep = 5.0

        self.source_detection_config = SourceDetectionTask.ConfigClass()
        self.source_detection_config.thresholdValue = (
            30  # detection threshold after smoothing
        )
        self.source_detection_config.minPixels = self.pre_side
        self.source_detection_config.combinedGrow = True
        self.source_detection_config.nSigmaToGrow = 1.2

        self.isr_config = IsrTask.ConfigClass()
        self.isr_config.doLinearize = False
        self.isr_config.doBias = True
        self.isr_config.doFlat = False
        self.isr_config.doDark = False
        self.isr_config.doFringe = False
        self.isr_config.doDefect = True
        self.isr_config.doAddDistortionModel = False
        self.isr_config.doSaturationInterpolation = False
        self.isr_config.doSaturation = False
        self.isr_config.doWrite = False

    # define the method that sets the hexapod offset to create intra/extra focal images
    @property
    def dz(self):
        if self._dz is None:
            self.dz = 0.8
        return self._dz

    @property
    def binning(self):
        return self._binning

    @binning.setter
    def binning(self, value):
        self._binning = value
        self.dz = self.dz

    @property
    def side(self):
        return int(self._side * self.dz / 1.5)

    @dz.setter
    def dz(self, value):
        self._dz = float(value)
        self.log.info("Using binning factor of {}".format(self.binning))
        cwfs_config_template = """#Auxiliary Telescope parameters:
Obscuration 				0.3525
Focal_length (m)			21.6
Aperture_diameter (m)   		1.2
Offset (m)				{}
Pixel_size (m)			{}	
"""
        config_index = f"auxtel_latiss"
        path = Path(cwfs.__file__).resolve().parents[3].joinpath("data", config_index)
        if not path.exists():
            os.makedirs(path)
        dest = path.joinpath(f"{config_index}.param")
        with open(dest, "w") as fp:
            fp.write(
                cwfs_config_template.format(self._dz * 0.041, 10e-6 * self.binning)
            )
        self.inst = Instrument(config_index, int(self.side * 2 / self.binning))
        self.algo = Algorithm("exp", self.inst, 1)

    async def take_intra_extra(self):
        """ Take Intra/Extra images.

        Returns
        -------
        images_end_readout_evt: list
            List of endReadout event for the intra and extra images.

        """

        self.log.debug("Move to intra-focal position")

        await self.hexapod_offset(-self.dz)

        group_id = astropytime.Time.now().tai.isot

        intra_image = await self.latiss.take_engtest(
            exptime=self.exposure_time,
            n=1,
            group_id=group_id,
            filter=self.filter,
            grating=self.grating,
        )

        self.log.debug("Move to extra-focal position")

        await self.hexapod_offset(self.dz * 2.0)

        self.log.debug("Take extra-focal image")

        extra_image = await self.latiss.take_engtest(
            exptime=self.exposure_time,
            n=1,
            group_id=group_id,
            filter=self.filter,
            grating=self.grating,
        )

        azel = await self.attcs.atmcs.tel_mount_AzEl_Encoders.aget()
        nasmyth = await self.attcs.atmcs.tel_mount_Nasmyth_Encoders.aget()

        self.angle = np.mean(azel.elevationCalculatedAngle) + np.mean(
            nasmyth.nasmyth2CalculatedAngle
        )

        self.log.debug("Move hexapod to zero position")
        # This is performed such that the telescope is left in the
        # same position it was before running the script
        await self.hexapod_offset(-self.dz)

        self.intra_visit_id = int(intra_image[0])

        self.log.info(f"intraImage expId for target: {self.intra_visit_id}")

        self.extra_visit_id = int(extra_image[0])

        self.log.info(f"extraImage expId for target: {self.extra_visit_id}")

    async def hexapod_offset(self, offset):
        """

        Parameters
        ----------
        offset: `float`
             Focus offset to the hexapod in mm

        Returns
        -------

        """

        offset = {
            "m1": 0.0,
            "m2": 0.0,
            "x": 0.0,
            "y": 0.0,
            "z": offset,
            "u": 0.0,
            "v": 0.0,
        }

        self.attcs.athexapod.evt_positionUpdate.flush()
        await self.attcs.ataos.cmd_offset.set_start(
            **offset, timeout=self.short_timeout
        )
        await self.attcs.athexapod.evt_positionUpdate.next(
            flush=False, timeout=self.long_timeout
        )

    def get_isr_exposure(self, exp_id):
        """Get ISR exposure."""

        isrTask = IsrTask(config=self.isr_config)

        got_exposure = False

        ntries = 0

        data_ref = None
        while not got_exposure:
            butler = dafPersist.Butler(self.dataPath)
            try:
                data_ref = butler.dataRef("raw", **dict(expId=exp_id))
            except RuntimeError as e:
                self.log.warning(
                    f"Could not get intra focus image from butler. Waiting "
                    f"{self.data_pool_sleep}s and trying again."
                )
                time.sleep(self.data_pool_sleep)
                if ntries > 10:
                    raise e
                ntries += 1
            else:
                got_exposure = True

        if data_ref is not None:
            return isrTask.runDataRef(data_ref).exposure
        else:
            raise RuntimeError(f"No data ref for {exp_id}.")

    async def run_cwfs(self):
        """ Runs CWFS code on intra/extra focal images.

        Returns
        -------
        zern: list
            List of zernike coefficients.

        """

        # get event loop to run blocking tasks
        loop = asyncio.get_event_loop()
        executor = concurrent.futures.ThreadPoolExecutor()

        self.cwfs_selected_sources = []

        if self.intra_visit_id is None or self.extra_visit_id is None:
            self.log.warning(
                "Intra/Extra images not taken. Running take image sequence."
            )
            await self.take_intra_extra()
        else:
            self.log.info(
                f"Running cwfs in " f"{self.intra_visit_id}/{self.extra_visit_id}."
            )

        self.intra_exposure = await loop.run_in_executor(
            executor, self.get_isr_exposure, self.intra_visit_id
        )
        self.detection_exp = self.intra_exposure.clone()

        self.extra_exposure = await loop.run_in_executor(
            executor, self.get_isr_exposure, self.extra_visit_id
        )

        # Prepare detection exposure
        self.detection_exp.image.array += self.extra_exposure.image.array
        self.detection_exp.image.array -= np.median(self.detection_exp.image.array)

        await loop.run_in_executor(executor, self.select_cwfs_source)

        await loop.run_in_executor(executor, self.center_and_cut_images)
        if self.binning != 1:
            self.log.info("Running CWFS code USING BINNED IMAGES.")

        # Now we should be ready to run cwfs

        self.algo.reset(self.I1[0], self.I2[0])

        await loop.run_in_executor(
            executor, self.algo.runIt, self.inst, self.I1[0], self.I2[0], "onAxis"
        )
        # self.algo.runIt(self.inst, self.I1[0], self.I2[0], 'onAxis')

        self.zern = [
            self.algo.zer4UpNm[3],
            self.algo.zer4UpNm[4],
            self.algo.zer4UpNm[0],
        ]

        self.show_results()

    def select_cwfs_source(self):
        """
        """

        if self.detection_exp is None:
            raise RuntimeError(
                "No detection exposure define. Run take_intra_extra or"
                "manually define sources before running."
            )

        self.log.info("Running source detection algorithm")

        # create the output table for source detection
        schema = afwTable.SourceTable.makeMinimalSchema()
        source_detection_task = SourceDetectionTask(
            schema=schema, config=self.source_detection_config
        )

        tab = afwTable.SourceTable.make(schema)
        result = source_detection_task.run(tab, self.detection_exp, sigma=12.1)

        self.log.debug(
            f"Found {len(result)} sources. Selecting the brightest for CWFS analysis"
        )

        def sum_source_flux(_source, exp, min_size):
            bbox = _source.getFootprint().getBBox()
            if bbox.getDimensions().x < min_size or bbox.getDimensions().y < min_size:
                return -1.0
            return np.sum(exp[bbox].image.array)

        selected_source = 0
        flux_selected = sum_source_flux(
            result.sources[selected_source], self.detection_exp, min_size=self.pre_side
        )
        xy = [
            result.sources[selected_source].getFootprint().getCentroid().x,
            result.sources[selected_source].getFootprint().getCentroid().y,
        ]

        for i in range(1, len(result.sources)):
            flux_i = sum_source_flux(
                result.sources[i], self.detection_exp, min_size=self.pre_side
            )
            if flux_i > flux_selected:
                selected_source = i
                flux_selected = flux_i
                xy = [
                    result.sources[selected_source].getFootprint().getCentroid().x,
                    result.sources[selected_source].getFootprint().getCentroid().y,
                ]

        self.log.debug(f"Selected source {selected_source} @ [{xy[0]}, {xy[1]}]")

        self.source_selection_result = result

        if selected_source not in self.cwfs_selected_sources:
            self.cwfs_selected_sources.append(selected_source)
        else:
            self.log.warning(f"Source {selected_source} already selected.")

        return xy

    def center_and_cut_images(self):
        """ After defining sources for cwfs cut snippet for cwfs analysis.
        """

        # reset I1 and I2
        self.I1 = []
        self.I2 = []

        for selected_source in self.cwfs_selected_sources:
            # iter 1
            source = self.source_selection_result.sources[selected_source]
            bbox = source.getFootprint().getBBox()
            image = self.detection_exp[bbox].image.array

            im_filtered = medfilt(image, [3, 3])
            im_filtered -= int(np.median(im_filtered))
            mean = np.mean(im_filtered)
            im_filtered[im_filtered < mean] = 0.0
            im_filtered[im_filtered > mean] = 1.0
            ceny, cenx = np.array(
                ndimage.measurements.center_of_mass(im_filtered), dtype=int
            )

            side = self.side  # side length of image
            self.log.debug(
                f"Creating stamps of centroid [y,x] = [{ceny},{cenx}] with a side "
                f"length of {side} pixels"
            )

            intra_exp = self.intra_exposure[bbox].image.array
            extra_exp = self.extra_exposure[bbox].image.array
            intra_square = intra_exp[
                ceny - side : ceny + side, cenx - side : cenx + side
            ]
            extra_square = extra_exp[
                ceny - side : ceny + side, cenx - side : cenx + side
            ]

            # Bin the images
            if self.binning != 1:
                intra_square0 = copy.deepcopy(intra_square)
                extra_square0 = copy.deepcopy(extra_square)
                # get tuple array from shape array (which is a tuple) and make an integer
                new_shape = tuple(
                    np.asarray(
                        np.asarray(intra_square0.shape) / self.binning, dtype=np.int32
                    )
                )
                intra_square = self.rebin(intra_square0, new_shape)
                extra_square = self.rebin(extra_square0, new_shape)
                self.log.info(f"intra_square shape is {intra_square.shape}")
                self.log.info(f"extra_square shape is {extra_square.shape}")

            self.I1.append(Image(intra_square, self.fieldXY, Image.INTRA))
            self.I2.append(Image(extra_square, self.fieldXY, Image.EXTRA))

    def rebin(self, arr, new_shape):
        # rebins image to new form
        shape = (
            new_shape[0],
            arr.shape[0] // new_shape[0],
            new_shape[1],
            arr.shape[1] // new_shape[1],
        )
        return arr.reshape(shape).mean(-1).mean(1)

    def show_results(self):
        rot_zern = np.matmul(
            self.zern, self.rotation_matrix(self.angle + self.camera_rotation_angle)
        )
        hexapod_offset = np.matmul(rot_zern, self.sensitivity_matrix)
        tel_offset = np.matmul(hexapod_offset, self.hexapod_offset_scale)

        self.log.info(
            f"""==============================
Measured zernike coeficients: {self.zern}
De-rotated zernike coeficients: {rot_zern}
Hexapod offset: {hexapod_offset}
Telescope offsets: {tel_offset}
==============================
"""
        )

    @classmethod
    def get_schema(cls):
        schema_yaml = """
            $schema: http://json-schema.org/draft-07/schema#
            $id: https://github.com/lsst-ts/ts_standardscripts/auxtel/LatissCWFSAlign.yaml
            title: LatissCWFSAlign v1
            description: Configuration for LatissCWFSAlign Script.
            type: object
            properties:
              filter:
                description: Which filter to use when taking intra/extra focal images.
                type: string
                default: KPNO_406_828nm
              grating:
                description: Which grating to use when taking intra/extra focal images.
                type: string
                default: empty_1
              exposure_time:
                description: The exposure time to use when taking intra/extra focal images (sec).
                type: number
                default: 30.
              dz:
                description: De-focus to apply when acquiring the intra/extra focal images (mm).
                type: number
                default: 0.8
              dataPath:
                description: Path to the butler data repository.
                type: string
                default: /mnt/dmcs/oods_butler_repo/repo/
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

        # TODO: check that the filter/grating are mounted on the spectrograph
        self.filter = config.filter
        self.grating = config.grating

        # exposure time for the intra/extra images (in seconds)
        self.exposure_time = config.exposure_time

        # offset for the intra/extra images
        self.dz = config.dz

        # butler data path.
        self.dataPath = config.dataPath

    def set_metadata(self, metadata):
        # It takes about 300s to run the cwfs code, plus the two exposures
        metadata.duration = 300.0 + 2.0 * self.exposure_time
        metadata.filter = f"{self.filter},{self.grating}"

    async def run(self):
        pass
