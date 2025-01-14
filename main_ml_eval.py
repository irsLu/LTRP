# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# DeiT: https://github.com/facebookresearch/deit
# BEiT: https://github.com/microsoft/unilm/tree/master/beit
# --------------------------------------------------------

import argparse
import numpy as np
import os
from pathlib import Path
import torch
import torch.backends.cudnn as cudnn
from torch.utils.tensorboard import SummaryWriter
import utils.lr_decay as lrd
import utils.misc as misc
from utils.misc import NativeScalerWithGradNormCount as NativeScaler
import models_ltrp_vit
from engine_finetune import evaluate_multi_label_coco
from factory import get_score_net
from collections import OrderedDict
from multi_classification.helper_functions import CocoDetection, ModelEma, OTE_detection
from utils.datasets import build_transform
from multi_classification.losses import AsymmetricLoss
from multi_classification.ml_decoder import add_ml_decoder_head


def get_args_parser():
    parser = argparse.ArgumentParser('MAE fine-tuning for image classification', add_help=False)
    parser.add_argument('--batch_size', default=64, type=int,
                        help='Batch size per GPU (effective batch size is batch_size * accum_iter * # gpus')
    parser.add_argument('--epochs', default=50, type=int)
    parser.add_argument('--accum_iter', default=1, type=int,
                        help='Accumulate gradient iterations (for increasing the effective batch size under memory constraints)')

    # Model parameters
    parser.add_argument('--model', default='vit_base', type=str, metavar='MODEL',
                        help='Name of model to train')

    parser.add_argument('--input_size', default=224, type=int,
                        help='images input size')

    parser.add_argument('--drop_path', type=float, default=0.1, metavar='PCT',
                        help='Drop path rate (default: 0.1)')

    # Optimizer parameters
    parser.add_argument('--clip_grad', type=float, default=None, metavar='NORM',
                        help='Clip gradient norm (default: None, no clipping)')
    parser.add_argument('--weight_decay', type=float, default=0.05,
                        help='weight decay (default: 0.05)')

    parser.add_argument('--lr', type=float, default=None, metavar='LR',
                        help='learning rate (absolute lr)')
    parser.add_argument('--blr', type=float, default=1e-3, metavar='LR',
                        help='base learning rate: absolute_lr = base_lr * total_batch_size / 256')
    parser.add_argument('--layer_decay', type=float, default=0.75,
                        help='layer-wise lr decay from ELECTRA/BEiT')

    parser.add_argument('--min_lr', type=float, default=1e-6, metavar='LR',
                        help='lower lr bound for cyclic schedulers that hit 0')

    parser.add_argument('--warmup_epochs', type=int, default=5, metavar='N',
                        help='epochs to warmup LR')

    # Augmentation parameters
    parser.add_argument('--color_jitter', type=float, default=None, metavar='PCT',
                        help='Color jitter factor (enabled only when not using Auto/RandAug)')
    parser.add_argument('--aa', type=str, default='rand-m9-mstd0.5-inc1', metavar='NAME',
                        help='Use AutoAugment policy. "v0" or "original". " + "(default: rand-m9-mstd0.5-inc1)'),
    parser.add_argument('--smoothing', type=float, default=0.1,
                        help='Label smoothing (default: 0.1)')

    # * Random Erase params
    parser.add_argument('--reprob', type=float, default=0.25, metavar='PCT',
                        help='Random erase prob (default: 0.25)')
    parser.add_argument('--remode', type=str, default='pixel',
                        help='Random erase mode (default: "pixel")')
    parser.add_argument('--recount', type=int, default=1,
                        help='Random erase count (default: 1)')
    parser.add_argument('--resplit', action='store_true', default=False,
                        help='Do not random erase first (clean) augmentation split')

    # * Mixup params
    parser.add_argument('--mixup', type=float, default=0,
                        help='mixup alpha, mixup enabled if > 0.')
    parser.add_argument('--cutmix', type=float, default=0,
                        help='cutmix alpha, cutmix enabled if > 0.')
    parser.add_argument('--cutmix_minmax', type=float, nargs='+', default=None,
                        help='cutmix min/max ratio, overrides alpha and enables cutmix if set (default: None)')
    parser.add_argument('--mixup_prob', type=float, default=1.0,
                        help='Probability of performing mixup or cutmix when either/both is enabled')
    parser.add_argument('--mixup_switch_prob', type=float, default=0.5,
                        help='Probability of switching to cutmix when both mixup and cutmix enabled')
    parser.add_argument('--mixup_mode', type=str, default='batch',
                        help='How to apply mixup/cutmix params. Per "batch", "pair", or "elem"')

    # * Finetuning params
    parser.add_argument('--finetune', default='',
                        help='finetune from checkpoint')
    parser.add_argument('--global_pool', action='store_true')
    parser.set_defaults(global_pool=True)
    parser.add_argument('--cls_token', action='store_false', dest='global_pool',
                        help='Use class token instead of global pool for classification')

    # Dataset parameters
    parser.add_argument('--data_path', default='/datasets01/imagenet_full_size/061417/', type=str,
                        help='dataset path')
    parser.add_argument('--nb_classes', default=80, type=int,
                        help='number of the classification types')

    parser.add_argument('--output_dir', default='./output_dir',
                        help='path where to save, empty for no saving')
    parser.add_argument('--log_dir', default=None,
                        help='path where to tensorboard log')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=0, type=int)
    parser.add_argument('--resume', default='',
                        help='resume from checkpoint')

    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', action='store_true',
                        help='Perform evaluation only')
    parser.add_argument('--dist_eval', action='store_true', default=False,
                        help='Enabling distributed evaluation (recommended during training for faster monitor')
    parser.add_argument('--num_workers', default=10, type=int)
    parser.add_argument('--pin_mem', action='store_true',
                        help='Pin CPU memory in DataLoader for more efficient (sometimes) transfer to GPU.')
    parser.add_argument('--no_pin_mem', action='store_false', dest='pin_mem')
    parser.set_defaults(pin_mem=True)

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--local_rank', default=-1, type=int)
    parser.add_argument('--dist_on_itp', action='store_true')
    parser.add_argument('--dist_url', default='env://',
                        help='url used to set up distributed training')
    parser.add_argument('--save_ckpt_freq', default=20, type=int)

    # -----

    parser.add_argument('--keep_nums', default=49, type=int, )
    parser.add_argument('--score_net', default='vit_small', type=str)
    parser.add_argument('--finetune_ltrp', default='', help='finetune from checkpoint')
    parser.add_argument('--random_chose', action='store_true',
                        help='Perform evaluation only')
    parser.add_argument('--finetune_scorenet', action='store_true',
                        help='Perform evaluation only')
    parser.add_argument('--use_mask_idx', action='store_true',
                        help='Perform evaluation only')
    parser.add_argument('--dino_random_head', action='store_true',
                        help='Perform evaluation only')
    # multi classifition
    parser.add_argument('--decoder_embedding', default=768, type=int, )
    parser.add_argument('--count_max', default=0, type=int)
    parser.add_argument('--ltrp_cluster_ratio', type=float, default=0.7)
    return parser


