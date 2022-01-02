# -*- coding: utf-8 -*-
# @Author: XP

import logging
import os
import torch
from core.test_backbone import test_backbone
import utils.data_loaders
import utils.helpers
from datetime import datetime
from tqdm import tqdm
from time import time
from tensorboardX import SummaryWriter
from utils.average_meter import AverageMeter
from torch.optim.lr_scheduler import StepLR
from utils.schedular import GradualWarmupScheduler
from utils.loss_utils import chamfer_sqrt
from models.pcn import Encoder
from models.SApcn import ASFM
from models.utils import fps_subsample, RMSELoss


def SAModulesInit(path, step_ratio=4):
    """
    Args
        path: string, path to the model we trained in step 1.

    returns
        BL-Encoder:baseline encoder
        SA-AutoEncoder
    """
    bl_encoder = Encoder()
    as_autoencoder = ASFM(step_ratio=step_ratio)

    checkpoint = torch.load(path)
    update_dict = {}
    for k, v in checkpoint['model'].items():
        if k.startswith("module.encoder"):
            k_ = '.'.join(k.split('.')[2:])
            update_dict[k_] = v

    print(bl_encoder.load_state_dict(update_dict))
    for param in bl_encoder.parameters():
        param.requires_grad = False

    print("="*20)

    update_dict = {}
    for k, v in checkpoint['model'].items():
        if k.startswith("module.decoder"):
            k_ = '.'.join(k.split('.')[1:])
            update_dict[k_] = v

    print(as_autoencoder.load_state_dict(update_dict, strict=False))

    logging.info('AS Preparation Done!')
    return bl_encoder, as_autoencoder


def FreezeDecoder(model):
    for param in model.module.decoder.parameters():
        param.requires_grad = False


