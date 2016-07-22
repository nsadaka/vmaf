__copyright__ = "Copyright 2016, Netflix, Inc."
__license__ = "Apache, Version 2.0"

import sys
import subprocess
import re
import hashlib

import numpy as np

import config
from core.executor import Executor
from core.result import Result
from core.feature_assembler import FeatureAssembler
from core.train_test_model import TrainTestModel
from core.feature_extractor import MomentFeatureExtractor


class QualityRunner(Executor):
    """
    QualityRunner takes in a list of assets, and run quality assessment on
    them, and return a list of corresponding results. A QualityRunner must
    specify a unique type and version combination (by the TYPE and VERSION
    attribute), so that the Result generated by it can be identified.

    There are two ways to create a derived class of QualityRunner:

    a) Call a command-line exectuable directly, very similar to what
    FeatureExtractor does. You must:
        1) Override TYPE and VERSION
        2) Override _run_and_generate_log_file(self, asset), which call a
        command-line executable and generate quality scores in a log file.
        3) Override _get_quality_scores(self, asset), which read the quality
        scores from the log file, and return the scores in a dictionary format.
        4) If necessary, override _remove_log(self, asset) if
        Executor._remove_log(self, asset) doesn't work for your purpose
        (sometimes the command-line executable could generate output log files
        in some different format, like multiple files).
    For an example, follow PsnrQualityRunner.

    b) Override the Executor._run_on_asset(self, asset) method to bypass the
    regular routine, but instead, in the method construct a FeatureAssembler
    (which calls a FeatureExtractor (or many) and assembles a list of features,
    followed by using a TrainTestModel (pre-trained somewhere else) to predict
    the final quality score. You must:
        1) Override TYPE and VERSION
        2) Override _run_on_asset(self, asset), which runs a FeatureAssembler,
        collect a feature vector, run TrainTestModel.predict() on it, and
        return a Result object (in this case, both Executor._run_on_asset(self,
        asset) and QualityRunner._read_result(self, asset) get bypassed.
        3) Override _remove_log(self, asset) by redirecting it to the
        FeatureAssembler.
        4) Override _remove_result(self, asset) by redirecting it to the
        FeatureAssembler.
    For an example, follow VmafQualityRunner.
    """

    def _read_result(self, asset):
        result = {}
        result.update(self._get_quality_scores(asset))
        executor_id = self.executor_id
        if self.optional_dict is not None:
            executor_id += '_{}'.format(
                '_'.join(
                    map(lambda k: '{k}_{v}'.format(k=k,v=self.optional_dict[k]),
                        sorted(self.optional_dict.keys()))
                )
            ) # include optional_dict info in executor_id for result store
        return Result(asset, executor_id, result)

    @classmethod
    def get_scores_key(cls):
        return cls.TYPE + '_scores'

    @classmethod
    def get_score_key(cls):
        return cls.TYPE + '_score'


class PsnrQualityRunner(QualityRunner):

    TYPE = 'PSNR'
    VERSION = '1.0'

    PSNR = config.ROOT + "/feature/psnr"

    def _run_and_generate_log_file(self, asset):
        # routine to call the command-line executable and generate quality
        # scores in the log file.

        log_file_path = self._get_log_file_path(asset)

        # run VMAF command line to extract features, 'APPEND' result (since
        # super method already does something
        quality_width, quality_height = asset.quality_width_height
        psnr_cmd = "{psnr} {yuv_type} {ref_path} {dis_path} {w} {h} >> {log_file_path}" \
        .format(
            psnr=self.PSNR,
            yuv_type=asset.yuv_type,
            ref_path=asset.ref_workfile_path,
            dis_path=asset.dis_workfile_path,
            w=quality_width,
            h=quality_height,
            log_file_path=log_file_path,
        )

        if self.logger:
            self.logger.info(psnr_cmd)

        subprocess.call(psnr_cmd, shell=True)

    def _get_quality_scores(self, asset):
        # routine to read the quality scores from the log file, and return
        # the scores in a dictionary format.

        log_file_path = self._get_log_file_path(asset)

        psnr_scores = []
        counter = 0
        with open(log_file_path, 'rt') as log_file:
            for line in log_file.readlines():
                mo = re.match(r"psnr: ([0-9]+) ([0-9.-]+)", line)
                if mo:
                    cur_idx = int(mo.group(1))
                    assert cur_idx == counter
                    psnr_scores.append(float(mo.group(2)))
                    counter += 1

        assert len(psnr_scores) != 0

        scores_key = self.get_scores_key()
        quality_result = {
            scores_key:psnr_scores
        }
        return quality_result


