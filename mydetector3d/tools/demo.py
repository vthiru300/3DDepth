import argparse
import glob
from pathlib import Path

try:
    import open3d
    from visual_utils import open3d_vis_utils as V
    OPEN3D_FLAG = True
except:
    import mayavi.mlab as mlab
    from visual_utils import mayavivisualize_utils as V
    OPEN3D_FLAG = False

import numpy as np
import torch
import os

from mydetector3d.config import cfg, cfg_from_yaml_file
from mydetector3d.datasets import DatasetTemplate
from mydetector3d.models import build_network #, load_data_to_gpu
from mydetector3d.utils import common_utils


def load_data_to_gpu(batch_dict, device):
    for key, val in batch_dict.items():
        if not isinstance(val, np.ndarray):
            continue
        elif key in ['frame_id', 'metadata', 'calib']:
            continue
        # elif key in ['images']:
        #     batch_dict[key] = kornia.image_to_tensor(val).float().cuda().contiguous()
        elif key in ['image_shape']:
            batch_dict[key] = batch_dict[key] = torch.from_numpy(val).int().to(device) #torch.from_numpy(val).int().cuda()
        else:
            batch_dict[key] = torch.from_numpy(val).float().to(device) #torch.from_numpy(val).float().cuda()

class DemoDataset(DatasetTemplate):
    def __init__(self, dataset_cfg, class_names, training=True, root_path=None, logger=None, ext='.bin'):
        """
        Args:
            root_path:
            dataset_cfg:
            class_names:
            training:
            logger:
        """
        super().__init__(
            dataset_cfg=dataset_cfg, class_names=class_names, training=training, root_path=root_path, logger=logger
        )
        self.root_path = root_path
        self.ext = ext
        data_file_list = glob.glob(
            str(root_path / f'*{self.ext}')) if self.root_path.is_dir() else [self.root_path]

        data_file_list.sort()
        self.sample_file_list = data_file_list

    def __len__(self):
        return len(self.sample_file_list)

    def __getitem__(self, index):
        if self.ext == '.bin':
            points = np.fromfile(
                self.sample_file_list[index], dtype=np.float32)
            points = points.reshape(-1, 4)
        elif self.ext == '.npy':
            points = np.load(self.sample_file_list[index])
        else:
            raise NotImplementedError

        input_dict = {
            'points': points,
            'frame_id': index,
        }

        data_dict = self.prepare_data(data_dict=input_dict)
        return data_dict

# /data/cmpe249-fa22/kitti/testing/velodyne


def parse_config():
    parser = argparse.ArgumentParser(description='arg parser')
    parser.add_argument('--cfg_file', type=str, default='mydetector3d/tools/cfgs/waymokitti_models/second.yaml',
                        help='specify the config for demo')
    parser.add_argument('--data_path', type=str, default='/home/lkk/Developer/data/001766.bin',
                        help='specify the point cloud data file or directory') #data/waymokittisample/velodyne/
    parser.add_argument('--ckpt', type=str, default='/home/lkk/Developer/data/waymo_second_epoch128.pth',
                        help='specify the pretrained model') #waymokitti_second_epoch128
    parser.add_argument('--ext', type=str, default='.bin',
                        help='specify the extension of your point cloud data file (.bin or npy)')

    args = parser.parse_args()

    cfg_from_yaml_file(args.cfg_file, cfg)

    return args, cfg

# If you doesn't have the intensity information, just set them to zeros.
# If you have the intensity information, you should normalize them to [0, 1].
# points[:, 3] = 0
# np.save(`my_data.npy`, points)


def main():
    print("Current directory", os.getcwd())
    args, cfg = parse_config()
    logger = common_utils.create_logger()
    logger.info(
        '-----------------Quick Demo of OpenPCDet-------------------------')
    demo_dataset = DemoDataset(
        dataset_cfg=cfg.DATA_CONFIG, class_names=cfg.CLASS_NAMES, training=False,
        root_path=Path(args.data_path), ext=args.ext, logger=logger
    )
    logger.info(f'Total number of samples: \t{len(demo_dataset)}')

    model = build_network(model_cfg=cfg.MODEL, num_class=len(
        cfg.CLASS_NAMES), dataset=demo_dataset)
    model.load_params_from_file(filename=args.ckpt, logger=logger, to_cpu=True)
    gpu_id=torch.device('cuda:0')
    #model.cuda()
    model.to(gpu_id)
    model.eval()
    with torch.no_grad():
        for idx, data_dict in enumerate(demo_dataset):
            logger.info(f'Visualized sample index: \t{idx + 1}')
            data_dict = demo_dataset.collate_batch([data_dict])
            load_data_to_gpu(data_dict, gpu_id)
            #data_dict.to(gpu_id) #'dict' object has no attribute 'to'
            pred_dicts, _ = model.forward(data_dict)
            print(pred_dicts[0])

            V.draw_scenes(
                points=data_dict['points'][:,
                                           1:], ref_boxes=pred_dicts[0]['pred_boxes'],
                ref_scores=pred_dicts[0]['pred_scores'], ref_labels=pred_dicts[0]['pred_labels']
            )

            if not OPEN3D_FLAG:
                mlab.show(stop=True)

    logger.info('Demo done.')


if __name__ == '__main__':
    main()
