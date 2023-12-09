#import _init_path
from scipy.fft import fft, ifft, fftfreq, fftshift #solve the ImportError: /cm/local/apps/gcc/11.2.0/lib64/libstdc++.so.6: version `GLIBCXX_3.4.30' not found
import PIL
import argparse
import datetime
import glob
import os
from pathlib import Path
from test import repeat_eval_ckpt

import torch
import torch.nn as nn
from tensorboardX import SummaryWriter

from mydetector3d.config import cfg, cfg_from_list, cfg_from_yaml_file, log_config_to_file
from mydetector3d.datasets import build_dataloader
from mydetector3d.models import build_network, model_fn_decorator
from mydetector3d.utils import common_utils
from mydetector3d.tools.optimization import build_optimizer, build_scheduler
from mydetector3d.tools.train_utils import train_model
from torch.utils.data import DistributedSampler as DistributedSampler
#.tools.train_utils import train_model

import os
os.environ['CUDA_VISIBLE_DEVICES'] = "0" #"0,1"
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:32"

#output/kitti_models/pointpillar/0413/ckpt/checkpoint_epoch_128.pth
#/home/010796032/3DObject/modelzoo_openpcdet/pointpillar_7728.pth

#'mydetector3d/tools/cfgs/kitti_models/second_multihead.yaml'

#'mydetector3d/tools/cfgs/kitti_models/my3dmodel_multihead.yaml'
#'mydetector3d/tools/cfgs/kitti_models/my3dmodel.yaml'

from mydetector3d.models.detectors.pointpillar import PointPillar
from mydetector3d.models.detectors.second_net import SECONDNet
from mydetector3d.models.detectors.voxelnext import VoxelNeXt
from mydetector3d.models.detectors.my3dmodel import My3Dmodel
from mydetector3d.models.detectors.my3dmodelv2 import My3Dmodelv2
from mydetector3d.models.detectors.bevfusion import BevFusion
from mydetector3d.models.detectors.centerpoint import CenterPoint
from mydetector3d.models.detectors.centerpoint_second import SECONDCenterpoint
__modelall__ = {
    #'Detector3DTemplate': Detector3DTemplate,
     'SECONDNet': SECONDNet,
     'SECONDCenterpoint':SECONDCenterpoint,
    # 'PartA2Net': PartA2Net,
    # 'PVRCNN': PVRCNN,
     'CenterPoint': CenterPoint,
     'PointPillar': PointPillar,
     'My3Dmodel': My3Dmodel,
     'My3Dmodelv2': My3Dmodelv2,
     'VoxelNeXt': VoxelNeXt,
     'BevFusion': BevFusion
}

from mydetector3d.datasets.kitti.kitti_dataset import KittiDataset
from mydetector3d.datasets.kitti.waymokitti_dataset import WaymoKittiDataset
from mydetector3d.datasets.kitti.dairkitti_dataset import DairKittiDataset
from mydetector3d.datasets.waymo.waymo_dataset import WaymoDataset
from mydetector3d.datasets.nuscenes.nuscenes_dataset import NuScenesDataset
from functools import partial
from torch.utils.data import DataLoader
__datasetall__ = {
    'KittiDataset': KittiDataset,
    'WaymoKittiDataset': WaymoKittiDataset,
    'WaymoDataset': WaymoDataset,
    'DairKittiDataset': DairKittiDataset,
    'NuScenesDataset': NuScenesDataset
}

#'mydetector3d/tools/cfgs/waymo_models/myvoxelnext.yaml'
#'mydetector3d/tools/cfgs/waymo_models/myvoxelnext_ioubranch.yaml'
#'mydetector3d/tools/cfgs/waymo_models/mysecond.yaml'

#'mydetector3d/tools/cfgs/waymokitti_models/voxelnext_3class.yaml'

#'mydetector3d/tools/cfgs/waymokitti_models/second.yaml'
#'mydetector3d/tools/cfgs/waymo_models/mysecond.yaml
#'mydetector3d/tools/cfgs/dairkitti_models/mybevfusion.yaml'

