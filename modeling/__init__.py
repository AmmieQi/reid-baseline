# encoding: utf-8
"""
@author:  sherlock
@contact: sherlockliao01@gmail.com
"""

from .strong_baseline import Baseline


def build_model(cfg, num_classes):
    model = Baseline(num_classes, cfg.MODEL.LAST_STRIDE, cfg.MODEL.NAME, cfg.MODEL.PRETRAIN_CHOICE,
                     se=cfg.MODEL.IF_SE,
                     ibn_a=cfg.MODEL.IF_IBN_A,
                     ibn_b=cfg.MODEL.IF_IBN_B)
    return model