def train_backbone(cfg):
    # Enable the inbuilt cudnn auto-tuner to find the best algorithm to use
    torch.backends.cudnn.benchmark = True

    train_dataset_loader = utils.data_loaders.DATASET_LOADER_MAPPING[cfg.DATASET.TRAIN_DATASET](
        cfg)
    test_dataset_loader = utils.data_loaders.DATASET_LOADER_MAPPING[cfg.DATASET.TEST_DATASET](
        cfg)

    train_data_loader = torch.utils.data.DataLoader(dataset=train_dataset_loader.get_dataset(
        utils.data_loaders.DatasetSubset.TRAIN),
        batch_size=cfg.TRAIN.BACKBONE_BATCH_SIZE,
        num_workers=cfg.CONST.NUM_WORKERS,
        collate_fn=utils.data_loaders.collate_fn,
        pin_memory=True,
        shuffle=True,
        drop_last=False)
    val_data_loader = torch.utils.data.DataLoader(dataset=test_dataset_loader.get_dataset(
        utils.data_loaders.DatasetSubset.TEST),
        batch_size=cfg.TRAIN.BACKBONE_BATCH_SIZE,
        num_workers=cfg.CONST.NUM_WORKERS//2,
        collate_fn=utils.data_loaders.collate_fn,
        pin_memory=True,
        shuffle=False)

    # Set up folders for logs and checkpoints
    output_dir = os.path.join(cfg.DIR.OUT_PATH, '%s', '%s',
                              datetime.now().isoformat())
    print(output_dir)
    cfg.DIR.CHECKPOINTS = output_dir % ('backbone', 'checkpoints')
    cfg.DIR.LOGS = output_dir % ('backbone', 'logs')
    if not os.path.exists(cfg.DIR.CHECKPOINTS):
        os.makedirs(cfg.DIR.CHECKPOINTS)

    # Create tensorboard writers
    train_writer = SummaryWriter(os.path.join(cfg.DIR.LOGS, 'train'))
    val_writer = SummaryWriter(os.path.join(cfg.DIR.LOGS, 'test'))

    bl_encoder, as_autoencoder = SAModulesInit(cfg.CONST.PCNWEIGHTS)
    if torch.cuda.is_available():
        bl_encoder = torch.nn.DataParallel(bl_encoder).cuda()
        as_autoencoder = torch.nn.DataParallel(as_autoencoder).cuda()

    # Create the optimizers
    optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad, as_autoencoder.parameters()),
                                 lr=cfg.TRAIN.BACKBONE_LEARNING_RATE,
                                 weight_decay=cfg.TRAIN.WEIGHT_DECAY,
                                 betas=cfg.TRAIN.BETAS)

    # lr scheduler
    lr_scheduler = StepLR(
        optimizer, step_size=20, gamma=0.7)
    init_epoch = 0
    best_metrics = float('inf')
    steps = 0

    if 'BBWEIGHTS' in cfg.CONST:
        print("BBWEIGHTS FOUND")
        logging.info('Recovering from %s ...' % (cfg.CONST.BBWEIGHTS))
        checkpoint = torch.load(cfg.CONST.BBWEIGHTS)
        init_epoch = checkpoint["epoch_index"]
        best_metrics = checkpoint['best_metrics']
        steps = checkpoint['steps']
        as_autoencoder.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
        logging.info('Recover complete. Current epoch = #%d; best metrics = %s.' % (
            init_epoch, best_metrics))
    else:
        print("BBWEIGHTS NOT FOUND")
        # no need 2 warmup
        lr_scheduler = GradualWarmupScheduler(optimizer, multiplier=1, total_epoch=cfg.TRAIN.WARMUP_STEPS,
                                              after_scheduler=lr_scheduler)

    # Freeze the decoder of SA-module
    # FreezeDecoder(as_autoencoder)
    # for name, param in as_autoencoder.named_parameters():
    #     if param.requires_grad:
    #         print(name)

    # print("="*20)

    # for name, param in bl_encoder.named_parameters():
    #     if param.requires_grad:
    #         print(name)
    # return

    getFeatLoss = RMSELoss()

    # Training/Testing the network
    count = 0
    for epoch_idx in range(init_epoch + 1, cfg.TRAIN.N_EPOCHS + 1):

        epoch_start_time = time()

        batch_time = AverageMeter()
        data_time = AverageMeter()

        as_autoencoder.train()

        total_cd_loss, iter_count = 0, 0
        total_cd_feat = 0
        total_cd_coarse = 0
        total_cd_fine = 0

        batch_end_time = time()
        n_batches = len(train_data_loader)

        accumulation_steps = 2  # 4 * bs(8) = 32(bs in paper)
        with tqdm(train_data_loader) as t:
            for batch_idx, (taxonomy_ids, model_ids, data) in enumerate(t):

                # Debug switch
                # count += 1
                if count > 3:
                    break

                data_time.update(time() - batch_end_time)
                for k, v in data.items():
                    data[k] = utils.helpers.var_or_cuda(v)
                partial = data['partial_cloud']
                gt = data['gtcloud']

                #  downsample gt to 2048
                bl_inputs = fps_subsample(gt, 2048)
                coarse_gt = fps_subsample(gt, 1024)
                fine_gt = fps_subsample(gt, 4096)

                # preprocess transpose
                partial = partial.permute(0, 2, 1)
                bl_inputs = bl_inputs.permute(0, 2, 1)

                v, y_coarse, y_detail = as_autoencoder(partial)

                # feature matching loss
                v_complete = bl_encoder(bl_inputs)
                loss_feat = getFeatLoss(v, v_complete)

                loss_coarse = chamfer_sqrt(coarse_gt, y_coarse)

                loss_fine = chamfer_sqrt(fine_gt, y_detail)

                loss = 1e-3 * loss_feat + loss_coarse + loss_fine
                loss = loss / accumulation_steps
                loss.backward()

                if (batch_idx+1) % accumulation_steps == 0:
                    optimizer.step()
                    optimizer.zero_grad()

                    cd_feat = loss_feat.item()
                    total_cd_feat += cd_feat
                    cd_coarse = loss_coarse.item() * 1e3
                    total_cd_coarse += cd_coarse
                    cd_fine = loss_fine.item() * 1e3
                    total_cd_fine += cd_fine
                    cd_total = loss.item() * 1e3
                    total_cd_loss += cd_total

                    n_itr = (epoch_idx - 1) * n_batches + batch_idx
                    train_writer.add_scalar(
                        'Loss/Batch/feat_matching', loss_feat, n_itr)
                    train_writer.add_scalar(
                        'Loss/Batch/cd_coarse', cd_coarse, n_itr)
                    train_writer.add_scalar(
                        'Loss/Batch/cd_fine', cd_fine, n_itr)
                    train_writer.add_scalar(
                        'Loss/Batch/cd_total', cd_total, n_itr)

                    batch_time.update(time() - batch_end_time)
                    batch_end_time = time()
                    t.set_description('[Epoch %d/%d][Batch %d/%d]' %
                                      (epoch_idx, cfg.TRAIN.N_EPOCHS, batch_idx + 1, n_batches))
                    t.set_postfix(loss='%s' % ['%.4f' % l for l in [
                        cd_feat, cd_coarse, cd_fine, cd_total]])

                    if 'BBWEIGHTS' not in cfg.CONST and steps <= cfg.TRAIN.WARMUP_STEPS:
                        lr_scheduler.step()
                        steps += 1

        avg_feat = total_cd_feat / n_batches
        avg_cdc = total_cd_coarse / n_batches
        avg_cdf = total_cd_fine / n_batches
        avg_cdt = total_cd_loss / n_batches

        lr_scheduler.step()
        print('epoch: ', epoch_idx, 'optimizer: ',
              optimizer.param_groups[0]['lr'])
        epoch_end_time = time()
        train_writer.add_scalar(
            'Loss/Epoch/feat_matching', avg_feat, epoch_idx)
        train_writer.add_scalar('Loss/Epoch/cd_coarse', avg_cdc, epoch_idx)
        train_writer.add_scalar('Loss/Epoch/cd_fine', avg_cdf, epoch_idx)
        train_writer.add_scalar('Loss/Epoch/cd_total', avg_cdt, epoch_idx)
        logging.info(
            '[Epoch %d/%d] EpochTime = %.3f (s) Losses = %s' %
            (epoch_idx, cfg.TRAIN.N_EPOCHS, epoch_end_time - epoch_start_time, ['%.4f' % l for l in [avg_feat, avg_cdc, avg_cdf, avg_cdt]]))

        # Validate the current model
        cd_eval = test_backbone(
            cfg, epoch_idx, val_data_loader, val_writer, as_autoencoder)

        # Save checkpoints
        if epoch_idx % cfg.TRAIN.SAVE_FREQ == 0 or cd_eval < best_metrics:
            file_name = 'pcnbackbone-best.pth' if cd_eval < best_metrics else 'ckpt-epoch-%03d.pth' % epoch_idx
            output_path = os.path.join(cfg.DIR.CHECKPOINTS, file_name)

            lr_scheduler_save = lr_scheduler
            if 'BBWEIGHTS' not in cfg.CONST:  # train from scratch
                lr_scheduler_save = lr_scheduler_save.after_scheduler

            torch.save({
                'epoch_index': epoch_idx,
                'best_metrics': best_metrics,
                'steps': steps,
                'model': as_autoencoder.state_dict(),
                'optimizer': optimizer.state_dict(),
                'lr_scheduler': lr_scheduler_save.state_dict()
            }, output_path)

            logging.info('Saved checkpoint to %s ...' % output_path)
            if cd_eval < best_metrics:
                best_metrics = cd_eval

    train_writer.close()
    val_writer.close()


if __name__ == "__main__":
    SAModulesInit(
        "checkpoint/backbone/checkpoints/2021-12-31T15:16:37.450752/pcnbackbone-best.pth")

    rmse = RMSELoss()
    a = torch.zeros((3, 1024))
    b = torch.ones((3, 1024))
    print(rmse(a, b))