class VmafLegacyQualityRunner(QualityRunner):

    TYPE = 'VMAF_legacy'
    #VERSION = '1.1'
    VERSION = '1.2' # update since adm, ansnr, vif feature computation has changed

    FEATURE_ASSEMBLER_DICT = {'VMAF_feature': 'all'}

    FEATURE_RESCALE_DICT = {'VMAF_feature_vif_scores': (0.0, 1.0),
                            'VMAF_feature_adm_scores': (0.4, 1.0),
                            'VMAF_feature_ansnr_scores': (10.0, 50.0),
                            'VMAF_feature_motion_scores': (0.0, 20.0)}

    SVM_MODEL_FILE = config.ROOT + "/resource/model/model_V8a.model"

    # model_v8a.model is trained with customized feature order:
    SVM_MODEL_ORDERED_SCORES_KEYS = ['VMAF_feature_vif_scores',
                                     'VMAF_feature_adm_scores',
                                     'VMAF_feature_ansnr_scores',
                                     'VMAF_feature_motion_scores']

    sys.path.append(config.ROOT + "/libsvm/python")
    import svmutil

    def _get_vmaf_feature_assembler_instance(self, asset):
        vmaf_fassembler = FeatureAssembler(
            feature_dict=self.FEATURE_ASSEMBLER_DICT,
            feature_option_dict=None,
            assets=[asset],
            logger=self.logger,
            fifo_mode=self.fifo_mode,
            delete_workdir=self.delete_workdir,
            result_store=self.result_store
        )
        return vmaf_fassembler

    def _run_on_asset(self, asset):
        # Override Executor._run_on_asset(self, asset), which runs a
        # FeatureAssembler, collect a feature vector, run
        # TrainTestModel.predict() on it, and return a Result object
        # (in this case, both Executor._run_on_asset(self, asset) and
        # QualityRunner._read_result(self, asset) get bypassed.

        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.run()
        feature_result = vmaf_fassembler.results[0]

        # =====================================================================

        # SVR predict
        model = self.svmutil.svm_load_model(self.SVM_MODEL_FILE)

        ordered_scaled_scores_list = []
        for scores_key in self.SVM_MODEL_ORDERED_SCORES_KEYS:
            scaled_scores = self._rescale(feature_result[scores_key],
                                          self.FEATURE_RESCALE_DICT[scores_key])
            ordered_scaled_scores_list.append(scaled_scores)

        scores = []
        for score_vector in zip(*ordered_scaled_scores_list):
            vif, adm, ansnr, motion = score_vector
            xs = [[vif, adm, ansnr, motion]]
            score = self.svmutil.svm_predict([0], xs, model)[0][0]
            score = self._post_correction(motion, score)
            scores.append(score)

        result_dict = {}
        # add all feature result
        result_dict.update(feature_result.result_dict)
        # add quality score
        result_dict[self.get_scores_key()] = scores

        return Result(asset, self.executor_id, result_dict)

    def _post_correction(self, motion, score):
        # post-SVM correction
        if motion > 12.0:
            val = motion
            if val > 20.0:
                val = 20
            score *= ((val - 12) * 0.015 + 1)
        if score > 100.0:
            score = 100.0
        elif score < 0.0:
            score = 0.0
        return score

    @classmethod
    def _rescale(cls, vals, lower_upper_bound):
        lower_bound, upper_bound = lower_upper_bound
        vals = np.double(vals)
        vals = np.clip(vals, lower_bound, upper_bound)
        vals = (vals - lower_bound) / (upper_bound - lower_bound)
        return vals

    # override
    def _remove_result(self, asset):
        # Override Executor._remove_result(self, asset) by redirecting it to the
        # FeatureAssembler.

        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.remove_results()


