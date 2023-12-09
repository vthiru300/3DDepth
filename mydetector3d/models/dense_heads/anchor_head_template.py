import numpy as np
import torch
import torch.nn as nn

from ...utils import box_coder_utils, common_utils, loss_utils
from .target_assigner.anchor_generator import AnchorGenerator
from .target_assigner.atss_target_assigner import ATSSTargetAssigner
from .target_assigner.axis_aligned_target_assigner import AxisAlignedTargetAssigner


class AnchorHeadTemplate(nn.Module):
    def __init__(self, model_cfg, num_class, class_names, grid_size, point_cloud_range, predict_boxes_when_training):
        super().__init__()
        self.model_cfg = model_cfg
        self.num_class = num_class #3
        self.class_names = class_names
        self.predict_boxes_when_training = predict_boxes_when_training #False
        self.use_multihead = self.model_cfg.get('USE_MULTIHEAD', False) #False

        anchor_target_cfg = self.model_cfg.TARGET_ASSIGNER_CONFIG # AxisAlignedTargetAssigner
        self.box_coder = getattr(box_coder_utils, anchor_target_cfg.BOX_CODER)( # ResidualCoder
            num_dir_bins=anchor_target_cfg.get('NUM_DIR_BINS', 6),
            **anchor_target_cfg.get('BOX_CODER_CONFIG', {})
        ) #coder size=7

        anchor_generator_cfg = self.model_cfg.ANCHOR_GENERATOR_CONFIG
        anchors, self.num_anchors_per_location = self.generate_anchors(
            anchor_generator_cfg, grid_size=grid_size, point_cloud_range=point_cloud_range,
            anchor_ndim=self.box_coder.code_size #code_size=7
        ) #anchors: [1, 248 (gridsize-493/stride-2), 216 (gridsize-432/stride-2), 1, 2, 7] *3 num_anchors_per_location=[2, 2, 2]
        self.anchors = [x.cuda() for x in anchors]
        self.target_assigner = self.get_target_assigner(anchor_target_cfg)

        self.forward_ret_dict = {}
        self.build_losses(self.model_cfg.LOSS_CONFIG)

    @staticmethod
    def generate_anchors(anchor_generator_cfg, grid_size, point_cloud_range, anchor_ndim=7):
        anchor_generator = AnchorGenerator(
            anchor_range=point_cloud_range,
            anchor_generator_config=anchor_generator_cfg
        )# config['feature_map_stride'] == 2 grid_size:[432,496,1]
        feature_map_size = [grid_size[:2] // config['feature_map_stride'] for config in anchor_generator_cfg] # [[216,248],[216,248],[216,248]]
        anchors_list, num_anchors_per_location_list = anchor_generator.generate_anchors(feature_map_size) #generate anchors for 3 classes

        if anchor_ndim != 7:
            for idx, anchors in enumerate(anchors_list):
                pad_zeros = anchors.new_zeros([*anchors.shape[0:-1], anchor_ndim - 7])
                new_anchors = torch.cat((anchors, pad_zeros), dim=-1)
                anchors_list[idx] = new_anchors

        return anchors_list, num_anchors_per_location_list

    def get_target_assigner(self, anchor_target_cfg):
        if anchor_target_cfg.NAME == 'ATSS':
            target_assigner = ATSSTargetAssigner(
                topk=anchor_target_cfg.TOPK,
                box_coder=self.box_coder,
                use_multihead=self.use_multihead,
                match_height=anchor_target_cfg.MATCH_HEIGHT
            )
        elif anchor_target_cfg.NAME == 'AxisAlignedTargetAssigner':
            target_assigner = AxisAlignedTargetAssigner(
                model_cfg=self.model_cfg,
                class_names=self.class_names,
                box_coder=self.box_coder,
                match_height=anchor_target_cfg.MATCH_HEIGHT
            )
        else:
            raise NotImplementedError
        return target_assigner

    def build_losses(self, losses_cfg):
        self.add_module(#classficiation loss
            'cls_loss_func',
            loss_utils.SigmoidFocalClassificationLoss(alpha=0.25, gamma=2.0)
        )
        reg_loss_name = 'WeightedSmoothL1Loss' if losses_cfg.get('REG_LOSS_TYPE', None) is None \
            else losses_cfg.REG_LOSS_TYPE
        self.add_module(#regression loss
            'reg_loss_func',
            getattr(loss_utils, reg_loss_name)(code_weights=losses_cfg.LOSS_WEIGHTS['code_weights'])
        )
        self.add_module(#direction classfication loss
            'dir_loss_func',
            loss_utils.WeightedCrossEntropyLoss()
        )

    def assign_targets(self, gt_boxes):
        """
        Args:
            gt_boxes: (B, M, 8) [16, 45, 8]
        Returns:

        """
        targets_dict = self.target_assigner.assign_targets(
            self.anchors, gt_boxes
        )
        return targets_dict

    def get_cls_layer_loss(self):
        #(batch_size, 248, 216, 18) predict classes
        cls_preds = self.forward_ret_dict['cls_preds'] #[16, 248, 216, 18]
        #foreground anchor classes, 'box_cls_labels' is calculated from assign_targets function
        box_cls_labels = self.forward_ret_dict['box_cls_labels'] #[16, 321408] 321408=248*216*3*2 num_anchors
        batch_size = int(cls_preds.shape[0]) #16
        #interested anchor, (threshold between 0.45~0.6 is -1, not included)
        cared = box_cls_labels >= 0  # [N, num_anchors] [16, 321408]
        positives = box_cls_labels > 0 #forground anchor
        negatives = box_cls_labels == 0 #background anchor
        negative_cls_weights = negatives * 1.0 #weight for background anchor
        cls_weights = (negative_cls_weights + 1.0 * positives).float() #background+foreground weight=weight for classification loss
        reg_weights = positives.float() #regression weight, many 0
        if self.num_class == 1:
            # class agnostic
            box_cls_labels[positives] = 1 #positive is 1 [16, 321408]

        pos_normalizer = positives.sum(1, keepdim=True).float() #sum of positive cases
        reg_weights /= torch.clamp(pos_normalizer, min=1.0)
        cls_weights /= torch.clamp(pos_normalizer, min=1.0)
        cls_targets = box_cls_labels * cared.type_as(box_cls_labels)
        #expand dimension in the last dimension
        cls_targets = cls_targets.unsqueeze(dim=-1) #[16, 321408, 1]

        cls_targets = cls_targets.squeeze(dim=-1)
        one_hot_targets = torch.zeros(
            *list(cls_targets.shape), self.num_class + 1, dtype=cls_preds.dtype, device=cls_targets.device
        ) #[16, 321408, 4] num_class+1 consider the background

        one_hot_targets.scatter_(-1, cls_targets.unsqueeze(dim=-1).long(), 1.0) #convert to one-hot encoding [16, 321408, 4]
        #(batch_size, 248, 216,18)->(batch_size, 321408,3)
        cls_preds = cls_preds.view(batch_size, -1, self.num_class) #[16, 321408, 3]
        one_hot_targets = one_hot_targets[..., 1:] #[16, 321408, 3] remove background class, do not calculate the classification loss for background
        
        #calculate classificationn loss [N,M] [16, 321408, 3]
        cls_loss_src = self.cls_loss_func(cls_preds, one_hot_targets, weights=cls_weights)  # [16, 321408, 3]
        cls_loss = cls_loss_src.sum() / batch_size

        cls_loss = cls_loss * self.model_cfg.LOSS_CONFIG.LOSS_WEIGHTS['cls_weight']
        tb_dict = {
            'rpn_loss_cls': cls_loss.item()
        }
        return cls_loss, tb_dict

    @staticmethod
    def add_sin_difference(boxes1, boxes2, dim=6):
        assert dim != -1
        rad_pred_encoding = torch.sin(boxes1[..., dim:dim + 1]) * torch.cos(boxes2[..., dim:dim + 1])
        rad_tg_encoding = torch.cos(boxes1[..., dim:dim + 1]) * torch.sin(boxes2[..., dim:dim + 1])
        boxes1 = torch.cat([boxes1[..., :dim], rad_pred_encoding, boxes1[..., dim + 1:]], dim=-1)
        boxes2 = torch.cat([boxes2[..., :dim], rad_tg_encoding, boxes2[..., dim + 1:]], dim=-1)
        return boxes1, boxes2

    @staticmethod
    def get_direction_target(anchors, reg_targets, one_hot=True, dir_offset=0, num_bins=2):
        batch_size = reg_targets.shape[0]
        #(b,321408,7)
        anchors = anchors.view(batch_size, -1, anchors.shape[-1])
        #(b,321408) -pi~pi, reg_targets is after encoding, add anchors degree to get the original degree
        rot_gt = reg_targets[..., 6] + anchors[..., 6]
        #limit to 0~2pi, original degreee is from -pi~pi
        offset_rot = common_utils.limit_period(rot_gt - dir_offset, 0, 2 * np.pi)
        #(b,321408) value 0 and 1, num_bins=2
        dir_cls_targets = torch.floor(offset_rot / (2 * np.pi / num_bins)).long()
        #(b,321408)
        dir_cls_targets = torch.clamp(dir_cls_targets, min=0, max=num_bins - 1)

        if one_hot:
            #(b,321408,2)
            dir_targets = torch.zeros(*list(dir_cls_targets.shape), num_bins, dtype=anchors.dtype,
                                      device=dir_cls_targets.device)
            #one-hot encoding for two directions (positive and negative)
            dir_targets.scatter_(-1, dir_cls_targets.unsqueeze(dim=-1).long(), 1.0)
            dir_cls_targets = dir_targets
        return dir_cls_targets

    def get_box_reg_layer_loss(self):
        #anchor_box 7 regression parameters, (batch_size, 248, 216, 42)
        box_preds = self.forward_ret_dict['box_preds'] #[16, 248, 216, 42]
        #anchor_box diretion prediction (batch_size, 248, 216, 12)
        box_dir_cls_preds = self.forward_ret_dict.get('dir_cls_preds', None) #[16, 248, 216, 12]
        #[batch_size, 321408, 7] anchor and gt coding results
        box_reg_targets = self.forward_ret_dict['box_reg_targets'] #[16, 321408, 7]
        box_cls_labels = self.forward_ret_dict['box_cls_labels'] #[16, 321408]
        batch_size = int(box_preds.shape[0])

        #get all foreground anchor mask: (batch_size, 321408)
        positives = box_cls_labels > 0
        reg_weights = positives.float()
        pos_normalizer = positives.sum(1, keepdim=True).float()
        reg_weights /= torch.clamp(pos_normalizer, min=1.0)

        if isinstance(self.anchors, list):
            if self.use_multihead:
                anchors = torch.cat(
                    [anchor.permute(3, 4, 0, 1, 2, 5).contiguous().view(-1, anchor.shape[-1]) for anchor in
                     self.anchors], dim=0)
            else:
                anchors = torch.cat(self.anchors, dim=-3) #[1, 248, 216, 1, 2, 7]*3 -> [1, 248, 216, 3, 2, 7]
        else:
            anchors = self.anchors
        #(1,248*216,7)->(b,248*216,7)
        anchors = anchors.view(1, -1, anchors.shape[-1]).repeat(batch_size, 1, 1) #[16, 321408, 7]
        #(b,248*216,7)
        box_preds = box_preds.view(batch_size, -1,
                                   box_preds.shape[-1] // self.num_anchors_per_location if not self.use_multihead else
                                   box_preds.shape[-1]) #[16, 321408, 7]
        # sin(a - b) = sinacosb-cosasinb
        #(b,321408,7)
        box_preds_sin, reg_targets_sin = self.add_sin_difference(box_preds, box_reg_targets)
        loc_loss_src = self.reg_loss_func(box_preds_sin, reg_targets_sin, weights=reg_weights)  # [16, 321408, 7]
        loc_loss = loc_loss_src.sum() / batch_size

        loc_loss = loc_loss * self.model_cfg.LOSS_CONFIG.LOSS_WEIGHTS['loc_weight']
        box_loss = loc_loss
        tb_dict = {
            'rpn_loss_loc': loc_loss.item() #return value
        }

        if box_dir_cls_preds is not None:
            #(b,321408,2)
            dir_targets = self.get_direction_target(
                anchors, box_reg_targets,
                dir_offset=self.model_cfg.DIR_OFFSET, #direction offset 0.785=pi/4
                num_bins=self.model_cfg.NUM_DIR_BINS #directions of bins =2
            )
            #(b,321408,2)
            dir_logits = box_dir_cls_preds.view(batch_size, -1, self.model_cfg.NUM_DIR_BINS)
            #positive sample (b,321408)
            weights = positives.type_as(dir_logits)
            #normalize
            weights /= torch.clamp(weights.sum(-1, keepdim=True), min=1.0)
            dir_loss = self.dir_loss_func(dir_logits, dir_targets, weights=weights)
            dir_loss = dir_loss.sum() / batch_size
            dir_loss = dir_loss * self.model_cfg.LOSS_CONFIG.LOSS_WEIGHTS['dir_weight']
            #add direction loss to the box loss
            box_loss += dir_loss
            tb_dict['rpn_loss_dir'] = dir_loss.item()

        return box_loss, tb_dict

    def get_loss(self):
        cls_loss, tb_dict = self.get_cls_layer_loss() #classification layer loss, 'rpn_loss_cls'
        box_loss, tb_dict_box = self.get_box_reg_layer_loss() #get 'rpn_loss_loc' and 'rpn_loss_dir'
        tb_dict.update(tb_dict_box)
        rpn_loss = cls_loss + box_loss

        tb_dict['rpn_loss'] = rpn_loss.item()
        return rpn_loss, tb_dict

    def generate_predicted_boxes(self, batch_size, cls_preds, box_preds, dir_cls_preds=None):
        """
        Args:
            batch_size:
            cls_preds: (N, H, W, C1)
            box_preds: (N, H, W, C2)
            dir_cls_preds: (N, H, W, C3)

        Returns:
            batch_cls_preds: (B, num_boxes, num_classes)
            batch_box_preds: (B, num_boxes, 7+C)

        """
        if isinstance(self.anchors, list):
            if self.use_multihead:
                anchors = torch.cat([anchor.permute(3, 4, 0, 1, 2, 5).contiguous().view(-1, anchor.shape[-1])
                                     for anchor in self.anchors], dim=0) #[321408, 8]
            else:
                anchors = torch.cat(self.anchors, dim=-3) #[1, 248, 216, 1, 2, 7]*3 =>[1, 248, 216, 3, 2, 7]
        else:
            anchors = self.anchors
        num_anchors = anchors.view(-1, anchors.shape[-1]).shape[0] #321408=248*216*3*2
        batch_anchors = anchors.view(1, -1, anchors.shape[-1]).repeat(batch_size, 1, 1) #[16, 321408, 7]
        batch_cls_preds = cls_preds.view(batch_size, num_anchors, -1).float() \
            if not isinstance(cls_preds, list) else cls_preds  #[16, 321408, 3]
        batch_box_preds = box_preds.view(batch_size, num_anchors, -1) if not isinstance(box_preds, list) \
            else torch.cat(box_preds, dim=1).view(batch_size, num_anchors, -1)
        batch_box_preds = self.box_coder.decode_torch(batch_box_preds, batch_anchors) #[16, 321408, 7]

        if dir_cls_preds is not None:
            dir_offset = self.model_cfg.DIR_OFFSET
            dir_limit_offset = self.model_cfg.DIR_LIMIT_OFFSET
            dir_cls_preds = dir_cls_preds.view(batch_size, num_anchors, -1) if not isinstance(dir_cls_preds, list) \
                else torch.cat(dir_cls_preds, dim=1).view(batch_size, num_anchors, -1) #[16, 321408, 2]
            dir_labels = torch.max(dir_cls_preds, dim=-1)[1] #[16, 321408] -> positive direction or negative direction

            period = (2 * np.pi / self.model_cfg.NUM_DIR_BINS) #3.14
            dir_rot = common_utils.limit_period(
                batch_box_preds[..., 6] - dir_offset, dir_limit_offset, period
            )#[16, 321408], limit to [0,pi]
            batch_box_preds[..., 6] = dir_rot + dir_offset + period * dir_labels.to(batch_box_preds.dtype) #[16, 321408, 7] convert to 0.25pi to 2.5pi

        if isinstance(self.box_coder, box_coder_utils.PreviousResidualDecoder):
            batch_box_preds[..., 6] = common_utils.limit_period(
                -(batch_box_preds[..., 6] + np.pi / 2), offset=0.5, period=np.pi * 2
            )

        return batch_cls_preds, batch_box_preds #[16, 321408, 3] [16, 321408, 7]

    def forward(self, **kwargs):
        raise NotImplementedError
