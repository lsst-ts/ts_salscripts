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

__all__ = ["BaseMakeBias"]

import yaml
import abc
import json
import asyncio

from lsst.ts import salobj


class BaseMakeBias(salobj.BaseScript, metaclass=abc.ABCMeta):
    """ Base class for taking biases and construct a master bias.

    Parameters
    ----------
    index : `int`
        SAL index of this script
    """

    def __init__(self, index, descr):
        super().__init__(index=index, descr=descr)
        # cpCombine + ISR per image with -j 1 at the summit [sec]
        # See DM-30483
        self.estimated_process_time = 45
        self.ocps = salobj.Remote(domain=self.domain, name="OCPS")

    @property
    @abc.abstractmethod
    def camera(self):
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def instrument_name(self):
        """String with instrument name for pipeline task"""
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def image_in_oods(self):
        """Archiver"""
        raise NotImplementedError()

    @classmethod
    def get_schema(cls):
        schema = """
        $schema: http://json-schema.org/draft-07/schema#
        $id: https://github.com/lsst-ts/ts_externalscripts/blob/master/python/lsst/ts/\
            externalscripts/maintel/make_comcam_bias.py
        title: BaseMakeBias v1
        description: Configuration for BaseMakeBias.
        type: object
        properties:
            n_bias:
                type: integer
                default: 1
                description: number of biases to take

            detectors:
                type: string
                items:
                    type: integer
                    minItems: 1
                default: (0)
                descriptor: Detector IDs

            input_collections:
                type: string
                descriptor: Input collections to pass to the bias pipetask.

            calib_dir:
                type: string
                descriptor: path to the calib directory for the bias when certifying it.

            repo:
                type: string
                descriptor: Butler repository. /repo/main is the default at NCSA;\
                    it migth be different at the summit.
                default: /repo/main

            max_counter_archiver_check:
                type: integer
                default: 1000
                descriptor: Maximum number of loops to wait for confirmation that \
                    images taken were archived and available to butler.

            oods_timeout:
                type: integer
                default 600
                descriptor: Timeout value, in seconds, for OODS.

        additionalProperties: false
        required: [input_collections, calib_dir, repo]
        """
        return yaml.safe_load(schema)

    async def configure(self, config):
        """Configure the script.

        Parameters
        ----------
        config: `types.SimpleNamespace`
            Configuration data. See `get_schema` for information about data
            structure.
        """
        # Log information about the configuration

        self.log.debug(
            f"n_bias: {config.n_bias}, detectors: {config.detectors}, "
            f"instrument: {self.instrument_name} "
        )

        self.config = config

    def set_metadata(self, metadata):
        """Set estimated duration of the script.
        """
        metadata.duration = self.config.n_bias*(self.camera.read_out_time + self.estimated_process_time)

    async def arun(self, checkpoint=False):

        # Take config.n_biases biases, and return a list of IDs
        if checkpoint:
            await self.checkpoint(f"Taking {self.config.n_bias} biases")

        self.image_in_oods.flush()
        exposures = tuple(await self.camera.take_bias(self.config.n_bias))

        if checkpoint:
            # Bias IDs
            await self.checkpoint(f"Biases taken: {exposures}")

        # did the images get archived and are they available to the butler?
        n_detectors = len(tuple(map(int, self.config.detectors[1:-1].split(','))))
        # exps are of the form, e.g., 2021070800019
        exposure_set = set()
        for exp in exposures:
            obs_day = f"{exp}"[:8]
            temp = int(f"{exp}"[8:])
            # See example of ack.obsid below
            seq_num = f"{temp:06}"
            obs_id = obs_day + "_" + seq_num
            exposure_set.add(obs_id)

        images_remaining = len(exposure_set)*n_detectors
        max_counter = self.config.max_counter_archiver_check
        counter = 0
        while images_remaining > 0:
            ack = await self.image_in_oods.next(flush=False, timeout=self.config.oods_timeout)
            # ack.obsid is of the form, e.g., CC_O_20210708_000019
            if f"{ack.obsid[-15:]}" in exposure_set:
                images_remaining -= 1
                counter = 0
            if counter == max_counter:
                self.log.warning(f"Maximum number of loops ({max_counter}) reached while waiting "
                                 "for image in archiver")
                break
            counter += 1

        self.ocps.evt_job_result.flush()

        # Now run the bias pipetask via the OCPS
        ack = await self.ocps.cmd_execute.set_start(
            wait_done=False, pipeline="${CP_PIPE_DIR}/pipelines/cpBias.yaml", version="",
            config=f"-j 8 -i {self.config.input_collections} --register-dataset-types -c isr:doDefect=False "
                   f"-c isr:doLinearize=False -c isr:doCrosstalk=False "
                   f"-c isr:overscan.fitType='MEDIAN_PER_ROW'",
            data_query=f"instrument='{self.instrument_name}' AND"
                       f" detector IN {self.config.detectors} AND exposure IN {exposures}"
        )
        if ack.ack != salobj.SalRetCode.CMD_ACK:
            ack.print_vars()

        # Wait for the in-progress acknowledgement with the job identifier.
        ack = await self.ocps.cmd_execute.next_ackcmd(ack, wait_done=False)
        self.log.debug('Received acknowledgement of ocps command')

        ack.print_vars()
        job_id = json.loads(ack.result)["job_id"]

        # Wait for the command completion acknowledgement.
        ack = await self.ocps.cmd_execute.next_ackcmd(ack)
        self.log.debug('Received command completion acknowledgement from ocps')
        if ack.ack != salobj.SalRetCode.CMD_COMPLETE:
            ack.print_vars()
        # Wait for the job result message that matches the job id we're
        # interested in ignoring any others (from other remotes).
        # This obviously needs to follow the first acknowledgement
        # (that returns the, job id) but might as well wait for the second.
        while True:
            msg = await self.ocps.evt_job_result.next(flush=False, timeout=self.config.oods_timeout)
            response = json.loads(msg.result)
            if response["jobId"] == job_id:
                break

        self.log.info(f"Final status: {response}")

        # Certify the bias, if the job completed successfully
        if not response['phase'] == 'completed':
            raise RuntimeError(f"Bias creation not completed successfully: {response['phase']}")
        else:
            self.log.info("Certifying bias: ")
            # certify the bias
            REPO = self.config.repo
            # This is the output collection where the OCPS puts the biases
            BIAS_DIR = f"u/ocps/{job_id}"
            CAL_DIR = self.config.calib_dir
            cmd = (f"butler certify-calibrations {REPO} {BIAS_DIR} {CAL_DIR} "
                   "--begin-date 1980-01-01 --end-date 2050-01-01 bias")
            self.log.info(cmd)

            process = await asyncio.create_subprocess_shell(cmd)
            stdout, stderr = await process.communicate()
            self.log.debug(f"Process returned: {process.returncode}")
            if process.returncode != 0:
                self.log.debug(stdout)
                self.log.error(stderr)
                raise RuntimeError("Error running command for certifying bias.")

    async def run(self):
        """"""
        await self.arun(checkpoint=True)