class VmafQualityRunner(QualityRunner):

    TYPE = 'VMAF'

    # VERSION = '0.1' # using model nflxall_vmafv1.pkl, VmafFeatureExtractor VERSION 0.1
    # DEFAULT_MODEL_FILEPATH = config.ROOT + "/resource/model/nflxall_vmafv1.pkl" # trained with resource/param/vmaf_v1.py on private/resource/dataset/NFLX_dataset.py (30 subjects)

    # VERSION = '0.2' # using model nflxall_vmafv2.pkl, VmafFeatureExtractor VERSION 0.2.1
    # DEFAULT_MODEL_FILEPATH = config.ROOT + "/resource/model/nflxall_vmafv2.pkl" # trained with resource/param/vmaf_v2.py on private/resource/dataset/NFLX_dataset.py (30 subjects)

    # VERSION = '0.3' # using model nflxall_vmafv3.pkl, VmafFeatureExtractor VERSION 0.2.1
    # DEFAULT_MODEL_FILEPATH = config.ROOT + "/resource/model/nflxall_vmafv3.pkl" # trained with resource/param/vmaf_v3.py on private/resource/dataset/NFLX_dataset.py (30 subjects)

    # VERSION = '0.3.1' # using model nflxall_vmafv3.pkl, VmafFeatureExtractor VERSION 0.2.1, NFLX_dataset with 26 subjects (last 4 outliers removed)
    # DEFAULT_MODEL_FILEPATH = config.ROOT + "/resource/model/nflxall_vmafv3a.pkl" # trained with resource/param/vmaf_v3.py on private/resource/dataset/NFLX_dataset.py (26 subjects)

    VERSION = '0.3.2'  # using model nflxall_vmafv4.pkl, VmafFeatureExtractor VERSION 0.2.2, NFLX_dataset with 26 subjects (last 4 outliers removed)
    DEFAULT_MODEL_FILEPATH = config.ROOT + "/resource/model/nflxall_vmafv4.pkl"  # trained with resource/param/vmaf_v4.py on private/resource/dataset/NFLX_dataset.py (26 subjects)

    DEFAULT_FEATURE_DICT = {'VMAF_feature': ['vif', 'adm', 'motion', 'ansnr']}

    def _get_vmaf_feature_assembler_instance(self, asset):

        # load TrainTestModel only to retrieve its 'feature_dict' extra info
        model = self._load_model()
        feature_dict = model.get_appended_info('feature_dict')
        if feature_dict is None:
            feature_dict = self.DEFAULT_FEATURE_DICT

        vmaf_fassembler = FeatureAssembler(
            feature_dict=feature_dict,
            feature_option_dict=None,
            assets=[asset],
            logger=self.logger,
            fifo_mode=self.fifo_mode,
            delete_workdir=self.delete_workdir,
            result_store=self.result_store
        )
        return vmaf_fassembler

    def _run_on_asset(self, asset):
        # Override Executor._run_on_asset(self, asset), which runs a
        # FeatureAssembler, collect a feature vector, run
        # TrainTestModel.predict() on it, and return a Result object
        # (in this case, both Executor._run_on_asset(self, asset) and
        # QualityRunner._read_result(self, asset) get bypassed.

        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.run()
        feature_result = vmaf_fassembler.results[0]

        xs = TrainTestModel.get_perframe_xs_from_result(feature_result)

        model = self._load_model()

        ys_pred = model.predict(xs)

        # 'score_clip'
        ys_pred = self.clip_score(model, ys_pred)

        result_dict = {}
        # add all feature result
        result_dict.update(feature_result.result_dict)
        # add quality score
        result_dict[self.get_scores_key()] = ys_pred

        return Result(asset, self.executor_id, result_dict)

    @staticmethod
    def set_clip_score(model, score_clip):
        """
        Enable post processing: clip final quality score within e.g. [0, 100]
        :param model:
        :param score_clip:
        :return:
        """
        model.append_info('score_clip', score_clip)

    @staticmethod
    def clip_score(model, ys_pred):
        """
        Do post processing: clip final quality score within e.g. [0, 100]
        :param model:
        :param ys_pred:
        :return:
        """
        score_clip = model.get_appended_info('score_clip')
        if score_clip is not None:
            lb, ub = score_clip
            ys_pred = np.clip(ys_pred, lb, ub)

        return ys_pred

    @staticmethod
    def warp_score(model, xs, ys_pred):
        """
        Do post processing: for pixel mean (luma) below certain threshold
        (i.e. dis1st_thr, or threshold for distorted video's first moment),
        warp the score towards highest score (e.g. 100).
        :param model:
        :param xs:
        :param ys_pred:
        :return:
        """
        score_clip = model.get_appended_info('score_clip')
        dis1st_thr = model.get_appended_info('dis1st_thr')
        dis1st_score_key = MomentFeatureExtractor.get_score_key('dis1st')
        if dis1st_thr is not None \
                and score_clip is not None \
                and dis1st_score_key in xs:
            y_max = score_clip[1]
            dis1sts = xs[dis1st_score_key]
            assert len(dis1sts) == len(ys_pred)
            ys_pred = map(
                lambda (y, dis1st): y_max - dis1st * (y_max - y)
                                            / dis1st_thr if dis1st < dis1st_thr else y,
                zip(ys_pred, dis1sts)
            )
        return ys_pred

    def _load_model(self):
        model_filepath = self.optional_dict['model_filepath'] \
            if (self.optional_dict is not None
                and 'model_filepath' in self.optional_dict
                and self.optional_dict['model_filepath'] is not None
                ) \
            else self.DEFAULT_MODEL_FILEPATH
        model = TrainTestModel.from_file(model_filepath, self.logger)
        return model

    def _remove_result(self, asset):
        # Override Executor._remove_result(self, asset) by redirecting it to the
        # FeatureAssembler.

        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.remove_results()