def main(args):
    misc.init_distributed_mode(args)

    print('job dir: {}'.format(os.path.dirname(os.path.realpath(__file__))))
    print("{}".format(args).replace(', ', ',\n'))

    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + misc.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)

    cudnn.benchmark = True

    train_transform = build_transform(True, args)
    val_transform = build_transform(False, args)

    if 'voc' in args.data_path:
        # COCO Data loading
        instances_path_val = os.path.join(args.data_path, 'annotations/instances_val.json')
        instances_path_train = os.path.join(args.data_path, 'annotations/instances_train.json')

        data_path_train = f'{args.data_path}/train'  # args.data
        data_path_val = f'{args.data_path}/val'  # args.data

        dataset_train = OTE_detection(data_path_train,
                                      instances_path_train, train_transform, count_max=args.count_max)
        dataset_val = OTE_detection(data_path_val,
                                    instances_path_val, val_transform, count_max=args.count_max)
    else:
        # COCO Data loading
        instances_path_val = os.path.join(args.data_path, 'annotations/instances_val2017.json')
        instances_path_train = os.path.join(args.data_path, 'annotations/instances_train2017.json')

        data_path_val = f'{args.data_path}/val2017'  # args.data
        data_path_train = f'{args.data_path}/train2017'  # args.data

        dataset_train = CocoDetection(data_path_train,
                                      instances_path_train, train_transform)
        dataset_val = CocoDetection(data_path_val,
                                    instances_path_val, val_transform, count_max=args.count_max)

    if True:  # args.distributed:
        num_tasks = misc.get_world_size()
        global_rank = misc.get_rank()
        sampler_train = torch.utils.data.DistributedSampler(
            dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True
        )
        print("Sampler_train = %s" % str(sampler_train))
        if args.dist_eval:
            if len(dataset_val) % num_tasks != 0:
                print('Warning: Enabling distributed evaluation with an eval dataset not divisible by process number. '
                      'This will slightly alter validation results as extra duplicate entries are added to achieve '
                      'equal num of samples per-process.')
            sampler_val = torch.utils.data.DistributedSampler(
                dataset_val, num_replicas=num_tasks, rank=global_rank,
                shuffle=True)  # shuffle=True to reduce monitor bias
        else:
            sampler_val = torch.utils.data.SequentialSampler(dataset_val)
    else:
        sampler_train = torch.utils.data.RandomSampler(dataset_train)
        sampler_val = torch.utils.data.SequentialSampler(dataset_val)

    if global_rank == 0 and args.log_dir is not None and not args.eval:
        os.makedirs(args.log_dir, exist_ok=True)
        log_writer = SummaryWriter(log_dir=args.log_dir)
    else:
        log_writer = None

    data_loader_val = torch.utils.data.DataLoader(
        dataset_val, sampler=sampler_val,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        pin_memory=args.pin_mem,
        drop_last=False
    )

    score_net = get_score_net(args)

    model = models_ltrp_vit.__dict__[args.model](
        num_classes=args.nb_classes,
        drop_path_rate=args.drop_path,
        global_pool=args.global_pool,
        keep_nums=args.keep_nums,
        score_net=score_net,
        random_chose=args.random_chose,
        finetune_scorenet=args.finetune_scorenet
    )

    model = add_ml_decoder_head(model, num_classes=args.nb_classes, num_of_groups=-1,
                                decoder_embedding=args.decoder_embedding, zsl=0)

    if args.finetune:
        checkpoint = torch.load(args.finetune, map_location='cpu')
        new_checkpoint_model = OrderedDict()
        ckpt = checkpoint['model']
        for k, v in ckpt.items():
            if not k.startswith('score_net.'):
                new_checkpoint_model[k] = ckpt[k]

        msg = model.load_state_dict(new_checkpoint_model, strict=False)
        print(msg)

    if args.finetune_ltrp:
        checkpoint = torch.load(args.finetune_ltrp, map_location='cpu')
        if args.score_net.startswith('dpc_knn'):
            state_dict = checkpoint
            # remove `module.` prefix
            state_dict = {k.replace("module.", ""): v for k, v in state_dict.items()}
            # remove `backbone.` prefix induced by multicrop wrapper
            state_dict = {k.replace("backbone.", ""): v for k, v in state_dict.items()}
            checkpoint_model = {k.replace("encoder.", ""): v for k, v in state_dict.items()}
            model.score_net.init_backbone()
            msg = model.score_net.backbone.load_state_dict(checkpoint_model, strict=False)
        elif args.score_net.startswith('gf_net'):
            model.score_net.load(checkpoint)
        elif args.score_net.startswith('IA_RED'):
            msg = model.score_net.model.load_state_dict(checkpoint)
        else:
            if args.score_net.startswith('evit'):
                checkpoint_model = checkpoint['model']

            elif args.score_net.startswith('moco'):
                checkpoint_model = OrderedDict()
                ckpt = checkpoint['state_dict']
                for k, v in ckpt.items():
                    if k.startswith('module.encoder_q.'):
                        checkpoint_model[k[len('module.encoder_q.'):]] = ckpt[k]
            elif args.score_net.startswith('dino_vit_small'):
                state_dict = checkpoint
                # remove `module.` prefix
                state_dict = {k.replace("module.", ""): v for k, v in state_dict.items()}
                # remove `backbone.` prefix induced by multicrop wrapper
                state_dict = {k.replace("backbone.", ""): v for k, v in state_dict.items()}
                checkpoint_model = {k.replace("encoder.", ""): v for k, v in state_dict.items()}
            else:
                checkpoint_model = OrderedDict()
                checkpoint_model_ltrp = checkpoint['model']
                for k, v in checkpoint_model_ltrp.items():
                    if k.startswith('score_net.'):
                        checkpoint_model[k[10:]] = checkpoint_model_ltrp[k]
            msg = model.score_net.load_state_dict(checkpoint_model, strict=False)
        print(args.score_net + " load ", msg)

    model.to(device)

    model_without_ddp = model
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)

    print('number of params (M): %.2f' % (n_parameters / 1.e6))

    eff_batch_size = args.batch_size * args.accum_iter * misc.get_world_size()

    if args.lr is None:  # only base_lr is specified
        args.lr = args.blr * eff_batch_size / 256

    print("base lr: %.2e" % (args.lr * 256 / eff_batch_size))
    print("actual lr: %.2e" % args.lr)

    print("accumulate grad iterations: %d" % args.accum_iter)
    print("effective batch size: %d" % eff_batch_size)

    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu], find_unused_parameters=True)
        model_without_ddp = model.module

    # build optimizer with layer-wise lr decay (lrd)
    param_groups = lrd.param_groups_lrd(model_without_ddp, args.weight_decay,
                                        no_weight_decay_list=model_without_ddp.no_weight_decay(),
                                        layer_decay=args.layer_decay
                                        )
    optimizer = torch.optim.AdamW(param_groups, lr=args.lr)
    loss_scaler = NativeScaler()

    criterion = AsymmetricLoss(gamma_neg=4, gamma_pos=0, clip=0.05, disable_torch_grad_focal_loss=True)

    print("criterion = %s" % str(criterion))

    misc.load_model(args=args, model_without_ddp=model_without_ddp, optimizer=optimizer, loss_scaler=loss_scaler)
    ema_model = ModelEma(model, 0.9997)
    test_stats = evaluate_multi_label_coco(data_loader_val, model, ema_model, device, args)
    print(f"mAP of the network on the {len(dataset_val)} test images: {test_stats['mAP']:.1f}%")
    exit(0)


if __name__ == '__main__':
    args = get_args_parser()
    args = args.parse_args()

    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
