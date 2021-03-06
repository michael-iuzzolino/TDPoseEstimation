# ------------------------------------------------------------------------------
# Copyright (c) Microsoft
# Licensed under the MIT License.
# Written by Bin Xiao (Bin.Xiao@microsoft.com)
# ------------------------------------------------------------------------------

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import logging
import time
from pathlib import Path

import torch
import torch.optim as optim

from core.config import get_model_name


def get_dataset_name(cfg):
    dataset = cfg.DATASET.DATASET + '_' + cfg.DATASET.HYBRID_JOINTS_TYPE \
        if cfg.DATASET.HYBRID_JOINTS_TYPE else cfg.DATASET.DATASET
    dataset = dataset.replace(':', '_')
    return dataset

def create_experiment_directory(cfg, cfg_name, distillation=False, make_dir=True):
    root_output_dir = Path(cfg.OUTPUT_DIR)
    # set up logger
    if not root_output_dir.exists() and make_dir:
        print('=> creating {}'.format(root_output_dir))
        root_output_dir.mkdir()

    dataset = get_dataset_name(cfg)
    model, _ = get_model_name(cfg)
    small = "small" in cfg_name

    cfg_name = os.path.basename(cfg_name).split('.')[0]

    # final_output_dir = os.path.join(root_output_dir, dataset, model, cfg_name)
    model_str = model + f"__TD_{cfg.LOSS.TD_LAMBDA}"
    if small:
        model_str = model_str + "__small"
    if distillation:
        model_str = model_str + "__distill"
        teacher_td = cfg.MODEL.TEACHER_CFG.split("td_")[1].split("__")[0]
        if "_" in teacher_td:
          teacher_td = teacher_td.replace("_", ".")
        teacher_td = float(teacher_td)
        model_str = model_str + f"__TD_{teacher_td}"
#     if cfg.MODEL.EXTRA.SHARE_HG_WEIGHTS:
#         model_str = model_str + "__shared_weights"
    if cfg.MODEL.EXTRA.DOUBLE_STACK:
        model_str = model_str + "__double"

    final_output_dir = root_output_dir / dataset / model_str
    if make_dir:
        print('=> creating {}'.format(final_output_dir))
        final_output_dir.mkdir(parents=True, exist_ok=True)

    return str(final_output_dir)


def create_logger(cfg, cfg_name, final_output_dir, phase='train', make_dir=True):
    time_str = time.strftime('%Y-%m-%d-%H-%M')
    log_file = '{}_{}_{}.log'.format(cfg_name, time_str, phase)
    final_log_file = os.path.join(final_output_dir, log_file)
    head = '%(asctime)-15s %(message)s'
    model, _ = get_model_name(cfg)
    dataset = get_dataset_name(cfg)
    logging.basicConfig(filename=str(final_log_file),
                        format=head)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    console = logging.StreamHandler()
    logging.getLogger('').addHandler(console)

    tensorboard_log_dir = Path(cfg.LOG_DIR) / dataset / model / \
        (cfg_name + '_' + time_str)
    print('=> creating {}'.format(tensorboard_log_dir))
    if make_dir:
        tensorboard_log_dir.mkdir(parents=True, exist_ok=True)

    return logger, str(tensorboard_log_dir)


def get_optimizer(cfg, model):
    optimizer = None
    if cfg.TRAIN.OPTIMIZER == 'sgd':
        optimizer = optim.SGD(
            model.parameters(),
            lr=cfg.TRAIN.LR,
            momentum=cfg.TRAIN.MOMENTUM,
            weight_decay=cfg.TRAIN.WD,
            nesterov=cfg.TRAIN.NESTEROV
        )
    elif cfg.TRAIN.OPTIMIZER == 'adam':
        optimizer = optim.Adam(
            model.parameters(),
            lr=cfg.TRAIN.LR,
            weight_decay=cfg.TRAIN.WD,
        )
    elif cfg.TRAIN.OPTIMIZER == 'rmsprop':
        optimizer = optim.RMSprop(
            model.parameters(),
            lr=cfg.TRAIN.LR,
            weight_decay=cfg.TRAIN.WD,
        )

    return optimizer


def save_checkpoint(save_dict, is_best, output_dir, filename='checkpoint.pth.tar'):
    # Checkpoint
    ckpt_path = os.path.join(output_dir, filename)
    torch.save(save_dict, ckpt_path)
    
    best_ckpt_path = os.path.join(output_dir, 'model_best.pth.tar')
    if is_best:
        torch.save(save_dict, best_ckpt_path)
