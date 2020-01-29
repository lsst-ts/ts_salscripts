# This file is part of ts_standardscripts
#
# Developed for the LSST Data Management System.
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
import copy
import time
import yaml
import wget
import asyncio
import warnings
import logging

import concurrent.futures

from pathlib import Path

import numpy as np
from astropy import time as astropytime
from astropy.io import fits

from scipy import ndimage
from scipy.signal import medfilt
from scipy.ndimage.filters import gaussian_filter
from astropy.modeling import models, fitting

from lsst.ts import salobj
from lsst.ts.standardscripts.auxtel.attcs import ATTCS
from lsst.ts.standardscripts.auxtel.latiss import LATISS
from lsst.ts.idl.enums.Script import ScriptState

import lsst.daf.persistence as dafPersist

# Source detection libraries
from lsst.meas.algorithms.detection import SourceDetectionTask

# cosmic ray rejection
from lsst.pipe.tasks.characterizeImage import CharacterizeImageTask

import lsst.afw.table as afwTable

from operator import itemgetter
from lsst.ip.isr.isrTask import IsrTask

import matplotlib.pyplot as plt

# Import CWFS package
from lsst import cwfs
from lsst.cwfs.instrument import Instrument
from lsst.cwfs.algorithm import Algorithm
from lsst.cwfs.image import Image, readFile, aperture2image, showProjection
import lsst.cwfs.plots as plots