#'mydetector3d/tools/cfgs/nuscenes_models/bevfusion.yaml'
#'mydetector3d/tools/cfgs/nuscenes_models/cbgs_pp_multihead.yaml'
def parse_config():
    parser = argparse.ArgumentParser(description='arg parser')
    parser.add_argument('--cfg_file', type=str, default='mydetector3d/tools/cfgs/waymokitti_models/centerpoint_second.yaml', help='specify the config for training')
    parser.add_argument('--batch_size', type=int, default=32, required=False, help='batch size for training')
    parser.add_argument('--epochs', type=int, default=35, required=False, help='number of epochs to train for')
    parser.add_argument('--workers', type=int, default=2, help='number of workers for dataloader')
    parser.add_argument('--extra_tag', type=str, default='varsha', help='extra tag for this experiment')
    parser.add_argument('--ckpt', type=str, default='/home/student/models/waymokitti_models/centerpoint_second/varsha2/ckpt/latest_model.pth') # '/home/student/models/waymokitti_models/pointpillar/1122/ckpt/latest_model.pth'
    parser.add_argument('--outputfolder', type=str, default='/home/student/models', help='output folder path')
    parser.add_argument('--pretrained_model', type=str, default=None, help='pretrained_model')
    parser.add_argument('--launcher', choices=['none', 'pytorch', 'slurm'], default='none')
    parser.add_argument('--tcp_port', type=int, default=18888, help='tcp port for distrbuted training')
    parser.add_argument('--sync_bn', action='store_true', default=False, help='whether to use sync bn')
    parser.add_argument('--fix_random_seed', action='store_true', default=False, help='')
    parser.add_argument('--ckpt_save_interval', type=int, default=8, help='number of training epochs')
    parser.add_argument('--local_rank', type=int, default=0, help='local rank for distributed training')
    parser.add_argument('--max_ckpt_save_num', type=int, default=30, help='max number of saved checkpoint')
    parser.add_argument('--merge_all_iters_to_one_epoch', action='store_true', default=False, help='')
    parser.add_argument('--set', dest='set_cfgs', default=None, nargs=argparse.REMAINDER,
                        help='set extra config keys if needed')

    parser.add_argument('--max_waiting_mins', type=int, default=0, help='max waiting minutes')
    parser.add_argument('--start_epoch', type=int, default=0, help='')
    parser.add_argument('--num_epochs_to_eval', type=int, default=0, help='number of checkpoints to be evaluated')
    parser.add_argument('--save_to_file', action='store_true', default=False, help='')
    
    parser.add_argument('--use_tqdm_to_record', action='store_true', default=False, help='if True, the intermediate losses will not be logged to file, only tqdm will be used')
    parser.add_argument('--logger_iter_interval', type=int, default=50, help='')
    parser.add_argument('--ckpt_save_time_interval', type=int, default=300, help='in terms of seconds')
    parser.add_argument('--wo_gpu_stat', action='store_true', help='')
    parser.add_argument('--use_amp', action='store_true', help='use mix precision training')
    

    args = parser.parse_args()

    cfg_from_yaml_file(args.cfg_file, cfg)
    cfg.TAG = Path(args.cfg_file).stem #Returns the substring from the beginning of filename: pointpillar
    #cfg.EXP_GROUP_PATH = '/'.join(args.cfg_file.split('/')[1:-1])  # remove 'cfgs' and 'xxxx.yaml'
    cfg.EXP_GROUP_PATH = '/'.join(args.cfg_file.split('/')[-2:-1])# get kitti_models
    
    args.use_amp = args.use_amp or cfg.OPTIMIZATION.get('USE_AMP', False)

    if args.set_cfgs is not None: #extra configuration keys
        cfg_from_list(args.set_cfgs, cfg)

    return args, cfg


