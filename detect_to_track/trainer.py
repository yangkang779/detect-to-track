"""handles joint training of entire system"""

import math
from pathlib import Path
from typing import Tuple, Sequence

import torch
from torch.nn.parallel import DataParallel
from torch.utils.data import BatchSampler
from torch.optim import SGD
import numpy as np
from ml_utils.prediction_filtering import PredictionFilterPipeline

from .data import ImageInstance
from .data.data_management import DataSampler, DataManager
from .data.encoding import (
    AnchorEncoder,
    RegionEncoder,
    frcnn_box_decode,
    track_encode
)
from .loss import RPNLoss, RCNNLoss, TrackLoss
from .models import DetectTrackModule, ResNetFeatures
from .utils import (
    DTLoss,
    tensor_to_ndarray,
    make_input_transform,
)


class DetectTrackTrainer:
    """approximate joint training for two stage object detectors.
    ignores detector head loss wrt to region proposal.
    this can be (but is not currently) addressed by substituting the
    ROIPooling layer for a ROIWarping layer.
    see https://arxiv.org/abs/1506.01497.

    Args:
        model:
        trn_sampler: training set sampler.
        val_manager: validation set manager.
        trn_sample_size: number of training examples to train on before
            validating and reporting.
        batch_size: minibatch size.
        net_input_hw: height and width of network input tensor.
        anchors:
        encoder_iou_thresh:
        encoder_iou_margin:
        region_filter: given a set of region proposals, returns a higher
            confidence subset of proposals.
        alpha: loss alpha balancing factor.
        gamma: loss focusing factor.
        loss_coefs: leading coefficient for each element of joint loss.
            gradients are backpropagated from dot(loss_coefs, losses)
        sgd_kwargs: parameters for stochastic gradient descent.
        patience:
        output_dir:
    """
    def __init__(
            self,
            model: DetectTrackModule,
            trn_sampler: DataSampler,
            val_manager: DataManager,
            trn_sample_size: int,
            batch_size: int,
            net_input_hw: int,
            anchors: np.ndarray,
            encoder_iou_thresh: float,
            encoder_iou_margin: float,
            region_filter: PredictionFilterPipeline,
            alpha: float,
            gamma: float,
            loss_coefs: Sequence[float],
            sgd_kwargs: dict,
            patience: int,
            output_dir: str = 'output'
    ) -> None:
        ### models
        self._im_to_x = make_input_transform(net_input_hw)
        if torch.cuda.device_count() > 1:
            model = DataParallel(model)
        self.model = model.cuda()

        ### datasets
        self.trn_sampler = trn_sampler
        self.val_loader = BatchSampler(val_manager, batch_size, False)
        self.trn_sample_size = trn_sample_size
        self.batch_size = batch_size

        ### ground-truth label encoding
        self._anchor_encoder = AnchorEncoder(
            anchors, encoder_iou_thresh, encoder_iou_margin
        )
        self._region_encoder = RegionEncoder(encoder_iou_thresh)
        self._region_filter = region_filter  # filters rois before rcnn

        ### loss
        self._rpn_loss_func = RPNLoss(alpha, gamma)
        self._rcnn_loss_func = RCNNLoss(alpha, gamma)
        self._track_loss_func = TrackLoss()
        self._loss_coefs = torch.as_tensor(loss_coefs).cuda()

        ### optimizers
        self._optim = SGD(self.model.parameters(), **sgd_kwargs)

        self.patience = patience
        self.output_dir = Path(output_dir)

        ### state
        self.n_iters = 0
        self.best_val_loss = float('inf')
        self.iters_no_improvement = 0

    def _forward_loss(
            self, instance: Tuple[ImageInstance, ImageInstance]
    ) -> DTLoss:
        """compute joint loss for a single instance.

        Args:
            instance: (image, labels) tuple for time t, t+tau.

        Returns:
            dt_loss:
                o_loss: RPN binary classification loss.
                b_loss_rpn: RPN bounding box regression loss.
                c_loss: RCNN multiclass classification loss.
                b_loss_rcnn: RCNN bounding box regression loss.
                t_loss: cross-frame tracking loss.
        """
        inst_0, inst_1 = instance

        ### extract feature maps.
        x0 = self._im_to_x(inst_0.im)  # (3, H, W)
        x1 = self._im_to_x(inst_1.im)  # (3, H, W)
        x = torch.stack([x0, x1])  # (2, 3, H, W)
        x = x.cuda()
        fmaps = self.model.backbone(x)  # pyramid of feature maps 3*(2, ...)

        ### compute losses for RPN
        ###   - inputs are feature maps
        ###   - supervision from ground-truth labels
        # RPN label encoding.
        lw0_rpn, c0_star_rpn, b0_star_rpn = self._anchor_encoder(inst_0.labels)
        lw1_rpn, c1_star_rpn, b1_star_rpn = self._anchor_encoder(inst_1.labels)
        lw_rpn = np.stack([lw0_rpn, lw1_rpn])  # (2, |A|)
        c_star_rpn = np.stack([c0_star_rpn, c1_star_rpn])  # (2, |A|)
        b_star_rpn = np.stack([b0_star_rpn, b1_star_rpn])  # (2, |A|, 4)
        # RPN predictions.
        o_hat_rpn, b_hat_rpn, fm_reg = self.model.rpn(fmaps.c4)
        # RPN loss.
        lw_rpn = torch.as_tensor(lw_rpn).cuda()
        c_star_rpn = torch.as_tensor(c_star_rpn).cuda()
        b_star_rpn = torch.as_tensor(b_star_rpn).cuda()
        o_loss_rpn, b_loss_rpn = self._rpn_loss_func(
            lw_rpn, o_hat_rpn, c_star_rpn, b_hat_rpn, b_star_rpn
        )

        ### compute losses for RCNN
        ###   - inputs are feature maps and RPN output
        ###   - supervision from ground-truth labels and regions from RPN output
        # acquire filtered regions for ROI pooling.
        o0_hat_rpn, o1_hat_rpn = [
            tensor_to_ndarray(confs)
            for confs in o_hat_rpn[:, :, 1]  # confidence for "object" class
        ]  # 2 * (|A|,)
        regions_0, regions_1 = [
            frcnn_box_decode(
                self._anchor_encoder.anchors,  # (|A|, 4)
                tensor_to_ndarray(offsets)  # (|A|, 4)
            )  # (|A|, 4)
            for offsets in b_hat_rpn  # (2, |A|, 4)
        ]  # 2*(|A|, 4)
        regions_0 = self._region_filter(o0_hat_rpn, regions_0)  # (|R0|, 4)
        regions_1 = self._region_filter(o1_hat_rpn, regions_1)  # (|R1|, 4)
        # would prefer to have encoding details abstracted away by a dataset
        # object, but the 2-stage structure complicates this. the main issue
        # is that the (unencoded) ground truth labels are required again once
        # we have obtained the region proposals in order to encode the labels
        # for the rcnn.
        c0_star_rcnn, b0_star_rcnn = self._region_encoder(
            regions_0, inst_0.labels
        )  # (|R0|,), (|R0|, 4)
        c1_star_rcnn, b1_star_rcnn = self._region_encoder(
            regions_1, inst_1.labels
        )  # (|R1|,), (|R1|, 4)
        c_star_rcnn = np.concatenate([c0_star_rcnn, c1_star_rcnn])  # (|R0 u R1|,)
        b_star_rcnn = np.concatenate([b0_star_rcnn, b1_star_rcnn])  # (|R0 u R1|, 4)
        # RCNN predictions.
        c5_0, c5_1 = fmaps.c5  # 2*(C', H', W')
        regions_0 = torch.as_tensor(regions_0).cuda()  # (|R0|, 4)
        regions_1 = torch.as_tensor(regions_1).cuda()  # (|R1|, 4)
        c0_hat_rcnn, b0_hat_rcnn = self.model.rcnn(c5_0, regions_0)  # (|R0|, ...)
        c1_hat_rcnn, b1_hat_rcnn = self.model.rcnn(c5_1, regions_1)  # (|R1|, ...)
        c_hat_rcnn = torch.cat([c0_hat_rcnn, c1_hat_rcnn])  # (|R0 u R1|, n_classes)
        b_hat_rcnn = torch.cat([b0_hat_rcnn, b1_hat_rcnn])  # (|R0 u R1|, 4)
        # RCNN loss.
        c_star_rcnn = torch.as_tensor(c_star_rcnn).cuda()  # (|R0 u R1|,)
        b_star_rcnn = torch.as_tensor(b_star_rcnn).cuda()  # (|R0 u R1|, 4)
        c_loss_rcnn, b_loss_rcnn = self._rcnn_loss_func(
            c_hat_rcnn, c_star_rcnn, b_hat_rcnn, b_star_rcnn
        )

        ### compute losses for correlation trackers
        ###   - inputs are feature maps from each time step
        ###   - supervision from ground-truth labels from each time step
        # CT label encoding.
        track_rois, t_star = track_encode(inst_0.labels, inst_1.labels)  # 2 * (|R0 n R1|, 4)
        # CT predictions.
        # start by unzipping features from each time step
        c3_0, c3_1 = fmaps.c3  # 2 * (C, H, W)
        c4_0, c4_1 = fmaps.c4  # 2 * (C, H', W')
        c5_0, c5_1 = fmaps.c5  # 2 * (C, H', W')
        fm_pyr0 = ResNetFeatures(c3=c3_0, c4=c4_0, c5=c5_0)
        fm_pyr1 = ResNetFeatures(c3=c3_1, c4=c4_1, c5=c5_1)
        fm_reg0, fm_reg1 = fm_reg  # 2 * (Cr, Hr, Wr) RPN feature maps
        track_rois = torch.as_tensor(track_rois).cuda()  # (|R0 n R1|, 4)
        t_hat = self.model.c_tracker(
            fm_pyr0, fm_pyr1, fm_reg0, fm_reg1, track_rois
        )  # (|R0 n R1|, 4)
        # CT loss.
        t_star = torch.as_tensor(t_star).cuda()
        t_loss = self._track_loss_func(t_hat, t_star)

        dt_loss = DTLoss(
            o_loss=o_loss_rpn,
            b_loss_rpn=b_loss_rpn,
            c_loss=c_loss_rcnn,
            b_loss_rcnn=b_loss_rcnn,
            t_loss=t_loss
        )

        return dt_loss

    def _minibatch_loss(
            self,
            minibatch: Sequence[Tuple[ImageInstance, ImageInstance]]
    ) -> DTLoss:
        """compute averaged loss for a single minibatch"""
        minibatch_loss = DTLoss()
        for instance in minibatch:
            minibatch_loss += self._forward_loss(instance)

        return minibatch_loss

    def step(self) -> Tuple[DTLoss, DTLoss]:
        """train on subset, validate, and report."""
        ### train
        self.model.train()
        trn_loss = DTLoss()
        for _ in range(self.trn_sample_size // self.batch_size):
            minibatch = [
                self.trn_sampler.sample() for _ in range(self.batch_size)
            ]
            minibatch_loss = self._minibatch_loss(minibatch)

            self._optim.zero_grad()
            minibatch_loss.backward(self._loss_coefs)
            self._optim.step()

            trn_loss += minibatch_loss
            self.n_iters += len(minibatch)

        ### validate
        self.model.eval()
        val_loss = DTLoss()
        with torch.no_grad():
            for minibatch in self.val_loader:
                minibatch_loss = self._minibatch_loss(minibatch)

                val_loss += minibatch_loss

        return trn_loss, val_loss

    def train(self, max_iters: int = math.inf) -> None:
        """iterate until stopping condition is satisfied."""
        while True:
            trn_loss, val_loss = self.step()

            scalar_val_loss = float(val_loss.to_scalar(self._loss_coefs))
            if scalar_val_loss < self.best_val_loss:
                self.best_val_loss = scalar_val_loss
                self.iters_no_improvement = 0
                torch.save(
                    self.model.state_dict(),
                    Path(self.output_dir, 'weights.pt')
                )
            else:
                self.iters_no_improvement += 1

            ### report
            print(' '.join([str(trn_loss), str(val_loss)]))

            ### check if any stopping conditions have been satisfied
            if any([
                    self.n_iters > max_iters,
                    self.iters_no_improvement > self.patience
            ]):
                return