class LatissCWFSAlign(salobj.BaseScript):
    """ Perform an optical alignment procedure of Auxiliary Telescope with
    the GenericCamera CSC.

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

    This is what the script does:

    * TBD
    """

    __test__ = False  # stop pytest from warning that this is not a test

    def __init__(self, index):

        super().__init__(index=index,
                         descr="Perform optical alignment procedure on LATISS data.")

        self.attcs = ATTCS(self.domain)
        self.latiss = LATISS(self.domain)

        self.short_timeout = 5.
        self.long_timeout = 30.

        # Sensitivity matrix: mm of hexapod motion for nm of wfs. To figure out
        # the hexapod correction multiply
        self.sensitivity_matrix = [[-1./131., 0., 0.],
                                   [0., 1./131., 0.],
                                   [0., 0., -1./4200.]]

        # Rotation matrix to take into account angle between camera and
        # boresight
        self.rotation_matrix = lambda angle: np.array([
            [np.cos(np.radians(angle)), -np.sin(np.radians(angle)), 0.],
            [np.sin(np.radians(angle)), np.cos(np.radians(angle)), 0.],
            [0., 0., 1.]])

        # Matrix to map hexapod offset to alt/az offset units are arcsec/mm
        self.hexapod_offset_scale = [[60., 0., 0.],
                                     [0., 60., 0.],
                                     [0., 0., 0.]]  # arcsec/mm (x-axis is elevation)

        # Angle between camera and bore-sight
        self.camera_rotation_angle = 20.

        self.filter = 'KPNO_406_828nm'
        self.grating = 'empty_1'

        # exposure time for the intra/extra images (in seconds)
        self.exposure_time = 30.

        # offset for the intra/extra images
        self._dz = None

        self.pre_side = 300
        self.side = 192  # size for dz=1.5

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
        self.algo = None

        self.zern = None
        self.hexapod_corr = None

        self.dataPath = '/mnt/dmcs/oods_butler_repo/repo/'

    @property
    def dz(self):
        if self._dz is None:
            self.dz = 0.8
        return self._dz

    @dz.setter
    def dz(self, value):
        self._dz = float(value)
        cwfs_config_template = """#Auxiliary Telescope parameters:
Obscuration 				0.3525
Focal_length (m)			21.6
Aperture_diameter (m)   		1.2
Offset (m)				{}
Pixel_size (m)				10.0e-6
"""
        config_index = f"auxtel_{self.index}"
        path = Path(cwfs.__file__).resolve().parents[3].joinpath("data", config_index)
        if not path.exists():
            os.makedirs(path)
        dest = path.joinpath(f"{config_index}.param")
        with open(dest, "w") as fp:
            fp.write(cwfs_config_template.format(self._dz * 0.041))
        self.inst = Instrument(config_index, int(self.side * self._dz / 1.5))
        self.algo = Algorithm('exp', self.inst, 1)

    async def take_intra_extra(self):
        """ Take Intra/Extra images.

        Returns
        -------
        images_end_readout_evt: list
            List of endReadout event for the intra and extra images.

        """

        self.log.debug('Move to intra-focal position')

        await self.hexapod_offset(-self.dz)

        group_id = astropytime.Time.now().tai.isot

        intra_image = await self.latiss.take_image(exptime=self.exp_time, shutter=True,
                                                   image_type='OBJECT', group_id=group_id,
                                                   filter=self.filter, grating=self.grating)

        self.log.debug('Move to extra-focal position')

        await self.hexapod_offset(self.dz * 2.)

        self.log.debug('Take extra-focal image')

        extra_image = await self.latiss.take_image(exptime=self.exp_time, shutter=True,
                                                   image_type='OBJECT', group_id=group_id,
                                                   filter=self.filter, grating=self.grating)

        azel = await self.attcs.atmcs.tel_mount_AzEl_Encoders.aget()
        nasmyth = await self.attcs.atmcs.tel_mount_Nasmyth_Encoders.aget()

        self.angle = np.mean(azel.elevationCalculatedAngle)+np.mean(nasmyth.nasmyth2CalculatedAngle)

        self.log.debug("Move hexapod to zero position")

        await self.hexapod_offset(-self.dz)

        # parse out visitID from filename -
        # (Patrick comment) this is highly annoying
        _, _, i_prefix, i_suffix = intra_image.imageName.split("_")

        self.intra_visit_id = int((i_prefix + i_suffix[1:]))
        self.log.info(message="intraImage visitID for target: {}".format(self.intra_visit_id))

        _, _, e_prefix, e_suffix = extra_image.imageName.split("_")

        self.extra_visit_id = int((e_prefix + e_suffix[1:]))
        self.log.info(message="extraImage visitID for target: {}".format(self.extra_visit_id))

    async def hexapod_offset(self, offset):
        """

        Parameters
        ----------
        offset
        use_ataos

        Returns
        -------

        """

        offset = {'m1': 0.0,
                  'm2': 0.0,
                  'x': 0.0,
                  'y': 0.0,
                  'z': offset,
                  'u': 0.0,
                  'v': 0.0
                  }

        self.attcs.athexapod.evt_positionUpdate.flush()
        await self.attcs.ataos.cmd_offset.set_start(**offset,
                                                    timeout=self.short_timeout)
        await self.attcs.athexapod.evt_positionUpdate.next(flush=False,
                                                           timeout=self.long_timeout)

    async def run_cwfs(self):
        """ Runs CWFS code on intra/extra focal images.

        Returns
        -------
        zern: list
            List of zernike coefficients.

        """

        if self.intra_visit_id is None or self.extra_visit_id is None:
            self.log.warning("Intra/Extra images not taken. Running take image sequence.")
            await self.take_intra_extra()
        else:
            self.log.info(f"Running cwfs in "
                          f"{self.intra_visit_id}/{self.extra_visit_id}.")

        isrConfig = IsrTask.ConfigClass()
        isrConfig.doLinearize = False
        isrConfig.doBias = True
        isrConfig.doFlat = False
        isrConfig.doDark = False
        isrConfig.doFringe = False
        isrConfig.doDefect = False
        isrConfig.doAddDistortionModel = False
        isrConfig.doWrite = False

        isrTask = IsrTask(config=isrConfig)

        got_intra = False

        while not got_intra:
            butler = dafPersist.Butler(self.dataPath)
            try:
                data_ref = butler.dataRef('raw', **dict(visit=self.intra_visit_id))
            except RuntimeError:
                self.log.warning(f"Could not get intra focus image from butler. Waiting "
                                 f"{self.data_pool_sleep}s and trying again.")
                await asyncio.sleep(self.data_pool_sleep)
            else:
                got_intra = True
            self.intra_exposure = isrTask.runDataRef(data_ref).exposure
            self.detection_exp = isrTask.runDataRef(data_ref).exposure

        got_extra = False
        while not got_extra:
            butler = dafPersist.Butler(self.dataPath)
            try:
                data_ref = butler.dataRef('raw', **dict(visit=self.extra_visit_id))
            except RuntimeError:
                self.log.warning(f"Could not get extra focus image from butler. Waiting "
                                 f"{self.data_pool_sleep}s and trying again.")
                await asyncio.sleep(self.data_pool_sleep)
            else:
                got_extra = True
            self.extra_exposure = isrTask.runDataRef(data_ref).exposure

        # Prepare detection exposure
        self.detection_exp.image.array += self.extra_exposure.image.array

        await self.select_cwfs_source()

        await self.center_and_cut_image()

        # Now we should be ready to run cwfs

        self.log.info("Running CWFS code.")
        self.algo.runIt(self.inst, self.I1[0], self.I2[0], 'onAxis')

        self.zern = [self.algo.zer4UpNm[3],
                     self.algo.zer4UpNm[4],
                     self.algo.zer4UpNm[0]]
        rot_zern = np.matmul(self.zern,
                             self.rotation_matrix(self.angle+self.camera_rotation_angle))
        hexapod_offset = np.matmul(rot_zern,
                                   self.sensitivity_matrix)
        tel_offset = np.matmul(hexapod_offset, self.hexapod_offset_scale)

        self.log.info(f"Measured zernike coeficients: {self.zern}")
        self.log.info(f"De-rotated zernike coeficients: {rot_zern}")
        self.log.info(f"Hexapod offset: {hexapod_offset}")
        self.log.info(f"Telescope offsets: {tel_offset}")

    async def select_cwfs_source(self):
        """
        """

        if self.detection_exp is None:
            raise RuntimeError("No detection exposure define. Run take_intra_extra or"
                               "manually define sources before running.")

        self.log.info("Running source detection algorithm")

        # create the output table for source detection
        schema = afwTable.SourceTable.makeMinimalSchema()
        config = SourceDetectionTask.ConfigClass()
        config.thresholdValue = 10  # detection threshold after smoothing
        source_detection_task = SourceDetectionTask(schema=schema, config=config)
        tab = afwTable.SourceTable.make(schema)
        result = source_detection_task.run(tab, self.detection_exp, sigma=12.1)

        self.log.debug(f"Found {len(result)} sources. Selecting the brightest for CWFS analysis")

        def sum_source_flux(_source, exp, min_size):
            bbox = _source.getFootprint().getBBox()
            if bbox.getDimensions().x < min_size or bbox.getDimensions().y < min_size:
                return -1.
            return np.sum(exp[bbox].image.array)

        selected_source = 0
        flux_selected = sum_source_flux(result.sources[selected_source], self.detection_exp)
        xy = [result.sources[selected_source].getFootprint().getCentroid().x,
              result.sources[selected_source].getFootprint().getCentroid().y]

        for i in range(1, len(result.sources)):
            flux_i = sum_source_flux(result.sources[i], self.detection_exp)
            if flux_i > flux_selected:
                selected_source = i
                flux_selected = flux_i
                xy = [result.sources[selected_source].getFootprint().getCentroid().x,
                      result.sources[selected_source].getFootprint().getCentroid().y]

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
            source = self.result.sources[selected_source]
            bbox = source.getFootprint().getBBox()
            image = self.detection_exp[bbox].image.array

            im_filtered = medfilt(image, [3, 3])
            im_filtered -= int(np.median(im_filtered))
            mean = np.mean(im_filtered)
            im_filtered[im_filtered < mean] = 0.
            im_filtered[im_filtered > mean] = 1.
            ceny, cenx = np.array(ndimage.measurements.center_of_mass(im_filtered), dtype=int)

            side = int(self.side * self.dz / 1.5)  # side length of image
            self.log.debug(f"Creating stamps of centroid [y,x] = [{ceny},{cenx}] with a side "
                           f"length of {side} pixels")

            intra_exp = self.intra_exposure[bbox].image.array
            extra_exp = self.extra_exposure[bbox].image.array
            intra_square = intra_exp[ceny - side:ceny + side, cenx - side:cenx + side]
            extra_square = extra_exp[ceny - side:ceny + side, cenx - side:cenx + side]

            self.I1.append(Image(intra_square, self.fieldXY, Image.INTRA))
            self.I2.append(Image(extra_square, self.fieldXY, Image.EXTRA))