def main():
    args, cfg = parse_config()
    if args.launcher == 'none':
        dist_train = False
        total_gpus = 1
    else:
        total_gpus, cfg.LOCAL_RANK = getattr(common_utils, 'init_dist_%s' % args.launcher)(
            args.tcp_port, args.local_rank, backend='nccl'
        )
        dist_train = True

    if args.batch_size is None:
        args.batch_size = cfg.OPTIMIZATION.BATCH_SIZE_PER_GPU
    else:
        assert args.batch_size % total_gpus == 0, 'Batch size should match the number of gpus'
        args.batch_size = args.batch_size // total_gpus

    args.epochs = cfg.OPTIMIZATION.NUM_EPOCHS if args.epochs is None else args.epochs

    if args.fix_random_seed:
        common_utils.set_random_seed(666 + cfg.LOCAL_RANK)

    #output_dir = cfg.ROOT_DIR / 'output' / cfg.EXP_GROUP_PATH / cfg.TAG / args.extra_tag
    output_dir = Path(args.outputfolder) / cfg.EXP_GROUP_PATH / cfg.TAG / args.extra_tag
    ckpt_dir = output_dir / 'ckpt'
    output_dir.mkdir(parents=True, exist_ok=True)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    log_file = output_dir / ('train_%s.log' % datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    logger = common_utils.create_logger(log_file, rank=cfg.LOCAL_RANK)

    # log to file
    logger.info('**********************Start logging**********************')
    #gpu_list = os.environ['CUDA_VISIBLE_DEVICES'] if 'CUDA_VISIBLE_DEVICES' in os.environ.keys() else 'ALL'
    #logger.info('CUDA_VISIBLE_DEVICES=%s' % gpu_list)
    print(os.environ.keys())
    print(os.environ['CUDA_VISIBLE_DEVICES'])
    #os.environ['CUDA_VISIBLE_DEVICES'] = "2"
    #print(os.environ['CUDA_VISIBLE_DEVICES'])
    num_gpus= torch.cuda.device_count()
    print("Device numbers:", num_gpus)
    for gpuidx in range(num_gpus):
        print("Device properties:", torch.cuda.get_device_properties(gpuidx))
        print("Utilization:", torch.cuda.utilization(gpuidx))
        print('Memory Usage:')
        print('Allocated:', round(torch.cuda.memory_allocated(gpuidx)/1024**3,1), 'GB')
        print('Cached:   ', round(torch.cuda.memory_reserved(gpuidx)/1024**3,1), 'GB')

    if dist_train:
        logger.info('Training in distributed mode : total_batch_size: %d' % (total_gpus * args.batch_size))
    else:
        logger.info('Training with a single process')
        
    for key, val in vars(args).items():
        logger.info('{:16} {}'.format(key, val))
    log_config_to_file(cfg, logger=logger)
    if cfg.LOCAL_RANK == 0:
        os.system('cp %s %s' % (args.cfg_file, output_dir))

    tb_log = SummaryWriter(log_dir=str(output_dir / 'tensorboard')) if cfg.LOCAL_RANK == 0 else None

    logger.info("----------- Create dataloader & network & optimizer -----------")
    # train_set, train_loader, train_sampler = build_dataloader(
    #     dataset_cfg=cfg.DATA_CONFIG,
    #     class_names=cfg.CLASS_NAMES,
    #     batch_size=args.batch_size,
    #     dist=dist_train, workers=args.workers,
    #     logger=logger,
    #     training=True,
    #     merge_all_iters_to_one_epoch=args.merge_all_iters_to_one_epoch,
    #     total_epochs=args.epochs,
    #     seed=666 if args.fix_random_seed else None
    # )
    training = True
    dataset_cfg = cfg.DATA_CONFIG
    class_names=cfg.CLASS_NAMES
    train_set = __datasetall__[dataset_cfg.DATASET](
        dataset_cfg=dataset_cfg,
        class_names=class_names,
        root_path=None,
        training=training,
        logger=logger,
    )
    train_loader = DataLoader(
        train_set, batch_size=args.batch_size, pin_memory=True, num_workers=args.workers,
        shuffle=None, collate_fn=train_set.collate_batch,
        drop_last=False, sampler=None, timeout=0, worker_init_fn=partial(common_utils.worker_init_fn, seed=None)
    )



    #return PointPillar module with module list
    #model = build_network(model_cfg=cfg.MODEL, num_class=len(cfg.CLASS_NAMES), dataset=train_set)
  
    model_cfg=cfg.MODEL
    model_name=model_cfg.NAME
    
    num_class=len(cfg.CLASS_NAMES)
    model = __modelall__[model_name](
        model_cfg=model_cfg, num_class=num_class, dataset=train_set
    )
    if args.sync_bn:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
    model.cuda()

    if dist_train:
        if training:
            sampler = torch.utils.data.distributed.DistributedSampler(train_set)
        else:
            rank, world_size = common_utils.get_dist_info()
            sampler = DistributedSampler(train_set, world_size, rank, shuffle=False)
    else:
        sampler = None

    optimizer = build_optimizer(model, cfg.OPTIMIZATION)

    # load checkpoint if it is possible
    start_epoch = it = 0
    last_epoch = -1
    if args.pretrained_model is not None:
        model.load_params_from_file(filename=args.pretrained_model, to_cpu=dist_train, logger=logger)

    if args.ckpt is not None:
        it, start_epoch = model.load_params_with_optimizer(args.ckpt, to_cpu=dist_train, optimizer=optimizer, logger=logger)
        last_epoch = start_epoch + 1
    torch.cuda.empty_cache()
    model.train()  # before wrap to DistributedDataParallel to support fixed some parameters
    if dist_train:
        model = nn.parallel.DistributedDataParallel(model, device_ids=[cfg.LOCAL_RANK % torch.cuda.device_count()])
    logger.info(f'----------- Model {cfg.MODEL.NAME} created, param count: {sum([m.numel() for m in model.parameters()])} -----------')
    logger.info(model)

    lr_scheduler, lr_warmup_scheduler = build_scheduler(
        optimizer, total_iters_each_epoch=len(train_loader), total_epochs=args.epochs,
        last_epoch=last_epoch, optim_cfg=cfg.OPTIMIZATION
    )

    # -----------------------start training---------------------------
    logger.info('**********************Start training %s/%s(%s)**********************'
                % (cfg.EXP_GROUP_PATH, cfg.TAG, args.extra_tag))

    torch.cuda.empty_cache()
    train_model(
        model,
        optimizer,
        train_loader,
        model_func=model_fn_decorator(),
        lr_scheduler=lr_scheduler,
        optim_cfg=cfg.OPTIMIZATION,
        start_epoch=start_epoch,
        total_epochs=args.epochs,
        start_iter=it,
        rank=cfg.LOCAL_RANK,
        tb_log=tb_log,
        ckpt_save_dir=ckpt_dir,
        train_sampler=sampler,
        lr_warmup_scheduler=lr_warmup_scheduler,
        ckpt_save_interval=args.ckpt_save_interval,
        max_ckpt_save_num=args.max_ckpt_save_num,
        merge_all_iters_to_one_epoch=args.merge_all_iters_to_one_epoch, 
        logger=logger, 
        logger_iter_interval=args.logger_iter_interval,
        ckpt_save_time_interval=args.ckpt_save_time_interval,
        use_logger_to_record=not args.use_tqdm_to_record, 
        show_gpu_stat=not args.wo_gpu_stat,
        use_amp=args.use_amp
    )

    if hasattr(train_set, 'use_shared_memory') and train_set.use_shared_memory:
        train_set.clean_shared_memory()

    logger.info('**********************End training %s/%s(%s)**********************\n\n\n'
                % (cfg.EXP_GROUP_PATH, cfg.TAG, args.extra_tag))

    # logger.info('**********************Start evaluation %s/%s(%s)**********************' %
    #             (cfg.EXP_GROUP_PATH, cfg.TAG, args.extra_tag))
    # test_set, test_loader, sampler = build_dataloader(
    #     dataset_cfg=cfg.DATA_CONFIG,
    #     class_names=cfg.CLASS_NAMES,
    #     batch_size=args.batch_size,
    #     dist=dist_train, workers=args.workers, logger=logger, training=False
    # )
    # eval_output_dir = output_dir / 'eval' / 'eval_with_train'
    # eval_output_dir.mkdir(parents=True, exist_ok=True)
    # args.start_epoch = max(args.epochs - args.num_epochs_to_eval, 0)  # Only evaluate the last args.num_epochs_to_eval epochs

    # repeat_eval_ckpt(
    #     model.module if dist_train else model,
    #     test_loader, args, eval_output_dir, logger, ckpt_dir,
    #     dist_test=dist_train
    # )
    # logger.info('**********************End evaluation %s/%s(%s)**********************' %
    #             (cfg.EXP_GROUP_PATH, cfg.TAG, args.extra_tag))


if __name__ == '__main__':
    main()
