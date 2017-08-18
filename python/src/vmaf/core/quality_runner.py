from abc import ABCMeta, abstractmethod
import re
from xml.etree import ElementTree

import numpy as np

from vmaf.config import VmafConfig
from vmaf import svmutil, ExternalProgram, ExternalProgramCaller
from vmaf.core.executor import Executor
from vmaf.core.result import Result
from vmaf.core.feature_assembler import FeatureAssembler
from vmaf.core.train_test_model import TrainTestModel
from vmaf.core.feature_extractor import SsimFeatureExtractor, MsSsimFeatureExtractor, \
    VmafFeatureExtractor, StrredFeatureExtractor
from vmaf.tools.misc import run_process

__copyright__ = "Copyright 2016-2017, Netflix, Inc."
__license__ = "Apache, Version 2.0"

class QualityRunner(Executor):
    """
    QualityRunner takes in a list of assets, and run quality assessment on
    them, and return a list of corresponding results. A QualityRunner must
    specify a unique type and version combination (by the TYPE and VERSION
    attribute), so that the Result generated by it can be identified and
    stored by ResultStore class.

    There are two ways to create a derived class of QualityRunner:

    a) Call a command-line exectuable directly, very similar to what
    FeatureExtractor does. You must:
        1) Override TYPE and VERSION
        2) Override _generate_result(self, asset), which call a
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

    __metaclass__ = ABCMeta

    @abstractmethod
    def _get_quality_scores(self, asset):
        raise NotImplementedError

    def _read_result(self, asset):
        result = {}
        result.update(self._get_quality_scores(asset))
        executor_id = self.executor_id
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

    def _generate_result(self, asset):
        # routine to call the command-line executable and generate quality
        # scores in the log file.

        quality_width, quality_height = asset.quality_width_height
        log_file_path = self._get_log_file_path(asset)

        yuv_type = self._get_workfile_yuv_type(asset)
        ref_path = asset.ref_workfile_path
        dis_path = asset.dis_workfile_path
        w = quality_width
        h = quality_height
        logger = self.logger

        ExternalProgramCaller.call_psnr(yuv_type, ref_path, dis_path, w, h, log_file_path, logger)

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

    # VERSION = '1.1'
    VERSION = 'F' + VmafFeatureExtractor.VERSION + '-1.1'

    FEATURE_ASSEMBLER_DICT = {'VMAF_feature': 'all'}

    FEATURE_RESCALE_DICT = {'VMAF_feature_vif_scores': (0.0, 1.0),
                            'VMAF_feature_adm_scores': (0.4, 1.0),
                            'VMAF_feature_ansnr_scores': (10.0, 50.0),
                            'VMAF_feature_motion_scores': (0.0, 20.0)}

    SVM_MODEL_FILE = VmafConfig.model_path("model_V8a.model")

    # model_v8a.model is trained with customized feature order:
    SVM_MODEL_ORDERED_SCORES_KEYS = ['VMAF_feature_vif_scores',
                                     'VMAF_feature_adm_scores',
                                     'VMAF_feature_ansnr_scores',
                                     'VMAF_feature_motion_scores']

    def _get_quality_scores(self, asset):
        raise NotImplementedError

    def _generate_result(self, asset):
        raise NotImplementedError

    def _get_vmaf_feature_assembler_instance(self, asset):
        vmaf_fassembler = FeatureAssembler(
            feature_dict=self.FEATURE_ASSEMBLER_DICT,
            feature_option_dict=None,
            assets=[asset],
            logger=self.logger,
            fifo_mode=self.fifo_mode,
            delete_workdir=self.delete_workdir,
            result_store=self.result_store,
            optional_dict=None,
            optional_dict2=None,
            parallelize=False, # parallelization already in a higher level
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
        model = svmutil.svm_load_model(self.SVM_MODEL_FILE)

        ordered_scaled_scores_list = []
        for scores_key in self.SVM_MODEL_ORDERED_SCORES_KEYS:
            scaled_scores = self._rescale(feature_result[scores_key],
                                          self.FEATURE_RESCALE_DICT[scores_key])
            ordered_scaled_scores_list.append(scaled_scores)

        scores = []
        for score_vector in zip(*ordered_scaled_scores_list):
            vif, adm, ansnr, motion = score_vector
            xs = [[vif, adm, ansnr, motion]]
            score = svmutil.svm_predict([0], xs, model)[0][0]
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
        # Override Executor._remove_result by redirecting it to the
        # FeatureAssembler.

        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.remove_results()


class VmafQualityRunner(QualityRunner):

    TYPE = 'VMAF'

    # VERSION = '0.1' # using model nflxall_vmafv1.pkl, VmafFeatureExtractor VERSION 0.1
    # DEFAULT_MODEL_FILEPATH = VmafConfig.model_path("nflxall_vmafv1.pkl") # trained with resource/param/vmaf_v1.py on private/resource/dataset/NFLX_dataset.py (30 subjects)

    # VERSION = '0.2' # using model nflxall_vmafv2.pkl, VmafFeatureExtractor VERSION 0.2.1
    # DEFAULT_MODEL_FILEPATH = VmafConfig.model_path("nflxall_vmafv2.pkl") # trained with resource/param/vmaf_v2.py on private/resource/dataset/NFLX_dataset.py (30 subjects)

    # VERSION = '0.3' # using model nflxall_vmafv3.pkl, VmafFeatureExtractor VERSION 0.2.1
    # DEFAULT_MODEL_FILEPATH = VmafConfig.model_path("nflxall_vmafv3.pkl") # trained with resource/param/vmaf_v3.py on private/resource/dataset/NFLX_dataset.py (30 subjects)

    # VERSION = '0.3.1' # using model nflxall_vmafv3.pkl, VmafFeatureExtractor VERSION 0.2.1, NFLX_dataset with 26 subjects (last 4 outliers removed)
    # DEFAULT_MODEL_FILEPATH = VmafConfig.model_path("nflxall_vmafv3a.pkl") # trained with resource/param/vmaf_v3.py on private/resource/dataset/NFLX_dataset.py (26 subjects)

    # VERSION = '0.3.2'  # using model nflxall_vmafv4.pkl, VmafFeatureExtractor VERSION 0.2.2, NFLX_dataset with 26 subjects (last 4 outliers removed)
    # ALGO_VERSION = 0
    # DEFAULT_MODEL_FILEPATH = VmafConfig.model_path("nflxall_vmafv4.pkl")  # trained with resource/param/vmaf_v4.py on private/resource/dataset/NFLX_dataset.py (26 subjects)

    # using model vmaf_v0.6.1.pkl. VmafFeatureExtractor VERSION 0.2.4b.
    VERSION = 'F' + VmafFeatureExtractor.VERSION + '-0.6.1'
    ALGO_VERSION = 2

    # trained with resource/param/vmaf_v6.py on private/user/zli/resource/dataset/dataset/derived/vmafplusstudy_laptop_raw_generalandcornercase.py, MLER, y=x+17
    DEFAULT_MODEL_FILEPATH = VmafConfig.model_path("vmaf_v0.6.1.pkl")

    DEFAULT_FEATURE_DICT = {'VMAF_feature': ['vif', 'adm', 'motion', 'ansnr']} # for backward-compatible with older model only

    def _get_quality_scores(self, asset):
        raise NotImplementedError

    def _generate_result(self, asset):
        raise NotImplementedError

    def _get_vmaf_feature_assembler_instance(self, asset):

        # load TrainTestModel only to retrieve its 'feature_dict' extra info
        feature_dict = self._load_model(asset).get_appended_info('feature_dict')
        if feature_dict is None:
            feature_dict = self.DEFAULT_FEATURE_DICT

        vmaf_fassembler = FeatureAssembler(
            feature_dict=feature_dict,
            feature_option_dict=None,
            assets=[asset],
            logger=self.logger,
            fifo_mode=self.fifo_mode,
            delete_workdir=self.delete_workdir,
            result_store=self.result_store,
            optional_dict=None,
            optional_dict2=None,
            parallelize=False, # parallelization already in a higher level
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
        model = self._load_model(asset)
        xs = model.get_per_unit_xs_from_a_result(feature_result)

        if self.optional_dict is not None and 'disable_clip_score' in self.optional_dict:
            disable_clip_score = self.optional_dict['disable_clip_score']
        else:
            disable_clip_score = False

        if self.optional_dict is not None and 'enable_transform_score' in self.optional_dict:
            enable_transform_score = self.optional_dict['enable_transform_score']
        else:
            enable_transform_score = False

        ys_pred = self.predict_with_model(model, xs,
                                          disable_clip_score=disable_clip_score,
                                          enable_transform_score=enable_transform_score)
        result_dict = {}
        result_dict.update(feature_result.result_dict) # add feature result
        result_dict[self.get_scores_key()] = ys_pred # add quality score
        return Result(asset, self.executor_id, result_dict)

    @classmethod
    def predict_with_model(cls, model, xs, **kwargs):
        ys_pred = model.predict(xs)

        do_transform_score = cls._do_transform_score(kwargs)

        if do_transform_score:
            ys_pred = cls.transform_score(model, ys_pred)
        else:
            pass

        if 'disable_clip_score' in kwargs and kwargs['disable_clip_score'] is True:
            pass
        else:
            ys_pred = cls.clip_score(model, ys_pred)

        return ys_pred

    @staticmethod
    def _do_transform_score(kwargs):
        return 'enable_transform_score' in kwargs and kwargs['enable_transform_score'] is True

    @staticmethod
    def set_transform_score(model, score_transform):
        model.append_info('score_transform', score_transform)

    @staticmethod
    def set_clip_score(model, score_clip):
        model.append_info('score_clip', score_clip)

    @staticmethod
    def transform_score(model, ys_pred):
        """
        Do post processing: transform final quality score e.g. via polynomial
        {'p0': 1, 'p1': 1, 'p2': 0.5} means transform through 1 + x + 0.5 * x^2.
        For now, only support polynomail up to 2nd-order.
        """
        transform_dict = model.get_appended_info('score_transform')

        if transform_dict is None:
            return ys_pred

        y_in = ys_pred
        y_out = np.zeros(ys_pred.shape)

        # quadratic transform
        if 'p0' in transform_dict:
            y_out += transform_dict['p0']
        if 'p1' in transform_dict:
            y_out += transform_dict['p1'] * y_in
        if 'p2' in transform_dict:
            y_out += transform_dict['p2'] * y_in * y_in

        # rectification
        if 'out_lte_in' in transform_dict and transform_dict['out_lte_in'] == 'true':
            # output must be less than or equal to input
            y_out = np.minimum(y_out, y_in)
        if 'out_gte_in' in transform_dict and transform_dict['out_gte_in'] == 'true':
            # output must be greater than or equal to input
            y_out = np.maximum(y_out, y_in)

        return y_out

    @staticmethod
    def clip_score(model, ys_pred):
        """
        Do post processing: clip final quality score within e.g. [0, 100]
        """
        score_clip = model.get_appended_info('score_clip')
        if score_clip is not None:
            lb, ub = score_clip
            ys_pred = np.clip(ys_pred, lb, ub)

        return ys_pred

    def _load_model(self, asset):
        if self.optional_dict is not None \
                and 'model_filepath' in self.optional_dict \
                and self.optional_dict['model_filepath'] is not None:
            model_filepath = self.optional_dict['model_filepath']
        else:
            model_filepath = self.DEFAULT_MODEL_FILEPATH
        model = TrainTestModel.from_file(model_filepath, self.logger)
        return model

    def _remove_result(self, asset):
        # Override Executor._remove_result(self, asset) by redirecting it to the
        # FeatureAssembler.

        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.remove_results()

class VmafPhoneQualityRunner(VmafQualityRunner):

    TYPE = 'VMAF_Phone'

    VERSION = '{}-phone'.format(VmafQualityRunner.VERSION)

    def _assert_args(self):
        super(VmafPhoneQualityRunner, self)._assert_args()

        if self.optional_dict is not None:
            assert 'enable_transform_score' not in self.optional_dict, \
                'Cannot specify enable_transform_score option in {cls}.'.format(cls=self.__class__.__name__)

    @staticmethod
    def _do_transform_score(kwargs):
        return True

class VmafossExecQualityRunner(QualityRunner):

    TYPE = 'VMAFOSSEXEC'

    # VERSION = '0.3'
    # DEFAULT_MODEL_FILEPATH_DOTMODEL = VmafConfig.model_path("nflxall_vmafv3.pkl.model")

    # VERSION = '0.3.1'
    # DEFAULT_MODEL_FILEPATH_DOTMODEL = VmafConfig.model_path("nflxall_vmafv3a.pkl.model")

    # VERSION = '0.3.2'
    # ALGO_VERSION = 0
    # # DEFAULT_MODEL_FILEPATH_DOTMODEL = VmafConfig.model_path("nflxall_vmafv4.pkl.model")
    # DEFAULT_MODEL_FILEPATH = VmafConfig.model_path("nflxall_vmafv4.pkl")

    VERSION = 'F' + VmafFeatureExtractor.VERSION + '-0.6.1'
    ALGO_VERSION = 2

    # trained with resource/param/vmaf_v6.py on private/user/zli/resource/dataset/dataset/derived/vmafplusstudy_laptop_raw_generalandcornercase.py, MLER, y=x+17
    DEFAULT_MODEL_FILEPATH = VmafConfig.model_path("vmaf_v0.6.1.pkl")

    FEATURES = ['adm2', 'adm_scale0', 'adm_scale1', 'adm_scale2', 'adm_scale3',
                'motion', 'vif_scale0', 'vif_scale1', 'vif_scale2',
                'vif_scale3', 'vif', 'psnr', 'ssim', 'ms_ssim', 'motion2']

    @classmethod
    def get_feature_scores_key(cls, atom_feature):
        return "{type}_{atom_feature}_scores".format(
            type=cls.TYPE, atom_feature=atom_feature)

    def _generate_result(self, asset):
        # routine to call the command-line executable and generate quality
        # scores in the log file.

        log_file_path = self._get_log_file_path(asset)

        if self.optional_dict is not None \
                and 'model_filepath' in self.optional_dict \
                and self.optional_dict['model_filepath'] is not None:
            model_filepath = self.optional_dict['model_filepath']
        else:
            model_filepath = self.DEFAULT_MODEL_FILEPATH

        if self.optional_dict is not None and 'disable_clip_score' in self.optional_dict:
            disable_clip_score = self.optional_dict['disable_clip_score']
        else:
            disable_clip_score = False

        if self.optional_dict is not None and 'enable_transform_score' in self.optional_dict:
            enable_transform_score = self.optional_dict['enable_transform_score']
        else:
            enable_transform_score = False

        if self.optional_dict is not None and 'phone_model' in self.optional_dict:
            phone_model = self.optional_dict['phone_model']
        else:
            phone_model = False

        if self.optional_dict is not None and 'disable_avx' in self.optional_dict:
            disable_avx = self.optional_dict['disable_avx']
        else:
            disable_avx = False

        quality_width, quality_height = asset.quality_width_height

        fmt=self._get_workfile_yuv_type(asset)
        w=quality_width
        h=quality_height
        ref_path=asset.ref_workfile_path
        dis_path=asset.dis_workfile_path
        model=model_filepath
        exe=self._get_exec()
        logger = self.logger

        ExternalProgramCaller.call_vmafossexec(fmt, w, h, ref_path, dis_path, model, log_file_path,
                                               disable_clip_score, enable_transform_score,
                                               phone_model, disable_avx, exe, logger)

    def _get_exec(self):
        return None # signaling default

    def _get_quality_scores(self, asset):
        # routine to read the quality scores from the log file, and return
        # the scores in a dictionary format.
        log_file_path = self._get_log_file_path(asset)
        tree = ElementTree.parse(log_file_path)
        root = tree.getroot()
        scores = []
        feature_scores = [[] for _ in self.FEATURES]
        for frame in root.findall('frames/frame'):
            scores.append(float(frame.attrib['vmaf']))
            for i_feature, feature in enumerate(self.FEATURES):
                try:
                    feature_scores[i_feature].append(float(frame.attrib[feature]))
                except KeyError:
                    pass # some features may be missing
        assert len(scores) != 0
        quality_result = {
            self.get_scores_key(): scores,
        }
        for i_feature, feature in enumerate(self.FEATURES):
            if len(feature_scores[i_feature]) != 0:
                quality_result[self.get_feature_scores_key(feature)] = feature_scores[i_feature]
        return quality_result

class SsimQualityRunner(QualityRunner):

    TYPE = 'SSIM'
    VERSION = '1.0'

    def _get_quality_scores(self, asset):
        raise NotImplementedError

    def _generate_result(self, asset):
        raise NotImplementedError

    def _get_feature_assembler_instance(self, asset):

        feature_dict = {SsimFeatureExtractor.TYPE: SsimFeatureExtractor.ATOM_FEATURES}

        feature_assembler = FeatureAssembler(
            feature_dict=feature_dict,
            feature_option_dict=None,
            assets=[asset],
            logger=self.logger,
            fifo_mode=self.fifo_mode,
            delete_workdir=self.delete_workdir,
            result_store=self.result_store,
            optional_dict=None,
            optional_dict2=None,
            parallelize=False, # parallelization already in a higher level
        )
        return feature_assembler

    def _run_on_asset(self, asset):
        # Override Executor._run_on_asset(self, asset)
        vmaf_fassembler = self._get_feature_assembler_instance(asset)
        vmaf_fassembler.run()
        feature_result = vmaf_fassembler.results[0]
        result_dict = {}
        result_dict.update(feature_result.result_dict.copy()) # add feature result
        result_dict[self.get_scores_key()] = feature_result.result_dict[
            SsimFeatureExtractor.get_scores_key('ssim')] # add ssim score
        del result_dict[SsimFeatureExtractor.get_scores_key('ssim')] # delete redundant
        return Result(asset, self.executor_id, result_dict)

    def _remove_result(self, asset):
        # Override Executor._remove_result(self, asset) by redirecting it to the
        # FeatureAssembler.

        vmaf_fassembler = self._get_feature_assembler_instance(asset)
        vmaf_fassembler.remove_results()

class MsSsimQualityRunner(QualityRunner):

    TYPE = 'MS_SSIM'
    VERSION = '1.0'

    def _get_quality_scores(self, asset):
        raise NotImplementedError

    def _generate_result(self, asset):
        raise NotImplementedError

    def _get_feature_assembler_instance(self, asset):

        feature_dict = {MsSsimFeatureExtractor.TYPE: MsSsimFeatureExtractor.ATOM_FEATURES}

        feature_assembler = FeatureAssembler(
            feature_dict=feature_dict,
            feature_option_dict=None,
            assets=[asset],
            logger=self.logger,
            fifo_mode=self.fifo_mode,
            delete_workdir=self.delete_workdir,
            result_store=self.result_store,
            optional_dict=None,
            optional_dict2=None,
            parallelize=False, # parallelization already in a higher level
        )
        return feature_assembler

    def _run_on_asset(self, asset):
        # Override Executor._run_on_asset(self, asset)
        vmaf_fassembler = self._get_feature_assembler_instance(asset)
        vmaf_fassembler.run()
        feature_result = vmaf_fassembler.results[0]
        result_dict = {}
        result_dict.update(feature_result.result_dict.copy()) # add feature result
        result_dict[self.get_scores_key()] = feature_result.result_dict[
            MsSsimFeatureExtractor.get_scores_key('ms_ssim')] # add ssim score
        del result_dict[MsSsimFeatureExtractor.get_scores_key('ms_ssim')] # delete redundant
        return Result(asset, self.executor_id, result_dict)

    def _remove_result(self, asset):
        # Override Executor._remove_result(self, asset) by redirecting it to the
        # FeatureAssembler.

        vmaf_fassembler = self._get_feature_assembler_instance(asset)
        vmaf_fassembler.remove_results()

class VmafSingleFeatureQualityRunner(QualityRunner):

    __metaclass__ = ABCMeta

    VERSION = 'F{}-0'.format(VmafFeatureExtractor.VERSION)

    @property
    @abstractmethod
    def FEATURE_NAME(self):
        raise NotImplementedError

    def _get_quality_scores(self, asset):
        raise NotImplementedError

    def _generate_result(self, asset):
        raise NotImplementedError

    def _get_vmaf_feature_assembler_instance(self, asset):

        vmaf_fassembler = FeatureAssembler(
            feature_dict={'VMAF_feature': [self.FEATURE_NAME]},
            feature_option_dict=None,
            assets=[asset],
            logger=self.logger,
            fifo_mode=self.fifo_mode,
            delete_workdir=self.delete_workdir,
            result_store=self.result_store,
            optional_dict=None,
            optional_dict2=None,
            parallelize=False, # parallelization already in a higher level
        )
        return vmaf_fassembler

    def _run_on_asset(self, asset):
        # Override Executor._run_on_asset(self, asset)
        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.run()
        feature_result = vmaf_fassembler.results[0]
        result_dict = {
            self.get_scores_key(): feature_result[VmafFeatureExtractor.get_scores_key(self.FEATURE_NAME)]
        }

        return Result(asset, self.executor_id, result_dict)

    def _remove_result(self, asset):
        # Override Executor._remove_result(self, asset) by redirecting it to the
        # FeatureAssembler.

        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.remove_results()

class Adm2QualityRunner(VmafSingleFeatureQualityRunner):
    TYPE = 'ADM2'
    # TYPE = 'DLM'
    FEATURE_NAME = 'adm2'

class VifScale0QualityRunner(VmafSingleFeatureQualityRunner):
    TYPE = 'VIF_SCALE0'
    FEATURE_NAME = 'vif_scale0'

class VifScale1QualityRunner(VmafSingleFeatureQualityRunner):
    TYPE = 'VIF_SCALE1'
    FEATURE_NAME = 'vif_scale1'

class VifScale2QualityRunner(VmafSingleFeatureQualityRunner):
    TYPE = 'VIF_SCALE2'
    FEATURE_NAME = 'vif_scale2'

class VifScale3QualityRunner(VmafSingleFeatureQualityRunner):
    TYPE = 'VIF_SCALE3'
    FEATURE_NAME = 'vif_scale3'

class MotionQualityRunner(VmafSingleFeatureQualityRunner):
    TYPE = 'MOTION'
    # TYPE = 'TI'
    FEATURE_NAME = 'motion'

class StrredQualityRunner(QualityRunner):

    TYPE = 'STRRED'

    # VERSION = '1.0'
    VERSION = 'F' + StrredFeatureExtractor.VERSION + '-1.1'

    def _get_quality_scores(self, asset):
        raise NotImplementedError

    def _generate_result(self, asset):
        raise NotImplementedError

    def _get_feature_assembler_instance(self, asset):

        feature_dict = {StrredFeatureExtractor.TYPE: StrredFeatureExtractor.ATOM_FEATURES + getattr(StrredFeatureExtractor, 'DERIVED_ATOM_FEATURES', [])}

        feature_assembler = FeatureAssembler(
            feature_dict=feature_dict,
            feature_option_dict=None,
            assets=[asset],
            logger=self.logger,
            fifo_mode=self.fifo_mode,
            delete_workdir=self.delete_workdir,
            result_store=self.result_store,
            optional_dict=None,
            optional_dict2=None,
            parallelize=False, # parallelization already in a higher level
        )
        return feature_assembler

    def _run_on_asset(self, asset):
        # Override Executor._run_on_asset(self, asset)
        vmaf_fassembler = self._get_feature_assembler_instance(asset)
        vmaf_fassembler.run()
        feature_result = vmaf_fassembler.results[0]
        result_dict = {}
        result_dict.update(feature_result.result_dict.copy()) # add feature result
        result_dict[self.get_scores_key()] = feature_result.result_dict[
            StrredFeatureExtractor.get_scores_key('strred')] # add strred score
        del result_dict[StrredFeatureExtractor.get_scores_key('strred')] # delete redundant
        return Result(asset, self.executor_id, result_dict)

    def _remove_result(self, asset):
        # Override Executor._remove_result(self, asset) by redirecting it to the
        # FeatureAssembler.

        vmaf_fassembler = self._get_feature_assembler_instance(asset)
        vmaf_fassembler.remove_results()
